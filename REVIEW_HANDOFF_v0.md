# Review Handoff v0 — Stage 2c Slice-1 Fix²

**For:** external reviewer (ChatGPT). Corrects the four findings in
`CLAUDE_STAGE2C_SLICE1_FIX_DEEP_REVIEW_FEEDBACK.md`. **Still the settlement service
layer only — no live route wired.** (New handoff naming: v0, v1, v2, …)

## Commit range
- **Slice-1-fix HEAD (reviewed):** `e3c28f79d2e8e18e84bd4eccfe7a7716d3421c27`
- **HEAD (this fix):** `f55f048…`
- Branch: `fix/p0-security-and-payments`
- **Commit added:** `f55f048 fix(2c slice1 v0): fingerprint versioning, VARCHAR->TEXT upgrade, strict item ids`

## Finding-by-finding
| # | Sev | Finding | Status |
|---|---|---|---|
| 1 | CRIT | `line_selection` in the fingerprint breaks idempotent retry of pre-Slice-1 attempts | ✅ versioned fingerprint |
| 2 | CRIT | `VARCHAR(500)->TEXT` doesn't upgrade a DB that already ran Slice-1 | ✅ explicit retype migration |
| 3 | HIGH | `canonical_selection()` silently coerces malformed item ids | ✅ strict validation |
| 4 | MED | Durability claim only proven for amount mismatch | ✅ parameterized amount + currency |

## 1 — Fingerprint backward compatibility (versioned)
`intent_fingerprint(..., version=N)`: **v1** excludes `line_selection`, **v2**
includes it. `CURRENT_FP_VERSION = 2`. Each attempt stores `fingerprint_version`;
`_assert_same_intent` **recomputes at the stored row's version**, so a legacy v1
intent is compared with the v1 algorithm and never becomes a false conflict when
v2 ships. New rows are v2. The migration adds `fingerprint_version` with a
backfill `DEFAULT 1` (existing rows are pre-selection = v1), then **drops that
default** so new inserts rely on the ORM's v2.

**PG upgrade proof** (`test_legacy_fingerprint_idempotency_survives_upgrade`):
build the pre-Slice-1 schema (drop the two Slice-1 columns), insert a row whose
`intent_fingerprint` is the **v1** hash, run the real migration (re-adds the
columns, backfills version=1), then:
1. same legacy intent + key → returns the existing attempt (`fingerprint_version==1`);
2. same key + changed amount → `IdempotencyConflict`.

## 2 — `line_selection` VARCHAR(500) → TEXT upgrade
The additive `ADD COLUMN` skips a column that already exists, so a DB that ran the
Slice-1 intermediate schema keeps `VARCHAR(500)`. The migration now detects a
bounded `character_maximum_length` and runs
`ALTER COLUMN line_selection TYPE TEXT` (Postgres), guarded so a re-run is a no-op.

**PG proof** (`test_line_selection_varchar_to_text_upgrade`): start from
`VARCHAR(500)`, migrate, assert `data_type == 'text'`, a **>500-char** selection
persists, and the retype re-run is a no-op.

## 3 — Strict item-id validation
`canonical_selection` validates each id via `_strict_item_id`: accepts a positive
Python `int` or a digit-only string; **rejects** `bool`, `float`, decimal string,
zero, negative, and arbitrary objects with an explicit `PaymentAttemptError` — no
lossy `int()` coercion (`True→1`, `1.9→1`).

**Proof** (`test_item_id_validation_is_strict`): `1` and `"2"` accepted; `True`,
`False`, `1.9`, `"1.9"`, `0`, `-1`, arbitrary object all rejected.

## 4 — Outer-transaction durability, amount + currency
`test_mismatch_reconciliation_durable_across_outer_tx` now loops over **both**
`pamt=9999` (amount) and `pcur="USD"` (currency): settle with `commit=False`,
`db.commit()`, then a fresh session confirms the attempt is durably
`REQUIRES_RECONCILIATION`.

## Tests — counts (0 failures)
Env: Python 3.14, SQLAlchemy 2.0.51, psycopg 3.3.4 (test-only), Postgres 16 @ :5433.
```
SQLite:  test_settlement 26 ok · full suite (config/attempts/refunds/providers/
         pg_concurrency/pg_migration/templates/security/admin/money/reconciliation/schedule) PASS
Postgres: test_pg_migration 30 ok · test_settlement 26 ok · attempts/refunds/pg_concurrency PASS
```
No regression; the migration + concurrency + provider suites remain green on both
engines.

## Still NOT in this slice
Live route wiring (slice 2, incl. `pay_seat` no-commit refactor + real
concurrent-settlement PG proof under the order lock); refund settlement +
refundable-balance concurrency (slice 3); refund/void route (slice 4); the
consolidated 25-test acceptance matrix.

## Areas I am least confident about
1. **`pay_seat` no-commit refactor (slice 2)** — the real Payment+allocations+seat
   atomicity depends on it; not yet proven.
2. **Exact pre-tip base equality** vs. real Square sandbox rounding — to validate
   against live data before auto-settlement trusts it.
3. **Fingerprint v1→v2 upgrade policy:** legacy rows are re-matched at v1 and left
   at v1 (not auto-upgraded). A first-ever v1 retry that also supplies a selection
   still matches at v1 (selection ignored for v1) — intended, but worth confirming
   the policy is acceptable.

---
## Diff (`git diff e3c28f7..HEAD`)
```diff
diff --git a/app/migrate.py b/app/migrate.py
index fa400a9..f78e978 100644
--- a/app/migrate.py
+++ b/app/migrate.py
@@ -99,6 +99,9 @@ ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
     # Stage 2c: the paid-item selection captured on the charge attempt (TEXT so a
     # large legitimate selection never overflows a fragile VARCHAR cap).
     ("payment_attempt", "line_selection", "TEXT NOT NULL DEFAULT ''"),
+    # Fingerprint algorithm version. Existing rows predate the selection-aware
+    # hash, so they are backfilled to v1; new rows are v2 (set by the ORM).
+    ("payment_attempt", "fingerprint_version", "INTEGER NOT NULL DEFAULT 1"),
 )
 
 # (table, column, min_length, new DDL type). Columns whose type/length GREW
@@ -337,6 +340,29 @@ def _migrate_payment_hardening(engine: Engine, strict: bool) -> list[str]:
                 conn.execute(text('ALTER TABLE payment_attempt ALTER COLUMN provider DROP DEFAULT'))
                 applied.append("dropped legacy default on payment_attempt.provider")
 
+        # 1d. A database that already ran the Slice-1 intermediate schema has
+        # line_selection as VARCHAR(500); widen it to TEXT in place (an additive
+        # ADD COLUMN was skipped because the column already exists — slice-1-fix #2).
+        if pg and _column_exists(conn, "payment_attempt", "line_selection"):
+            maxlen = conn.execute(text(
+                "SELECT character_maximum_length FROM information_schema.columns "
+                "WHERE table_name='payment_attempt' AND column_name='line_selection' "
+                "AND table_schema=current_schema()")).scalar_one_or_none()
+            if maxlen is not None:      # currently a bounded VARCHAR -> retype to TEXT
+                conn.execute(text('ALTER TABLE payment_attempt ALTER COLUMN line_selection TYPE TEXT'))
+                applied.append("widened payment_attempt.line_selection -> TEXT")
+
+        # 1e. Drop the backfill default on fingerprint_version so new inserts rely
+        # on the ORM's v2, not a lingering v1 server default.
+        if pg and _column_exists(conn, "payment_attempt", "fingerprint_version"):
+            fv_default = conn.execute(text(
+                "SELECT column_default FROM information_schema.columns "
+                "WHERE table_name='payment_attempt' AND column_name='fingerprint_version' "
+                "AND table_schema=current_schema()")).scalar_one_or_none()
+            if fv_default is not None:
+                conn.execute(text('ALTER TABLE payment_attempt ALTER COLUMN fingerprint_version DROP DEFAULT'))
+                applied.append("dropped backfill default on payment_attempt.fingerprint_version")
+
         # 2. Retire the old provider_refund_id. Drop only when empty; non-null under
         # strict fails closed (rolling back the whole tx).
         if _column_exists(conn, "payment_attempt", "provider_refund_id"):
diff --git a/app/models/oltp.py b/app/models/oltp.py
index 91b0919..3bc2037 100644
--- a/app/models/oltp.py
+++ b/app/models/oltp.py
@@ -1066,8 +1066,13 @@ class PaymentAttempt(Base):
     # reusing a key with different order/amount/currency/selection is a conflict,
     # not a silent wrong-attempt hit.
     intent_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
+    # Which fingerprint algorithm produced intent_fingerprint. v1 = pre-Stage-2c
+    # (selection-unaware); v2 = selection-aware. A durable attempt is re-matched
+    # using ITS OWN version, so adding line_selection to the hash never turns a
+    # legacy intent into a false IdempotencyConflict on retry (slice-1-fix review #1).
+    fingerprint_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
     # Canonical identity of WHAT is being paid — the sorted set of OrderItem ids
-    # this attempt settles (Stage 2c). Part of the intent fingerprint, and what
+    # this attempt settles (Stage 2c). Part of the v2 intent fingerprint, and what
     # settlement reconciles against. TEXT (not a fragile VARCHAR cap) so a large
     # legitimate selection never fails as a low-level DB error (slice-1 review #4).
     line_selection: Mapped[str] = mapped_column(Text, nullable=False, default="")
diff --git a/app/services/payment_attempts.py b/app/services/payment_attempts.py
index 1c848cd..0dedef2 100644
--- a/app/services/payment_attempts.py
+++ b/app/services/payment_attempts.py
@@ -77,15 +77,32 @@ def new_idempotency_key() -> str:
     return secrets.token_hex(24)
 
 
+# Current fingerprint algorithm version (v2 = selection-aware).
+CURRENT_FP_VERSION = 2
+
+
+def _strict_item_id(v) -> int:
+    """A paid-item id is a positive integer. Reject bool/float/decimal-string/
+    zero/negative/objects rather than silently coercing (slice-1-fix review #3).
+    A digit-only string is accepted (form inputs arrive as strings)."""
+    if isinstance(v, bool):
+        raise PaymentAttemptError(f"invalid item id {v!r} (bool is not an id)")
+    if isinstance(v, int):
+        n = v
+    elif isinstance(v, str) and v.strip().isdigit():
+        n = int(v.strip())
+    else:
+        raise PaymentAttemptError(f"invalid item id {v!r} (expected a positive integer)")
+    if n <= 0:
+        raise PaymentAttemptError(f"invalid item id {n} (must be positive)")
+    return n
+
+
 def canonical_selection(item_ids) -> str:
     """Stable identity of the paid-item set: sorted, de-duplicated, comma-joined
-    (Stage 2c). '' means a whole-order/amount-only intent. Malformed ids raise an
-    explicit domain error rather than silently coercing (slice-1 review #4)."""
-    try:
-        ids = sorted({int(i) for i in (item_ids or [])})
-    except (TypeError, ValueError) as exc:
-        raise PaymentAttemptError(f"invalid item id in selection: {exc}") from exc
-    return ",".join(str(i) for i in ids)
+    (Stage 2c). '' means a whole-order/amount-only intent. Each id is strictly
+    validated (positive integer) — no lossy coercion (slice-1-fix review #3)."""
+    return ",".join(str(i) for i in sorted({_strict_item_id(i) for i in (item_ids or [])}))
 
 
 def intent_fingerprint(
@@ -103,15 +120,20 @@ def intent_fingerprint(
     discount_cents: int,
     surcharge_cents: int,
     line_selection: str = "",
+    version: int = CURRENT_FP_VERSION,
 ) -> str:
-    """Stable hash of the immutable intent behind an idempotency key. Reusing a
-    key with a different fingerprint — including a different paid-item selection —
-    is rejected as a conflict."""
-    canonical = "|".join(str(x) for x in (
+    """Stable hash of the immutable intent behind an idempotency key. ``version``
+    selects the algorithm: v1 (pre-Stage-2c) excludes the paid-item selection, v2
+    includes it. A durable attempt is always re-matched using its stored version,
+    so introducing v2 never turns a legacy v1 intent into a false conflict."""
+    fields = [
         provider, order_id, seat_id, staff_id, currency.upper(),
         expected_total_cents, subtotal_cents, tax_cents, tip_cents,
-        service_charge_cents, discount_cents, surcharge_cents, line_selection,
-    ))
+        service_charge_cents, discount_cents, surcharge_cents,
+    ]
+    if version >= 2:
+        fields.append(line_selection)
+    canonical = "|".join(str(x) for x in fields)
     return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:64]
 
 
@@ -158,8 +180,12 @@ def _by_key(db: Session, key: str) -> PaymentAttempt | None:
     ).scalar_one_or_none()
 
 
-def _assert_same_intent(existing: PaymentAttempt, fingerprint: str) -> None:
-    if existing.intent_fingerprint and existing.intent_fingerprint != fingerprint:
+def _assert_same_intent(existing: PaymentAttempt, intent: dict) -> None:
+    """Re-match the requested intent against a stored attempt using the stored
+    row's own fingerprint version (slice-1-fix #1) — so a v1 legacy row is compared
+    with the v1 (selection-unaware) algorithm and a v2 row with v2."""
+    expected = intent_fingerprint(**intent, version=existing.fingerprint_version)
+    if existing.intent_fingerprint and existing.intent_fingerprint != expected:
         raise IdempotencyConflict(
             f"idempotency key {existing.idempotency_key!r} was already used for a "
             f"different payment intent (attempt {existing.id})."
@@ -200,18 +226,19 @@ def create_attempt(
     if expected_total_cents < 0:
         raise PaymentAttemptError("expected_total_cents cannot be negative.")
 
-    fingerprint = intent_fingerprint(
+    intent = dict(
         provider=provider, order_id=order_id, seat_id=seat_id, staff_id=staff_id,
         currency=currency, expected_total_cents=expected_total_cents,
         subtotal_cents=subtotal_cents, tax_cents=tax_cents, tip_cents=tip_cents,
         service_charge_cents=service_charge_cents, discount_cents=discount_cents,
         surcharge_cents=surcharge_cents, line_selection=line_selection,
     )
+    fingerprint = intent_fingerprint(**intent, version=CURRENT_FP_VERSION)
 
     if idempotency_key:
         existing = _by_key(db, idempotency_key)
         if existing is not None:
-            _assert_same_intent(existing, fingerprint)
+            _assert_same_intent(existing, intent)
             return existing
     else:
         idempotency_key = new_idempotency_key()
@@ -219,7 +246,7 @@ def create_attempt(
     attempt = PaymentAttempt(
         order_id=order_id, seat_id=seat_id, staff_id=staff_id, provider=provider,
         idempotency_key=idempotency_key, intent_fingerprint=fingerprint,
-        line_selection=line_selection,
+        fingerprint_version=CURRENT_FP_VERSION, line_selection=line_selection,
         subtotal_cents=subtotal_cents, tax_cents=tax_cents, tip_cents=tip_cents,
         service_charge_cents=service_charge_cents, discount_cents=discount_cents,
         surcharge_cents=surcharge_cents, expected_total_cents=expected_total_cents,
@@ -235,7 +262,7 @@ def create_attempt(
         existing = _by_key(db, idempotency_key)
         if existing is None:
             raise
-        _assert_same_intent(existing, fingerprint)
+        _assert_same_intent(existing, intent)
         return existing
     db.refresh(attempt)
     return attempt
diff --git a/tests/test_pg_migration.py b/tests/test_pg_migration.py
index ef13d0e..be20406 100644
--- a/tests/test_pg_migration.py
+++ b/tests/test_pg_migration.py
@@ -12,10 +12,14 @@ import sys
 
 sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
 
-from tests._pay_fixture import pg_dsn
+from tests._pay_fixture import Session, pg_dsn, seed_parents
 
 from sqlalchemy import create_engine, text
 from app import migrate
+from app.database import Base
+from app.models import oltp  # noqa: F401
+from app.models.oltp import PaymentAttempt
+from app.services import payment_attempts as pa
 
 _failures = []
 
@@ -250,6 +254,92 @@ def test_hardening_atomic_rollback(engine):
     check(providers == {"square"}, "provider backfill rolled back too (rows still 'square')")
 
 
+def test_legacy_fingerprint_idempotency_survives_upgrade(engine):
+    """A pre-Stage-2c (v1, selection-unaware) attempt must still be re-matched by a
+    retry after the selection-aware v2 hash is introduced (slice-1-fix #1)."""
+    Base.metadata.drop_all(engine)
+    Base.metadata.create_all(engine)
+    s = Session(engine)()
+    ids = seed_parents(s)
+    s.close()
+    # Simulate the pre-Slice-1 schema: remove the two Slice-1 columns.
+    with engine.begin() as c:
+        c.execute(text("ALTER TABLE payment_attempt DROP COLUMN line_selection"))
+        c.execute(text("ALTER TABLE payment_attempt DROP COLUMN fingerprint_version"))
+    intent = dict(provider="manual", order_id=ids["order_id"], seat_id=None,
+                  staff_id=ids["staff_id"], currency="CAD", expected_total_cents=1000,
+                  subtotal_cents=1000, tax_cents=0, tip_cents=0, service_charge_cents=0,
+                  discount_cents=0, surcharge_cents=0, line_selection="")
+    fp_v1 = pa.intent_fingerprint(**intent, version=1)
+    with engine.begin() as c:
+        c.execute(text(
+            "INSERT INTO payment_attempt (order_id, staff_id, provider, idempotency_key, "
+            "intent_fingerprint, subtotal_cents, tax_cents, tip_cents, service_charge_cents, "
+            "discount_cents, surcharge_cents, expected_total_cents, currency, status, last_error, "
+            "reconciled_by, reconciliation_note, created_at, updated_at) "
+            "VALUES (:o,:s,'manual','legacy-key',:fp,1000,0,0,0,0,0,1000,'CAD','created','','','',"
+            "now(),now())"),
+            {"o": ids["order_id"], "s": ids["staff_id"], "fp": fp_v1})
+
+    migrate.run(engine, strict=True)   # re-adds line_selection (TEXT) + fingerprint_version (backfilled 1)
+
+    s2 = Session(engine)()
+    a = pa.create_attempt(s2, provider="manual", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=1000,
+                          subtotal_cents=1000, currency="CAD", idempotency_key="legacy-key")
+    check(a.idempotency_key == "legacy-key" and a.fingerprint_version == 1,
+          "legacy v1 attempt retried returns the existing row, not a false conflict (#1)")
+    raised = False
+    try:
+        pa.create_attempt(s2, provider="manual", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=2000,
+                          subtotal_cents=2000, currency="CAD", idempotency_key="legacy-key")
+    except pa.IdempotencyConflict:
+        raised = True
+    check(raised, "legacy row: same key + changed amount still conflicts (#1)")
+    s2.close()
+
+
+def test_line_selection_varchar_to_text_upgrade(engine):
+    """A DB that ran the Slice-1 intermediate schema has line_selection VARCHAR(500);
+    the migration must widen it to TEXT in place (slice-1-fix #2)."""
+    Base.metadata.drop_all(engine)
+    Base.metadata.create_all(engine)
+    s = Session(engine)()
+    ids = seed_parents(s)
+    s.close()
+    with engine.begin() as c:
+        c.execute(text("ALTER TABLE payment_attempt ALTER COLUMN line_selection TYPE VARCHAR(500)"))
+
+    migrate.run(engine, strict=True)
+
+    with engine.connect() as c:
+        dtype = c.execute(text(
+            "SELECT data_type FROM information_schema.columns WHERE table_name='payment_attempt' "
+            "AND column_name='line_selection' AND table_schema=current_schema()")).scalar_one()
+    check(dtype == "text", "line_selection widened VARCHAR(500) -> TEXT (#2)")
+
+    long_sel = ",".join(str(i) for i in range(1, 400))   # > 500 chars
+    with engine.begin() as c:
+        c.execute(text(
+            "INSERT INTO payment_attempt (order_id, staff_id, provider, idempotency_key, "
+            "line_selection, intent_fingerprint, subtotal_cents, tax_cents, tip_cents, "
+            "service_charge_cents, discount_cents, surcharge_cents, expected_total_cents, currency, "
+            "status, last_error, reconciled_by, reconciliation_note, fingerprint_version, "
+            "created_at, updated_at) "
+            "VALUES (:o,:s,'manual','long-sel',:ls,'',1000,0,0,0,0,0,1000,'CAD','created','','','',2,"
+            "now(),now())"),
+            {"o": ids["order_id"], "s": ids["staff_id"], "ls": long_sel})
+    with engine.connect() as c:
+        stored = c.execute(text(
+            "SELECT line_selection FROM payment_attempt WHERE idempotency_key='long-sel'")).scalar_one()
+    check(stored == long_sel and len(stored) > 500, "a >500-char selection persists after upgrade (#2)")
+
+    # rerun is a no-op for the retype
+    again = migrate.run(engine, strict=True)
+    check(not any("line_selection -> TEXT" in a for a in again), "TEXT retype re-run is a no-op (#2)")
+
+
 def test_nonnull_provider_refund_id_fails_strict(engine):
     with engine.begin() as conn:
         conn.execute(text("DROP TABLE IF EXISTS refund_attempt CASCADE"))
@@ -317,6 +407,8 @@ if __name__ == "__main__":
     for fn in (test_clean_upgrade, test_provider_default_removed,
                test_card_terminal_instrument_backfilled, test_card_terminal_preserves_explicit_provider,
                test_hardening_is_idempotent, test_hardening_atomic_rollback,
+               test_legacy_fingerprint_idempotency_survives_upgrade,
+               test_line_selection_varchar_to_text_upgrade,
                test_nonnull_provider_refund_id_fails_strict,
                test_upgrade_blocks_on_duplicate_payment_id,
                test_upgrade_blocks_on_duplicate_checkout_id, test_duplicate_rejected_after_upgrade):
diff --git a/tests/test_settlement.py b/tests/test_settlement.py
index b4bf1e8..a10ef9a 100644
--- a/tests/test_settlement.py
+++ b/tests/test_settlement.py
@@ -107,20 +107,44 @@ def test_currency_mismatch_reconciles_no_payment():
 
 
 def test_mismatch_reconciliation_durable_across_outer_tx():
-    """Slice-1 review #1: with commit=False inside the caller's transaction, a
-    mismatch must survive the caller's commit (no exception rolls it back)."""
-    SM, ids = _fresh_sessions()
-    db = SM()
-    a = _approved_ext(db, ids, expected=1000, pamt=9999)
-    aid = a.id
-    res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000), commit=False)
-    check(not res.is_settled, "mismatch reported as result, not exception (#1)")
-    db.commit()          # caller commits the transaction incl. the reconciliation transition
-    db.close()
-    db2 = SM()
-    fresh = db2.get(PaymentAttempt, aid)
-    check(fresh.status == S.REQUIRES_RECONCILIATION, "reconciliation durably committed (#1)")
-    db2.close()
+    """Slice-1 review #1 + slice-1-fix review #4: with commit=False inside the
+    caller's transaction, BOTH an amount and a currency mismatch must survive the
+    caller's commit (no exception rolls it back)."""
+    for label, kw in (("amount", {"pamt": 9999}), ("currency", {"pcur": "USD"})):
+        SM, ids = _fresh_sessions()
+        db = SM()
+        a = _approved_ext(db, ids, expected=1000, **kw)
+        aid = a.id
+        res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000), commit=False)
+        check(not res.is_settled, f"{label} mismatch reported as result, not exception (#1)")
+        db.commit()      # caller commits the transaction incl. the reconciliation transition
+        db.close()
+        db2 = SM()
+        fresh = db2.get(PaymentAttempt, aid)
+        check(fresh.status == S.REQUIRES_RECONCILIATION,
+              f"{label} mismatch reconciliation durably committed across outer tx (#1/#4)")
+        db2.close()
+
+
+def test_item_id_validation_is_strict():
+    db, ids = _db()
+
+    def make(item_ids, key):
+        return pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
+                                 staff_id=ids["staff_id"], expected_total_cents=1000,
+                                 item_ids=item_ids, idempotency_key=key)
+
+    check(make([1], "a").line_selection == "1", "positive int accepted (#3)")
+    check(make(["2"], "b").line_selection == "2", "digit string accepted (#3)")
+    for bad, label in ((True, "bool True"), (False, "bool False"), (1.9, "float"),
+                       ("1.9", "decimal string"), (0, "zero"), (-1, "negative"),
+                       (object(), "object")):
+        raised = False
+        try:
+            make([bad], f"k_{label}")
+        except pa.PaymentAttemptError:
+            raised = True
+        check(raised, f"{label} item id rejected (#3)")
 
 
 def test_manual_settles_without_evidence():
@@ -175,6 +199,7 @@ if __name__ == "__main__":
         test_amount_mismatch_reconciles_no_payment,
         test_currency_mismatch_reconciles_no_payment,
         test_mismatch_reconciliation_durable_across_outer_tx,
+        test_item_id_validation_is_strict,
         test_manual_settles_without_evidence,
         test_selection_canonicalized_by_service,
         test_different_selection_conflicts,
```
