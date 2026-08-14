# Stage 2c — Slice 1 Checkpoint (Charge Settlement Service)

**For:** external reviewer (ChatGPT).
**This is a slice checkpoint, not the full Stage 2c handoff.** We are wiring
Stage 2c **slice by slice** with a review between each. Slice 1 builds the
charge-settlement **service layer only** — **no live money route is wired yet**.
The consolidated `REVIEW_HANDOFF_STAGE_2C.md` will follow once all slices (charge
route, refund settlement, refund route, failure-injection) are done.

## Commit range
- **main (pre-remediation) SHA:** `07ee4f1af26aa383b7de06377f18caa98b153e4b`
- **Handoff #7 (GO) HEAD:** `2c2e6768f4970030b65d4319c10e15d00f308d2c`
- **HEAD (this slice):** `50f687a5c1f69a7e3d31e35f5d2c450e1aaa3ca9`
- Branch: `fix/p0-security-and-payments`
- **Commit added:** `50f687a feat(2c slice 1): charge settlement service + paid-item selection on the attempt`

## `git status --short`
No uncommitted **source** changes. Untracked non-source: `REVIEW_HANDOFF*.md`,
business docs/decks, `.github/`, `restaurant-app-review.zip`.

## Scope of this slice
Service layer for turning an approved `PaymentAttempt` into exactly one local
`Payment`, honouring the two guardrails the GO review required for settlement:
amount/currency invariant (#3) and idempotent local ledger (#6). It is
consumed by nobody yet — the live routes still use the old path (unchanged).

## Schema change
`payment_attempt.line_selection VARCHAR(500) NOT NULL DEFAULT ''` — the canonical
paid-item selection. Additive migration entry (`ADDED_COLUMNS`); fresh DBs get it
from `create_all`. Verified applied on a Postgres upgrade run (existing migration
suite still green).

## Paid-item selection identity (reviewer's #3 note)
`canonical_selection(item_ids)` → sorted, de-duplicated, comma-joined string
(order-independent; `''` = amount-only intent). Stored on the attempt and folded
into `intent_fingerprint(...)`. Reusing an idempotency key with a **different**
selection is an `IdempotencyConflict`; the same selection in a different order is
the same intent.

## Settlement service contract (`app/services/settlement.py`)
`settle_charge(db, attempt, *, payment_factory, commit=True) -> Payment`:

1. **Idempotent (#6):** if `attempt.payment_id` is already set, return that
   Payment and create nothing (retry after processor-success + local-failure
   converges on the one Payment). `payment_id` is write-once + unique.
2. Requires `attempt.status == PROCESSOR_APPROVED`.
3. **Amount/currency invariant (#3):** for an **external** provider
   (`get_provider(attempt.provider).is_external`), the processor evidence must
   match the immutable snapshot:
   - `processor_currency (upper) == attempt.currency (upper)`;
   - `processor_amount_cents == attempt.expected_total_cents`
     (both pre-tip base — the documented contract).
   On mismatch: transition the attempt to `REQUIRES_RECONCILIATION` with the
   reason and **raise `SettlementMismatch` — no Payment is written**. The order/
   payment is never adjusted to match the processor.
   Manual/local providers have no external evidence and settle directly.
4. Creates the Payment via a caller-supplied `payment_factory` (in slice 2 this
   delegates to the venue's real `pay_seat`), then transitions `SETTLED` with the
   new `payment_id`. A concurrent double-settle (should be prevented by the
   caller's order-row lock in slice 2) is caught as `TransitionConflict` and
   converges on the winner's Payment.

## Tests run — commands, counts (0 failures)
Env: Python 3.14, SQLAlchemy 2.0.51, psycopg 3.3.4 (test-only), Postgres 16 @ :5433.
```
# SQLite (default)
test_settlement          13 ok
test_config / test_payment_attempts / test_refund_attempts / test_payment_providers  PASS
test_pg_concurrency / test_pg_migration  SKIP (no PG_TEST_DSN)
test_templates/security/admin/money/reconciliation/schedule  PASS

# PostgreSQL (PG_TEST_DSN set)
test_settlement          13 ok
test_payment_attempts / test_refund_attempts / test_pg_concurrency / test_pg_migration  PASS
```
Full suite green on both engines; the pre-existing migration + concurrency suites
remain green (no regression from the new column).

### Slice-1 acceptance coverage
- external amount match → settles; exactly one Payment; attempt links it;
- retry → same Payment, no duplicate (#6);
- amount mismatch → `SettlementMismatch`, attempt reconciled, **no Payment** (#3);
- currency mismatch → `SettlementMismatch`, **no Payment** (#3);
- manual provider settles with no processor evidence;
- selection folded into fingerprint (order-independent equal; different →
  `IdempotencyConflict`).

## Explicitly NOT in this slice (deferred to later slices)
- **Live route wiring** (`pay.py` terminal + cash/manual through the attempt +
  settlement) → slice 2. The concurrent-settlement PG proof lives there, where the
  order-row `SELECT … FOR UPDATE` exists.
- **Refund settlement service** + refundable-balance concurrency (over-refund
  prevention) → slice 3.
- **Refund route wiring / void** → slice 4.
- **Full failure-injection matrix + the 25 acceptance tests** from the GO review
  → assembled across slices, consolidated in `REVIEW_HANDOFF_STAGE_2C.md`.

## Areas I am least confident about
1. **`payment_factory` seam.** The service delegates real Payment creation to a
   callback so it stays provider-neutral and testable. Whether `pay_seat` (the
   venue's creator) is fully idempotent *by itself* under the attempt guard is a
   slice-2 concern — the attempt's write-once `payment_id` is the idempotency
   anchor, but `pay_seat` also mutates allocations/seat status, which slice 2 must
   run exactly once under the order lock.
2. **Amount comparison is exact equality** on the pre-tip base. If a real Square
   sandbox ever returns a base that legitimately differs (rounding, service-charge
   handling), this would over-reconcile. To be validated against live sandbox data
   before slice 2 trusts it for auto-settlement.
3. **`line_selection` length** is capped at 500 chars; a very large multi-item
   settle could exceed it. Realistic seat/item counts are far below this, but it's
   a hard cap worth noting.
4. Concurrency of settlement is **not** proven in this slice (no lock here) — it
   is deferred to slice 2 by design.

---
## Slice diff (`git diff 2c2e676..HEAD`)
```diff
diff --git a/app/migrate.py b/app/migrate.py
index ce63f03..fe6b19e 100644
--- a/app/migrate.py
+++ b/app/migrate.py
@@ -96,6 +96,8 @@ ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
     ("payment_attempt", "reconciled_by", "VARCHAR(60) NOT NULL DEFAULT ''"),
     ("payment_attempt", "reconciliation_note", "VARCHAR(300) NOT NULL DEFAULT ''"),
     ("refund_attempt", "intent_fingerprint", "VARCHAR(64) NOT NULL DEFAULT ''"),
+    # Stage 2c: the paid-item selection captured on the charge attempt.
+    ("payment_attempt", "line_selection", "VARCHAR(500) NOT NULL DEFAULT ''"),
 )
 
 # (table, column, min_length, new DDL type). Columns whose type/length GREW
diff --git a/app/models/oltp.py b/app/models/oltp.py
index 6f06e92..fd8b46f 100644
--- a/app/models/oltp.py
+++ b/app/models/oltp.py
@@ -1063,9 +1063,13 @@ class PaymentAttempt(Base):
     # Client-generated idempotency key sent to the processor; also our dedupe key.
     idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
     # Fingerprint of the immutable intent behind idempotency_key (finding #3), so
-    # reusing a key with different order/amount/currency is a conflict, not a
-    # silent wrong-attempt hit.
+    # reusing a key with different order/amount/currency/selection is a conflict,
+    # not a silent wrong-attempt hit.
     intent_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
+    # Canonical identity of WHAT is being paid — the sorted set of OrderItem ids
+    # this attempt settles (Stage 2c). Part of the intent fingerprint, and what
+    # settlement reconciles against.
+    line_selection: Mapped[str] = mapped_column(String(500), nullable=False, default="")
 
     # Immutable payable snapshot (integer cents), locked before creation.
     subtotal_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
diff --git a/app/services/payment_attempts.py b/app/services/payment_attempts.py
index e06ce5b..772ab18 100644
--- a/app/services/payment_attempts.py
+++ b/app/services/payment_attempts.py
@@ -77,6 +77,12 @@ def new_idempotency_key() -> str:
     return secrets.token_hex(24)
 
 
+def canonical_selection(item_ids) -> str:
+    """Stable identity of the paid-item set: sorted, de-duplicated, comma-joined
+    (Stage 2c). '' means a whole-order/amount-only intent."""
+    return ",".join(str(i) for i in sorted({int(i) for i in (item_ids or [])}))
+
+
 def intent_fingerprint(
     *,
     provider: str,
@@ -91,13 +97,15 @@ def intent_fingerprint(
     service_charge_cents: int,
     discount_cents: int,
     surcharge_cents: int,
+    line_selection: str = "",
 ) -> str:
     """Stable hash of the immutable intent behind an idempotency key. Reusing a
-    key with a different fingerprint is rejected as a conflict."""
+    key with a different fingerprint — including a different paid-item selection —
+    is rejected as a conflict."""
     canonical = "|".join(str(x) for x in (
         provider, order_id, seat_id, staff_id, currency.upper(),
         expected_total_cents, subtotal_cents, tax_cents, tip_cents,
-        service_charge_cents, discount_cents, surcharge_cents,
+        service_charge_cents, discount_cents, surcharge_cents, line_selection,
     ))
     return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:64]
 
@@ -168,6 +176,7 @@ def create_attempt(
     discount_cents: int = 0,
     surcharge_cents: int = 0,
     currency: str | None = None,
+    line_selection: str = "",
     idempotency_key: str | None = None,
 ) -> PaymentAttempt:
     """Persist a CREATED attempt from an already-locked payable snapshot. Commits.
@@ -188,7 +197,7 @@ def create_attempt(
         currency=currency, expected_total_cents=expected_total_cents,
         subtotal_cents=subtotal_cents, tax_cents=tax_cents, tip_cents=tip_cents,
         service_charge_cents=service_charge_cents, discount_cents=discount_cents,
-        surcharge_cents=surcharge_cents,
+        surcharge_cents=surcharge_cents, line_selection=line_selection,
     )
 
     if idempotency_key:
@@ -202,6 +211,7 @@ def create_attempt(
     attempt = PaymentAttempt(
         order_id=order_id, seat_id=seat_id, staff_id=staff_id, provider=provider,
         idempotency_key=idempotency_key, intent_fingerprint=fingerprint,
+        line_selection=line_selection,
         subtotal_cents=subtotal_cents, tax_cents=tax_cents, tip_cents=tip_cents,
         service_charge_cents=service_charge_cents, discount_cents=discount_cents,
         surcharge_cents=surcharge_cents, expected_total_cents=expected_total_cents,
diff --git a/app/services/settlement.py b/app/services/settlement.py
new file mode 100644
index 0000000..d2e730b
--- /dev/null
+++ b/app/services/settlement.py
@@ -0,0 +1,95 @@
+"""Charge settlement (Stage 2c, slice 1).
+
+Turns a PROCESSOR_APPROVED PaymentAttempt into exactly one local Payment, under
+two guardrails the reviewer requires before live wiring:
+
+* **Amount/currency invariant (guardrail #3).** For an external provider, the
+  processor-confirmed pre-tip base and currency must match the attempt's
+  immutable snapshot (``expected_total_cents`` / ``currency``). A mismatch does
+  NOT settle — the attempt is parked in REQUIRES_RECONCILIATION and no Payment is
+  written. The order/payment is never silently adjusted to match the processor.
+
+* **Idempotent local ledger (guardrail #6).** At most one Payment per attempt: a
+  settled attempt already carries ``payment_id`` (write-once, unique), so a retry
+  after a processor-success + local-failure converges on the existing Payment
+  instead of creating a second one.
+
+This module is provider-neutral and does not itself build a Payment — the caller
+passes a ``payment_factory`` that performs the venue's real Payment creation
+(``pay_seat`` etc.). Concurrency is serialized by the caller's order-row
+``SELECT ... FOR UPDATE`` (slice 2); this service adds the attempt-level guards.
+"""
+from __future__ import annotations
+
+from typing import Callable
+
+from sqlalchemy.orm import Session
+
+from app.config import venue_currency
+from app.models.oltp import Payment, PaymentAttempt, PaymentAttemptStatus
+from app.services import payment_attempts as pa
+
+
+class SettlementMismatch(pa.PaymentAttemptError):
+    """Processor evidence disagrees with the attempt's snapshot — do not settle."""
+
+
+def _mismatch_reason(attempt: PaymentAttempt) -> str | None:
+    """Why an external attempt must not settle, or None if it may. Manual/local
+    providers have no external evidence to reconcile against."""
+    from app.services.payment_providers import get_provider
+    if not get_provider(attempt.provider).is_external:
+        return None
+    want_cur = (attempt.currency or venue_currency()).upper()
+    got_cur = (attempt.processor_currency or "").upper()
+    if got_cur != want_cur:
+        return f"currency {got_cur or '<none>'} != expected {want_cur}"
+    if attempt.processor_amount_cents is None:
+        return "no processor amount recorded"
+    if attempt.processor_amount_cents != attempt.expected_total_cents:
+        return (f"processor base {attempt.processor_amount_cents} != expected "
+                f"{attempt.expected_total_cents}")
+    return None
+
+
+def settle_charge(
+    db: Session,
+    attempt: PaymentAttempt,
+    *,
+    payment_factory: Callable[[], Payment],
+    commit: bool = True,
+) -> Payment:
+    """Settle an approved attempt into exactly one Payment. Idempotent.
+
+    Returns the existing Payment on a retry. Raises ``SettlementMismatch`` (after
+    parking the attempt in REQUIRES_RECONCILIATION) when processor evidence does
+    not match the snapshot — no Payment is created in that case.
+    """
+    # Idempotent: already settled -> return the one Payment, create nothing.
+    if attempt.payment_id is not None:
+        return db.get(Payment, attempt.payment_id)
+
+    if attempt.status != PaymentAttemptStatus.PROCESSOR_APPROVED:
+        raise pa.PaymentAttemptError(
+            f"cannot settle an attempt in status {attempt.status!r}; "
+            "only a PROCESSOR_APPROVED attempt settles.")
+
+    reason = _mismatch_reason(attempt)
+    if reason is not None:
+        # Do not settle a disagreeing charge — park it, write no Payment.
+        pa.transition(db, attempt, PaymentAttemptStatus.REQUIRES_RECONCILIATION,
+                      last_error=f"settlement mismatch: {reason}", commit=commit)
+        raise SettlementMismatch(reason)
+
+    payment = payment_factory()
+    db.flush()  # assign payment.id
+    try:
+        pa.transition(db, attempt, PaymentAttemptStatus.SETTLED,
+                      payment_id=payment.id, commit=commit)
+    except pa.TransitionConflict:
+        # A concurrent settle won (same order lock normally prevents this). Roll
+        # back our Payment and converge on the winner's.
+        db.rollback()
+        db.refresh(attempt)
+        return db.get(Payment, attempt.payment_id)
+    return payment
diff --git a/tests/test_settlement.py b/tests/test_settlement.py
new file mode 100644
index 0000000..da9ccb4
--- /dev/null
+++ b/tests/test_settlement.py
@@ -0,0 +1,140 @@
+"""Stage 2c slice 1 — charge settlement service (amount/currency invariant +
+idempotent local Payment). Runs on SQLite-with-FK by default, Postgres if
+PG_TEST_DSN is set. Run: python tests/test_settlement.py
+"""
+import os
+import sys
+
+sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
+
+from tests._pay_fixture import new_db as _db
+
+from app.models.oltp import Payment, PaymentAttemptStatus as S
+from app.services import payment_attempts as pa
+from app.services import settlement as settle
+
+_failures = []
+
+
+def check(cond, label):
+    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
+    if not cond:
+        _failures.append(label)
+
+
+def _approved_ext(db, ids, *, expected=1000, pamt=None, pcur="CAD", key=None, selection=""):
+    pamt = expected if pamt is None else pamt
+    a = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=expected,
+                          subtotal_cents=expected, currency="CAD",
+                          line_selection=selection, idempotency_key=key)
+    pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id="chk")
+    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="pay",
+                  processor_amount_cents=pamt, processor_currency=pcur)
+    return a
+
+
+def _factory(db, ids, amount):
+    def make():
+        p = Payment(order_id=ids["order_id"], instrument_id=ids["instrument_id"],
+                    staff_id=ids["staff_id"], total_cents=amount)
+        db.add(p)
+        return p
+    return make
+
+
+def test_external_settlement_amount_match():
+    db, ids = _db()
+    a = _approved_ext(db, ids, expected=1000)
+    n0 = db.query(Payment).count()
+    pay = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
+    check(a.status == S.SETTLED, "matching evidence settles the attempt")
+    check(a.payment_id == pay.id, "attempt links its one Payment")
+    check(db.query(Payment).count() == n0 + 1, "exactly one new Payment created")
+
+
+def test_settlement_is_idempotent():
+    db, ids = _db()
+    a = _approved_ext(db, ids, expected=1000)
+    pay = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
+    n = db.query(Payment).count()
+    pay2 = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
+    check(pay2.id == pay.id, "retry returns the same Payment")
+    check(db.query(Payment).count() == n, "retry creates no duplicate Payment (#6)")
+
+
+def test_amount_mismatch_reconciles_no_payment():
+    db, ids = _db()
+    a = _approved_ext(db, ids, expected=1000, pamt=9999)  # processor charged a different base
+    n0 = db.query(Payment).count()
+    raised = False
+    try:
+        settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
+    except settle.SettlementMismatch:
+        raised = True
+    check(raised, "amount mismatch raises SettlementMismatch (#3)")
+    check(a.status == S.REQUIRES_RECONCILIATION, "mismatch parks the attempt for reconciliation")
+    check(db.query(Payment).count() == n0, "no Payment written on mismatch")
+
+
+def test_currency_mismatch_reconciles_no_payment():
+    db, ids = _db()
+    a = _approved_ext(db, ids, expected=1000, pcur="USD")  # attempt currency is CAD
+    n0 = db.query(Payment).count()
+    raised = False
+    try:
+        settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
+    except settle.SettlementMismatch:
+        raised = True
+    check(raised, "currency mismatch raises SettlementMismatch (#3)")
+    check(a.status == S.REQUIRES_RECONCILIATION and db.query(Payment).count() == n0,
+          "no Payment; parked for reconciliation")
+
+
+def test_manual_settles_without_evidence():
+    db, ids = _db()
+    a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=500,
+                          subtotal_cents=500, idempotency_key="m")
+    pa.transition(db, a, S.PROCESSOR_PENDING)
+    pa.transition(db, a, S.PROCESSOR_APPROVED)  # manual: no external evidence
+    pay = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 500))
+    check(a.status == S.SETTLED and a.payment_id == pay.id,
+          "manual provider settles with no processor evidence")
+
+
+def test_selection_is_part_of_the_fingerprint():
+    db, ids = _db()
+    k = "sel"
+    a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=1000,
+                          line_selection=pa.canonical_selection([3, 1, 2]), idempotency_key=k)
+    b = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=1000,
+                          line_selection=pa.canonical_selection([1, 2, 3]), idempotency_key=k)
+    check(a.id == b.id, "same key + order-independent same selection -> same attempt")
+    raised = False
+    try:
+        pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=1000,
+                          line_selection=pa.canonical_selection([1, 2, 4]), idempotency_key=k)
+    except pa.IdempotencyConflict:
+        raised = True
+    check(raised, "same key + different paid-item selection -> IdempotencyConflict (#3)")
+
+
+if __name__ == "__main__":
+    for fn in (
+        test_external_settlement_amount_match,
+        test_settlement_is_idempotent,
+        test_amount_mismatch_reconciles_no_payment,
+        test_currency_mismatch_reconciles_no_payment,
+        test_manual_settles_without_evidence,
+        test_selection_is_part_of_the_fingerprint,
+    ):
+        print(f"- {fn.__name__}")
+        fn()
+    if _failures:
+        print(f"\n{len(_failures)} FAILED")
+        sys.exit(1)
+    print("\nall settlement tests passed")
```
