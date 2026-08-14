# Stage 2c — Slice 1 Fix Checkpoint

**For:** external reviewer (ChatGPT). Corrects the four findings in
`CLAUDE_STAGE2C_SLICE1_DEEP_REVIEW_FEEDBACK.md`. **Still no live route wired** —
this is the settlement service layer only. Slice 2 (live charge route under the
order-row lock) does not begin until this checkpoint is clean.

## Commit range
- **Slice-1 HEAD (reviewed):** `50f687a5c1f69a7e3d31e35f5d2c450e1aaa3ca9`
- **HEAD (this fix):** `e3c28f79d2e8e18e84bd4eccfe7a7716d3421c27`
- Branch: `fix/p0-security-and-payments`
- **Commit added:** `e3c28f7 fix(2c slice 1 review): transaction contract, CAS convergence, selection ownership`

## `git status --short`
No uncommitted **source** changes. Untracked non-source: handoffs, business
docs/decks, `.github/`, `restaurant-app-review.zip`.

## Finding-by-finding
| # | Sev | Finding | Status |
|---|---|---|---|
| 1 | CRIT | `commit=False` mismatch could be rolled back by the escaping exception | ✅ |
| 2 | CRIT | `TransitionConflict` treated as "someone settled" (could return Payment(None)) | ✅ |
| 3 | HIGH | `payment_factory` transaction contract implicit/unsafe | ✅ (contract fixed; enforcement lands with `pay_seat` refactor in slice 2) |
| 4 | HIGH | `line_selection` canonicalized by caller; fragile VARCHAR cap | ✅ |

## 1 — Settlement transaction contract (no exception controls the tx)
`settle_charge` now returns a structured **`SettlementResult`**
(`SETTLED` with the Payment, or `RECONCILED` with a reason and no Payment) and
**never raises to signal a mismatch**. On mismatch it transitions the attempt to
`REQUIRES_RECONCILIATION` (with `commit` per the caller) and returns — so the
reconciliation cannot be undone by an escaping exception rolling back the caller's
outer transaction.

**Contract:** the caller owns the transaction. In slice 2 the live route holds the
order-row `SELECT … FOR UPDATE`, calls `settle_charge(..., commit=False)`, and
commits once. Callers check `result.is_settled`; there is nothing to "remember to
commit despite an exception".

**Proof:** `test_mismatch_reconciliation_durable_across_outer_tx` — settle with
`commit=False` inside a transaction, assert a non-settled result, `db.commit()`,
then a **fresh session** confirms the attempt is durably `REQUIRES_RECONCILIATION`.
(Both amount and currency mismatch covered.)

## 2 — CAS-loser converges only on a proven winner
On a `TransitionConflict` at the SETTLED transition, `settle_charge` rolls back,
refreshes, and converges **only** when the DB truth proves a real winner:
```
attempt.status == SETTLED  AND  attempt.payment_id is not None  AND  Payment exists
```
Any other CAS loss — the attempt was moved to `REQUIRES_RECONCILIATION`, a
write-once id differed, etc. — **re-raises `TransitionConflict`**. It never
returns `db.get(Payment, None)` as a false "successful convergence".

**Proof (real two-session Postgres, READ COMMITTED):**
- `test_settlement_cas_converges_on_winner`: writer B loads the APPROVED attempt,
  writer A settles first, B's settle loses the CAS and **converges on A's Payment**.
- `test_settlement_cas_reraises_on_non_winner`: A parks the attempt in
  reconciliation instead of settling; B's settle loses the CAS and **re-raises**,
  reporting no false success.

## 3 — `payment_factory` transaction contract (explicit)
Documented and depended upon: `payment_factory` MUST use the same `Session` and
MUST NOT `commit`/`rollback` — it mutates + flushes only, so a lost CAS race rolls
the Payment back together with the attempt transition (`settle_charge` calls
`db.flush()`, never `commit`, and does `db.rollback()` on conflict).
**Slice-2 obligation (stated, not yet done):** refactor `pay_seat` into a
no-commit transactional core before using it as the factory, and prove (in slice
2, under the order lock) that an injected transition failure rolls back
Payment + allocations + seat mutations together with no early commit releasing the
lock.

## 4 — Selection canonicalization owned by the attempt service
`create_attempt(item_ids=…)` canonicalizes internally (sorted, de-duplicated) and
fingerprints/stores that — identity no longer depends on caller discipline.
Malformed ids raise an explicit `PaymentAttemptError` (no silent coercion).
`line_selection` is now **`TEXT`** (not `VARCHAR(500)`), so a large legitimate
selection never fails as a low-level DB error. Migration DDL updated to `TEXT`.

**Proof:** `test_selection_canonicalized_by_service` — `[3,2,1]` and `[1,1,2,3]`
under the same key resolve to the same attempt with `line_selection == "1,2,3"`;
`["not-an-int"]` raises a domain error. `test_different_selection_conflicts` —
different selection under the same key → `IdempotencyConflict`.

## Tests run — commands, counts (0 failures)
Env: Python 3.14, SQLAlchemy 2.0.51, psycopg 3.3.4 (test-only), Postgres 16 @ :5433.
```
# SQLite (default): full suite green
test_settlement          15 ok        (+ config/attempts/refunds/providers/... all PASS)
# PostgreSQL (PG_TEST_DSN set):
test_settlement          15 ok
test_pg_concurrency      PASS  (adds the two CAS-convergence proofs above)
test_payment_attempts / test_refund_attempts / test_pg_migration  PASS
```
No regression: the pre-existing migration + concurrency + provider suites remain
green on both engines.

## What is still NOT in slice 1 (unchanged from the prior checkpoint)
Live route wiring (slice 2, incl. the `pay_seat` no-commit refactor + real
concurrent-settlement PG proof under the order lock); refund settlement +
refundable-balance concurrency (slice 3); refund/void route (slice 4); the
consolidated 25-test acceptance matrix (`REVIEW_HANDOFF_STAGE_2C.md`).

## Areas I am least confident about
1. **`pay_seat` no-commit refactor (slice 2).** The settlement service is correct
   given a well-behaved factory, but the real guarantee that Payment + allocations
   + seat status roll back together depends on that refactor, which is slice-2
   work and not yet proven.
2. **Exact amount equality** on the pre-tip base vs. real Square sandbox rounding —
   to validate against live sandbox data before auto-settlement trusts it.
3. **Two-session CAS tests** rely on READ COMMITTED semantics on Postgres; the
   deterministic ordering (B loads, A commits, B loses) models the race but the
   full threaded proof under the order-row lock is slice 2.

---
## Fix diff (`git diff 50f687a..HEAD`)
```diff
diff --git a/app/migrate.py b/app/migrate.py
index fe6b19e..fa400a9 100644
--- a/app/migrate.py
+++ b/app/migrate.py
@@ -96,8 +96,9 @@ ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
     ("payment_attempt", "reconciled_by", "VARCHAR(60) NOT NULL DEFAULT ''"),
     ("payment_attempt", "reconciliation_note", "VARCHAR(300) NOT NULL DEFAULT ''"),
     ("refund_attempt", "intent_fingerprint", "VARCHAR(64) NOT NULL DEFAULT ''"),
-    # Stage 2c: the paid-item selection captured on the charge attempt.
-    ("payment_attempt", "line_selection", "VARCHAR(500) NOT NULL DEFAULT ''"),
+    # Stage 2c: the paid-item selection captured on the charge attempt (TEXT so a
+    # large legitimate selection never overflows a fragile VARCHAR cap).
+    ("payment_attempt", "line_selection", "TEXT NOT NULL DEFAULT ''"),
 )
 
 # (table, column, min_length, new DDL type). Columns whose type/length GREW
diff --git a/app/models/oltp.py b/app/models/oltp.py
index fd8b46f..91b0919 100644
--- a/app/models/oltp.py
+++ b/app/models/oltp.py
@@ -1068,8 +1068,9 @@ class PaymentAttempt(Base):
     intent_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
     # Canonical identity of WHAT is being paid — the sorted set of OrderItem ids
     # this attempt settles (Stage 2c). Part of the intent fingerprint, and what
-    # settlement reconciles against.
-    line_selection: Mapped[str] = mapped_column(String(500), nullable=False, default="")
+    # settlement reconciles against. TEXT (not a fragile VARCHAR cap) so a large
+    # legitimate selection never fails as a low-level DB error (slice-1 review #4).
+    line_selection: Mapped[str] = mapped_column(Text, nullable=False, default="")
 
     # Immutable payable snapshot (integer cents), locked before creation.
     subtotal_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
diff --git a/app/services/payment_attempts.py b/app/services/payment_attempts.py
index 772ab18..1c848cd 100644
--- a/app/services/payment_attempts.py
+++ b/app/services/payment_attempts.py
@@ -79,8 +79,13 @@ def new_idempotency_key() -> str:
 
 def canonical_selection(item_ids) -> str:
     """Stable identity of the paid-item set: sorted, de-duplicated, comma-joined
-    (Stage 2c). '' means a whole-order/amount-only intent."""
-    return ",".join(str(i) for i in sorted({int(i) for i in (item_ids or [])}))
+    (Stage 2c). '' means a whole-order/amount-only intent. Malformed ids raise an
+    explicit domain error rather than silently coercing (slice-1 review #4)."""
+    try:
+        ids = sorted({int(i) for i in (item_ids or [])})
+    except (TypeError, ValueError) as exc:
+        raise PaymentAttemptError(f"invalid item id in selection: {exc}") from exc
+    return ",".join(str(i) for i in ids)
 
 
 def intent_fingerprint(
@@ -176,19 +181,22 @@ def create_attempt(
     discount_cents: int = 0,
     surcharge_cents: int = 0,
     currency: str | None = None,
-    line_selection: str = "",
+    item_ids=None,
     idempotency_key: str | None = None,
 ) -> PaymentAttempt:
     """Persist a CREATED attempt from an already-locked payable snapshot. Commits.
 
     ``currency`` defaults to the venue currency (``config.venue_currency()``) when
     omitted — never a hard-coded CAD — so a USD venue does not silently create a
-    CAD intent (#3). Idempotent and concurrency-safe: a repeated key returns the
-    existing attempt; a repeated key with a different intent raises
+    CAD intent (#3). ``item_ids`` (the paid selection) is canonicalized *here*, so
+    the paid-item identity is owned by the attempt service, not the caller
+    (slice-1 review #4). Idempotent and concurrency-safe: a repeated key returns
+    the existing attempt; a repeated key with a different intent raises
     ``IdempotencyConflict``; an unregistered provider is rejected.
     """
     _validate_provider(provider)
     currency = (currency or venue_currency()).upper()
+    line_selection = canonical_selection(item_ids)
     if expected_total_cents < 0:
         raise PaymentAttemptError("expected_total_cents cannot be negative.")
 
diff --git a/app/services/settlement.py b/app/services/settlement.py
index d2e730b..ab27507 100644
--- a/app/services/settlement.py
+++ b/app/services/settlement.py
@@ -1,26 +1,26 @@
-"""Charge settlement (Stage 2c, slice 1).
+"""Charge settlement (Stage 2c, slice 1 — corrected per slice-1 deep review).
 
 Turns a PROCESSOR_APPROVED PaymentAttempt into exactly one local Payment, under
-two guardrails the reviewer requires before live wiring:
-
-* **Amount/currency invariant (guardrail #3).** For an external provider, the
-  processor-confirmed pre-tip base and currency must match the attempt's
-  immutable snapshot (``expected_total_cents`` / ``currency``). A mismatch does
-  NOT settle — the attempt is parked in REQUIRES_RECONCILIATION and no Payment is
-  written. The order/payment is never silently adjusted to match the processor.
-
-* **Idempotent local ledger (guardrail #6).** At most one Payment per attempt: a
-  settled attempt already carries ``payment_id`` (write-once, unique), so a retry
-  after a processor-success + local-failure converges on the existing Payment
-  instead of creating a second one.
-
-This module is provider-neutral and does not itself build a Payment — the caller
-passes a ``payment_factory`` that performs the venue's real Payment creation
-(``pay_seat`` etc.). Concurrency is serialized by the caller's order-row
-``SELECT ... FOR UPDATE`` (slice 2); this service adds the attempt-level guards.
+the reviewer's guardrails:
+
+* **Amount/currency invariant (#3).** For an external provider, the processor
+  pre-tip base and currency must equal the attempt snapshot. A mismatch parks the
+  attempt in REQUIRES_RECONCILIATION and writes NO Payment.
+* **Idempotent local ledger (#6).** At most one Payment per attempt (write-once,
+  unique ``payment_id``); a retry converges on the existing Payment.
+
+**Transaction contract (slice-1 review #1, #3).** ``settle_charge`` never uses an
+exception to control the transaction: a mismatch is reported as a structured
+``SettlementResult`` so the *caller* commits the reconciliation transition
+intentionally (in slice 2 the live route owns the order-row lock and a single
+commit). ``payment_factory`` MUST use the same Session and MUST NOT commit or
+rollback — it mutates + flushes only, so a lost CAS race rolls the Payment back
+with the attempt transition. (Slice 2 refactors ``pay_seat`` into a no-commit
+core to honour this.)
 """
 from __future__ import annotations
 
+from dataclasses import dataclass
 from typing import Callable
 
 from sqlalchemy.orm import Session
@@ -29,9 +29,22 @@ from app.config import venue_currency
 from app.models.oltp import Payment, PaymentAttempt, PaymentAttemptStatus
 from app.services import payment_attempts as pa
 
+SETTLED = "settled"
+RECONCILED = "reconciled"
 
-class SettlementMismatch(pa.PaymentAttemptError):
-    """Processor evidence disagrees with the attempt's snapshot — do not settle."""
+
+@dataclass
+class SettlementResult:
+    """Outcome of a settlement attempt. ``settled`` carries the one Payment;
+    ``reconciled`` means processor evidence disagreed and the attempt was parked
+    (no Payment). No exception is used to drive the caller's transaction."""
+    status: str
+    payment: Payment | None = None
+    reason: str = ""
+
+    @property
+    def is_settled(self) -> bool:
+        return self.status == SETTLED
 
 
 def _mismatch_reason(attempt: PaymentAttempt) -> str | None:
@@ -58,16 +71,16 @@ def settle_charge(
     *,
     payment_factory: Callable[[], Payment],
     commit: bool = True,
-) -> Payment:
+) -> SettlementResult:
     """Settle an approved attempt into exactly one Payment. Idempotent.
 
-    Returns the existing Payment on a retry. Raises ``SettlementMismatch`` (after
-    parking the attempt in REQUIRES_RECONCILIATION) when processor evidence does
-    not match the snapshot — no Payment is created in that case.
+    Returns a ``SettlementResult`` — never raises to signal a mismatch, so the
+    caller's transaction (which owns the reconciliation transition) is committed
+    intentionally rather than rolled back by an escaping exception.
     """
     # Idempotent: already settled -> return the one Payment, create nothing.
     if attempt.payment_id is not None:
-        return db.get(Payment, attempt.payment_id)
+        return SettlementResult(SETTLED, db.get(Payment, attempt.payment_id))
 
     if attempt.status != PaymentAttemptStatus.PROCESSOR_APPROVED:
         raise pa.PaymentAttemptError(
@@ -76,20 +89,27 @@ def settle_charge(
 
     reason = _mismatch_reason(attempt)
     if reason is not None:
-        # Do not settle a disagreeing charge — park it, write no Payment.
+        # Park for reconciliation (durably, via the caller's commit) and write no
+        # Payment. Returning — not raising — is what makes the reconciliation
+        # survive the caller's outer transaction (slice-1 review #1).
         pa.transition(db, attempt, PaymentAttemptStatus.REQUIRES_RECONCILIATION,
                       last_error=f"settlement mismatch: {reason}", commit=commit)
-        raise SettlementMismatch(reason)
+        return SettlementResult(RECONCILED, None, reason)
 
     payment = payment_factory()
-    db.flush()  # assign payment.id
+    db.flush()  # assign payment.id; never commit here — the caller owns the tx
     try:
         pa.transition(db, attempt, PaymentAttemptStatus.SETTLED,
                       payment_id=payment.id, commit=commit)
     except pa.TransitionConflict:
-        # A concurrent settle won (same order lock normally prevents this). Roll
-        # back our Payment and converge on the winner's.
+        # A CAS loss is NOT automatically "someone settled". Roll our Payment back
+        # and converge ONLY on a proven winner: the DB truth must show SETTLED +
+        # an existing payment_id. Anything else (moved to reconciliation, etc.) is
+        # a real conflict and is re-raised (slice-1 review #2).
         db.rollback()
         db.refresh(attempt)
-        return db.get(Payment, attempt.payment_id)
-    return payment
+        winner = db.get(Payment, attempt.payment_id) if attempt.payment_id is not None else None
+        if attempt.status == PaymentAttemptStatus.SETTLED and winner is not None:
+            return SettlementResult(SETTLED, winner)
+        raise
+    return SettlementResult(SETTLED, payment)
diff --git a/tests/test_pg_concurrency.py b/tests/test_pg_concurrency.py
index 56cae67..2afaffd 100644
--- a/tests/test_pg_concurrency.py
+++ b/tests/test_pg_concurrency.py
@@ -17,9 +17,10 @@ sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
 
 from tests._pay_fixture import Session, fresh_schema, make_engine, pg_dsn, seed_parents
 
-from app.models.oltp import PaymentAttempt, PaymentAttemptStatus as S, RefundAttempt
+from app.models.oltp import Payment, PaymentAttempt, PaymentAttemptStatus as S, RefundAttempt
 from app.services import payment_attempts as pa
 from app.services import refund_attempts as ra
+from app.services import settlement as settle
 
 _failures = []
 
@@ -168,6 +169,59 @@ def test_concurrent_refund_reconciliation(engine, ids):
     check(len(refusals) == 5, "the losers refuse with an explicit typed error (#1)")
 
 
+def _approved_ext(db, ids, key):
+    a = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=1000,
+                          subtotal_cents=1000, currency="CAD", idempotency_key=key)
+    pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id="chk_" + key)
+    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="pay_" + key,
+                  processor_amount_cents=1000, processor_currency="CAD")
+    return a
+
+
+def _pay_factory(db, ids):
+    def make():
+        p = Payment(order_id=ids["order_id"], instrument_id=ids["instrument_id"],
+                    staff_id=ids["staff_id"], total_cents=1000)
+        db.add(p)
+        return p
+    return make
+
+
+def test_settlement_cas_converges_on_winner(engine, ids):
+    """A CAS-losing settle converges ONLY on a real SETTLED winner (#2)."""
+    Sess = Session(engine)
+    sa, sb = Sess(), Sess()
+    a = _approved_ext(sa, ids, "cw")
+    aid = a.id
+    b = sb.get(PaymentAttempt, aid)                 # B loads the APPROVED attempt
+    pay_a = settle.settle_charge(sa, a, payment_factory=_pay_factory(sa, ids)).payment
+    sa.commit()                                     # A settles first
+    res_b = settle.settle_charge(sb, b, payment_factory=_pay_factory(sb, ids))
+    check(res_b.is_settled and res_b.payment.id == pay_a.id,
+          "CAS loser converges on the real winner's Payment (#2)")
+    sa.close(); sb.close()
+
+
+def test_settlement_cas_reraises_on_non_winner(engine, ids):
+    """A CAS loss to a NON-settled state (e.g. reconciliation) re-raises rather
+    than reporting false success (#2)."""
+    Sess = Session(engine)
+    sa, sb = Sess(), Sess()
+    a = _approved_ext(sa, ids, "cr")
+    aid = a.id
+    b = sb.get(PaymentAttempt, aid)
+    pa.transition(sa, a, S.REQUIRES_RECONCILIATION, last_error="lost")
+    sa.commit()                                     # A parks it, does not settle
+    raised = False
+    try:
+        settle.settle_charge(sb, b, payment_factory=_pay_factory(sb, ids))
+    except pa.TransitionConflict:
+        raised = True
+    check(raised, "CAS loss to a non-settled state re-raises, not false success (#2)")
+    sa.close(); sb.close()
+
+
 def test_provider_payment_id_unique(engine, ids):
     Sess = Session(engine)
     db = Sess()
@@ -239,6 +293,8 @@ if __name__ == "__main__":
         test_provider_payment_id_unique,
         test_fk_enforced,
         test_one_settlement_per_attempt,
+        test_settlement_cas_converges_on_winner,
+        test_settlement_cas_reraises_on_non_winner,
     ]
     for fn in tests:
         print(f"- {fn.__name__}")
diff --git a/tests/test_settlement.py b/tests/test_settlement.py
index da9ccb4..b4bf1e8 100644
--- a/tests/test_settlement.py
+++ b/tests/test_settlement.py
@@ -1,15 +1,19 @@
-"""Stage 2c slice 1 — charge settlement service (amount/currency invariant +
-idempotent local Payment). Runs on SQLite-with-FK by default, Postgres if
-PG_TEST_DSN is set. Run: python tests/test_settlement.py
+"""Stage 2c slice 1 (corrected) — charge settlement service.
+
+Amount/currency invariant + idempotent Payment + structured mismatch outcome +
+service-owned selection canonicalization. Runs on SQLite-with-FK by default,
+Postgres if PG_TEST_DSN is set. Run: python tests/test_settlement.py
 """
 import os
 import sys
 
 sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
 
-from tests._pay_fixture import new_db as _db
+from tests._pay_fixture import (
+    Session, _state as _fx_state, fresh_schema, make_engine, new_db as _db, seed_parents,
+)
 
-from app.models.oltp import Payment, PaymentAttemptStatus as S
+from app.models.oltp import Payment, PaymentAttempt, PaymentAttemptStatus as S
 from app.services import payment_attempts as pa
 from app.services import settlement as settle
 
@@ -22,12 +26,32 @@ def check(cond, label):
         _failures.append(label)
 
 
-def _approved_ext(db, ids, *, expected=1000, pamt=None, pcur="CAD", key=None, selection=""):
+def _fresh_sessions():
+    # Tear down any open new_db() session/engine first, so drop_all on a shared
+    # Postgres database is not blocked by its still-held table locks.
+    if _fx_state["session"] is not None:
+        try:
+            _fx_state["session"].close()
+        except Exception:
+            pass
+    if _fx_state["engine"] is not None:
+        _fx_state["engine"].dispose()
+    _fx_state["session"] = _fx_state["engine"] = None
+    engine, _ = make_engine()
+    fresh_schema(engine)
+    SM = Session(engine)
+    s = SM()
+    ids = seed_parents(s)
+    s.close()
+    return SM, ids
+
+
+def _approved_ext(db, ids, *, expected=1000, pamt=None, pcur="CAD", key=None, item_ids=None):
     pamt = expected if pamt is None else pamt
     a = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
                           staff_id=ids["staff_id"], expected_total_cents=expected,
                           subtotal_cents=expected, currency="CAD",
-                          line_selection=selection, idempotency_key=key)
+                          item_ids=item_ids, idempotency_key=key)
     pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id="chk")
     pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="pay",
                   processor_amount_cents=pamt, processor_currency=pcur)
@@ -47,48 +71,56 @@ def test_external_settlement_amount_match():
     db, ids = _db()
     a = _approved_ext(db, ids, expected=1000)
     n0 = db.query(Payment).count()
-    pay = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
-    check(a.status == S.SETTLED, "matching evidence settles the attempt")
-    check(a.payment_id == pay.id, "attempt links its one Payment")
+    res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
+    check(res.is_settled, "matching evidence settles")
+    check(a.status == S.SETTLED and a.payment_id == res.payment.id, "attempt links its one Payment")
     check(db.query(Payment).count() == n0 + 1, "exactly one new Payment created")
 
 
 def test_settlement_is_idempotent():
     db, ids = _db()
     a = _approved_ext(db, ids, expected=1000)
-    pay = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
+    r1 = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
     n = db.query(Payment).count()
-    pay2 = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
-    check(pay2.id == pay.id, "retry returns the same Payment")
+    r2 = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
+    check(r2.is_settled and r2.payment.id == r1.payment.id, "retry returns the same Payment")
     check(db.query(Payment).count() == n, "retry creates no duplicate Payment (#6)")
 
 
 def test_amount_mismatch_reconciles_no_payment():
     db, ids = _db()
-    a = _approved_ext(db, ids, expected=1000, pamt=9999)  # processor charged a different base
+    a = _approved_ext(db, ids, expected=1000, pamt=9999)
     n0 = db.query(Payment).count()
-    raised = False
-    try:
-        settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
-    except settle.SettlementMismatch:
-        raised = True
-    check(raised, "amount mismatch raises SettlementMismatch (#3)")
+    res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
+    check(not res.is_settled, "amount mismatch returns a non-settled result (no exception, #3)")
     check(a.status == S.REQUIRES_RECONCILIATION, "mismatch parks the attempt for reconciliation")
     check(db.query(Payment).count() == n0, "no Payment written on mismatch")
 
 
 def test_currency_mismatch_reconciles_no_payment():
     db, ids = _db()
-    a = _approved_ext(db, ids, expected=1000, pcur="USD")  # attempt currency is CAD
+    a = _approved_ext(db, ids, expected=1000, pcur="USD")
     n0 = db.query(Payment).count()
-    raised = False
-    try:
-        settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
-    except settle.SettlementMismatch:
-        raised = True
-    check(raised, "currency mismatch raises SettlementMismatch (#3)")
-    check(a.status == S.REQUIRES_RECONCILIATION and db.query(Payment).count() == n0,
-          "no Payment; parked for reconciliation")
+    res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
+    check(not res.is_settled and a.status == S.REQUIRES_RECONCILIATION and db.query(Payment).count() == n0,
+          "currency mismatch: no Payment; parked for reconciliation (#3)")
+
+
+def test_mismatch_reconciliation_durable_across_outer_tx():
+    """Slice-1 review #1: with commit=False inside the caller's transaction, a
+    mismatch must survive the caller's commit (no exception rolls it back)."""
+    SM, ids = _fresh_sessions()
+    db = SM()
+    a = _approved_ext(db, ids, expected=1000, pamt=9999)
+    aid = a.id
+    res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000), commit=False)
+    check(not res.is_settled, "mismatch reported as result, not exception (#1)")
+    db.commit()          # caller commits the transaction incl. the reconciliation transition
+    db.close()
+    db2 = SM()
+    fresh = db2.get(PaymentAttempt, aid)
+    check(fresh.status == S.REQUIRES_RECONCILIATION, "reconciliation durably committed (#1)")
+    db2.close()
 
 
 def test_manual_settles_without_evidence():
@@ -97,27 +129,40 @@ def test_manual_settles_without_evidence():
                           staff_id=ids["staff_id"], expected_total_cents=500,
                           subtotal_cents=500, idempotency_key="m")
     pa.transition(db, a, S.PROCESSOR_PENDING)
-    pa.transition(db, a, S.PROCESSOR_APPROVED)  # manual: no external evidence
-    pay = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 500))
-    check(a.status == S.SETTLED and a.payment_id == pay.id,
-          "manual provider settles with no processor evidence")
+    pa.transition(db, a, S.PROCESSOR_APPROVED)
+    res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 500))
+    check(res.is_settled and a.status == S.SETTLED, "manual provider settles with no evidence")
 
 
-def test_selection_is_part_of_the_fingerprint():
+def test_selection_canonicalized_by_service():
+    """Slice-1 review #4: the attempt service owns canonicalization."""
     db, ids = _db()
-    k = "sel"
     a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                           staff_id=ids["staff_id"], expected_total_cents=1000,
-                          line_selection=pa.canonical_selection([3, 1, 2]), idempotency_key=k)
+                          item_ids=[3, 2, 1], idempotency_key="k")
     b = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                           staff_id=ids["staff_id"], expected_total_cents=1000,
-                          line_selection=pa.canonical_selection([1, 2, 3]), idempotency_key=k)
-    check(a.id == b.id, "same key + order-independent same selection -> same attempt")
+                          item_ids=[1, 1, 2, 3], idempotency_key="k")
+    check(a.id == b.id and a.line_selection == "1,2,3",
+          "unordered/duplicate item_ids canonicalize identically in the service (#4)")
     raised = False
     try:
         pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                           staff_id=ids["staff_id"], expected_total_cents=1000,
-                          line_selection=pa.canonical_selection([1, 2, 4]), idempotency_key=k)
+                          item_ids=["not-an-int"], idempotency_key="k2")
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "malformed item id raises an explicit domain error (#4)")
+
+
+def test_different_selection_conflicts():
+    db, ids = _db()
+    pa.create_attempt(db, provider="manual", order_id=ids["order_id"], staff_id=ids["staff_id"],
+                      expected_total_cents=1000, item_ids=[1, 2, 3], idempotency_key="s")
+    raised = False
+    try:
+        pa.create_attempt(db, provider="manual", order_id=ids["order_id"], staff_id=ids["staff_id"],
+                          expected_total_cents=1000, item_ids=[1, 2, 4], idempotency_key="s")
     except pa.IdempotencyConflict:
         raised = True
     check(raised, "same key + different paid-item selection -> IdempotencyConflict (#3)")
@@ -129,8 +174,10 @@ if __name__ == "__main__":
         test_settlement_is_idempotent,
         test_amount_mismatch_reconciles_no_payment,
         test_currency_mismatch_reconciles_no_payment,
+        test_mismatch_reconciliation_durable_across_outer_tx,
         test_manual_settles_without_evidence,
-        test_selection_is_part_of_the_fingerprint,
+        test_selection_canonicalized_by_service,
+        test_different_selection_conflicts,
     ):
         print(f"- {fn.__name__}")
         fn()
```
