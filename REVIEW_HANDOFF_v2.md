# Review Handoff v2 — Stage 2c Slice-1 (Fingerprint Migration Correctness)

**For:** external reviewer (ChatGPT), for audit. Corrects the three findings in
`CLAUDE_STAGE2C_V1_DEEP_REVIEW_FEEDBACK.md`. **Still the settlement service layer
only — no live route wired.**

## Commit range
- **v1 HEAD (reviewed):** `a0c0cf9`
- **HEAD (this handoff):** `ca9ea30`
- Branch: `fix/p0-security-and-payments`
- **Commit added:** `ca9ea30 fix(2c v2): per-row fingerprint-version backfill, unsupported-version fail-closed, ASCII item ids`

## `git status --short`
No uncommitted **source** changes. Untracked non-source: `REVIEW_HANDOFF*.md`,
business docs/decks, `.github/`, `restaurant-app-review.zip`.

## Finding-by-finding
| # | Sev | Finding | Status |
|---|---|---|---|
| 1 | CRIT | Blanket `fingerprint_version=1` mislabels intermediate v2 rows; empty legacy fingerprints treated as accept-any | ✅ per-row classification + reconstruction + fail-closed |
| 2 | HIGH | Unsupported/corrupt fingerprint versions silently interpreted | ✅ `{1,2}` supported set, else fail closed |
| 3 | MED | Digit-string item validation not ASCII-safe (could leak a raw ValueError) | ✅ ASCII `[0-9]+` grammar |

## 1 — Per-row fingerprint-version classification (exact algorithm)
The migration **no longer** adds `fingerprint_version` with a blanket default.
Instead, in `_migrate_payment_hardening` (one atomic transaction):
1. If the column is absent, `ADD COLUMN fingerprint_version INTEGER NOT NULL DEFAULT 0`
   (0 = *unclassified* sentinel — a neutral value, not a guess).
2. `_backfill_fingerprint_version()` classifies **each** row where
   `fingerprint_version = 0`, recomputing **both** algorithms from that row's own
   snapshot (`provider, order_id, seat_id, staff_id, currency, expected_total,
   subtotal, tax, tip, service_charge, discount, surcharge, line_selection`):
   - stored hash `== v2` → `fingerprint_version = 2`;
   - stored hash `== v1` → `fingerprint_version = 1`;
   - stored hash `== ''` (predates fingerprinting; snapshot present) → **reconstruct**
     the canonical v1 hash and set it, version 1 (so a *different* intent can no
     longer reuse the key);
   - **matches neither** → **fail closed** (`MigrationError`) under `strict`; in
     non-strict it is left at 0, which then fails closed at idempotency (#2).
3. Postgres: drop the sentinel default so new inserts use the ORM's v2.

### Handling of empty historical fingerprints
Reconstructed to the canonical v1 hash from the immutable snapshot during
migration; `_assert_same_intent` no longer treats an empty stored fingerprint as
"accept any intent" (the truthiness guard was removed).

### Fail-closed for a hash matching neither v1 nor v2
`strict=True` (production) raises `MigrationError` naming the row; the row is never
guessed. Non-strict leaves it unclassified (0) → rejected on any idempotent reuse.

### PostgreSQL mixed-row migration proof (`test_mixed_fingerprint_version_backfill`, +`test_unverifiable_fingerprint_fails_strict`)
Builds the intermediate schema (drops `fingerprint_version`, keeps `line_selection`)
with **Row A** (v1 hash, empty selection), **Row B** (v2 hash, selection `1,2,3`),
**Row C** (empty hash, snapshot present), runs the real migration, and proves:
1. A → version 1; same-intent retry returns A; changed amount conflicts.
2. B → version 2; same-selection retry returns B; **different selection conflicts**.
3. C → reconstructed non-empty hash, version 1; changed intent does **not** silently pass.
4. an unclassifiable stored hash **fails closed** under strict.
5. migration rerun is idempotent (no rows left at 0 → nothing re-classified).

## 2 — Supported fingerprint-version set
`SUPPORTED_FP_VERSIONS = {1, 2}`. `_assert_same_intent` rejects a stored version
outside the set with `IdempotencyConflict` (a corrupt/unclassified/future row can
never reuse its idempotency key). `intent_fingerprint(version=…)` also raises on an
unsupported version. **Proof:** `test_unsupported_fingerprint_version_fails_closed`
— stored versions `0`, `3`, `99` all fail closed.

## 3 — ASCII-only item-id grammar
`_strict_item_id` matches `^[0-9]+$` (ASCII), so a Unicode digit-like character
(`str.isdigit()` accepts `²`, `٣`) is rejected with `PaymentAttemptError` — never a
raw parser exception. **Proof:** `test_item_id_validation_is_strict` now also
rejects `"²"` and `"٣"` alongside bool/float/decimal/zero/negative/object.

## Tests — counts (0 failures)
Env: Python 3.14, SQLAlchemy 2.0.51, psycopg 3.3.4 (test-only), Postgres 16 @ :5433.
```
SQLite (full suite green):
  test_settlement 28 · test_payment_attempts 32 · test_refund_attempts 39 · test_payment_providers 79 · … PASS
PostgreSQL:
  test_pg_migration 41 · test_settlement 28 · test_payment_attempts · test_refund_attempts ·
  test_pg_concurrency (incl. two-session CAS convergence)  PASS
```
No regression; migration + concurrency + provider suites remain green on both engines.

## Still NOT in this slice (unchanged)
Live route wiring (slice 2 — `pay_seat` no-commit refactor + real concurrent-
settlement PG proof under the order lock); refund settlement + refundable-balance
concurrency (slice 3); refund/void route (slice 4); the consolidated 25-test matrix.

## Areas I am least confident about
1. **Backfill recompute fidelity.** Per-row classification recomputes the exact
   canonical string from the row's columns; if any pre-Slice-1 code ever wrote a
   fingerprint from a *different* field order/format than today's `intent_fingerprint`,
   those rows would be flagged unverifiable (fail closed) rather than matched. That
   is the safe direction, but worth confirming no such format drift exists.
2. **Faked Square vs. real sandbox** — the largest remaining trust gap (unchanged).
3. **`pay_seat` no-commit refactor (slice 2)** — not yet built or proven.

---
## Diff (`git diff a0c0cf9..HEAD`)
```diff
diff --git a/app/migrate.py b/app/migrate.py
index f78e978..a1e338d 100644
--- a/app/migrate.py
+++ b/app/migrate.py
@@ -99,9 +99,9 @@ ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
     # Stage 2c: the paid-item selection captured on the charge attempt (TEXT so a
     # large legitimate selection never overflows a fragile VARCHAR cap).
     ("payment_attempt", "line_selection", "TEXT NOT NULL DEFAULT ''"),
-    # Fingerprint algorithm version. Existing rows predate the selection-aware
-    # hash, so they are backfilled to v1; new rows are v2 (set by the ORM).
-    ("payment_attempt", "fingerprint_version", "INTEGER NOT NULL DEFAULT 1"),
+    # NOTE: fingerprint_version is NOT added here — a blanket default would
+    # misclassify rows created by the intermediate (v2, selection-aware) schema.
+    # It is added + classified PER ROW in _migrate_payment_hardening.
 )
 
 # (table, column, min_length, new DDL type). Columns whose type/length GREW
@@ -352,16 +352,23 @@ def _migrate_payment_hardening(engine: Engine, strict: bool) -> list[str]:
                 conn.execute(text('ALTER TABLE payment_attempt ALTER COLUMN line_selection TYPE TEXT'))
                 applied.append("widened payment_attempt.line_selection -> TEXT")
 
-        # 1e. Drop the backfill default on fingerprint_version so new inserts rely
-        # on the ORM's v2, not a lingering v1 server default.
-        if pg and _column_exists(conn, "payment_attempt", "fingerprint_version"):
+        # 1e. fingerprint_version: add with a neutral sentinel (0 = unclassified),
+        # then classify EACH existing row by matching its stored hash against the
+        # v1 and v2 algorithms recomputed from that row's own snapshot — never a
+        # blanket default that would mislabel intermediate v2 rows (slice-1-fix² #1).
+        if not _column_exists(conn, "payment_attempt", "fingerprint_version"):
+            conn.execute(text(
+                "ALTER TABLE payment_attempt ADD COLUMN fingerprint_version INTEGER NOT NULL DEFAULT 0"))
+            applied.append("added payment_attempt.fingerprint_version (0=unclassified)")
+        applied.extend(_backfill_fingerprint_version(conn, strict))
+        if pg:
             fv_default = conn.execute(text(
                 "SELECT column_default FROM information_schema.columns "
                 "WHERE table_name='payment_attempt' AND column_name='fingerprint_version' "
                 "AND table_schema=current_schema()")).scalar_one_or_none()
             if fv_default is not None:
                 conn.execute(text('ALTER TABLE payment_attempt ALTER COLUMN fingerprint_version DROP DEFAULT'))
-                applied.append("dropped backfill default on payment_attempt.fingerprint_version")
+                applied.append("dropped sentinel default on payment_attempt.fingerprint_version")
 
         # 2. Retire the old provider_refund_id. Drop only when empty; non-null under
         # strict fails closed (rolling back the whole tx).
@@ -411,6 +418,58 @@ def _migrate_payment_hardening(engine: Engine, strict: bool) -> list[str]:
     return applied
 
 
+def _backfill_fingerprint_version(conn, strict: bool) -> list[str]:
+    """Classify each not-yet-versioned payment_attempt row (fingerprint_version=0)
+    by recomputing v1 and v2 fingerprints from its own snapshot and matching the
+    stored hash — so intermediate v2 (selection-aware) rows are labelled v2, older
+    v1 rows v1, and pre-fingerprint ('' hash) rows get a reconstructed v1 hash. A
+    stored hash matching neither fails closed (strict) rather than being guessed
+    (slice-1-fix² #1)."""
+    from app.services.payment_attempts import intent_fingerprint  # lazy: avoids import cycle
+    applied: list[str] = []
+    rows = conn.execute(text(
+        "SELECT id, provider, order_id, seat_id, staff_id, currency, expected_total_cents, "
+        "subtotal_cents, tax_cents, tip_cents, service_charge_cents, discount_cents, "
+        "surcharge_cents, line_selection, intent_fingerprint FROM payment_attempt "
+        "WHERE fingerprint_version = 0")).mappings().all()
+    reconstructed = unverifiable = 0
+    for r in rows:
+        fields = dict(
+            provider=r["provider"], order_id=r["order_id"], seat_id=r["seat_id"],
+            staff_id=r["staff_id"], currency=r["currency"] or "CAD",
+            expected_total_cents=r["expected_total_cents"], subtotal_cents=r["subtotal_cents"],
+            tax_cents=r["tax_cents"], tip_cents=r["tip_cents"],
+            service_charge_cents=r["service_charge_cents"], discount_cents=r["discount_cents"],
+            surcharge_cents=r["surcharge_cents"], line_selection=r["line_selection"] or "",
+        )
+        stored = r["intent_fingerprint"] or ""
+        fp_v1 = intent_fingerprint(**fields, version=1)
+        fp_v2 = intent_fingerprint(**fields, version=2)
+        if stored == "":
+            # Predates fingerprinting; the immutable snapshot is present, so
+            # reconstruct the canonical v1 hash instead of leaving '' (which would
+            # otherwise let a DIFFERENT intent reuse the key).
+            conn.execute(text("UPDATE payment_attempt SET intent_fingerprint=:fp, "
+                              "fingerprint_version=1 WHERE id=:id"), {"fp": fp_v1, "id": r["id"]})
+            reconstructed += 1
+        elif stored == fp_v2:
+            conn.execute(text("UPDATE payment_attempt SET fingerprint_version=2 WHERE id=:id"),
+                         {"id": r["id"]})
+        elif stored == fp_v1:
+            conn.execute(text("UPDATE payment_attempt SET fingerprint_version=1 WHERE id=:id"),
+                         {"id": r["id"]})
+        else:
+            msg = (f"payment_attempt {r['id']} has an intent_fingerprint matching neither v1 nor "
+                   "v2 recomputed from its snapshot; cannot classify safely.")
+            if strict:
+                raise MigrationError(msg)
+            unverifiable += 1  # left at 0 -> _assert_same_intent fails closed on it
+    if rows:
+        applied.append(f"classified {len(rows)} fingerprint_version rows "
+                       f"({reconstructed} reconstructed, {unverifiable} unverifiable)")
+    return applied
+
+
 def _backfill_locations(conn) -> list[str]:
     """Give tables that predate floors and zones a home.
 
diff --git a/app/services/payment_attempts.py b/app/services/payment_attempts.py
index 0dedef2..fd5e424 100644
--- a/app/services/payment_attempts.py
+++ b/app/services/payment_attempts.py
@@ -22,6 +22,7 @@ Concurrency/idempotency guarantees (Postgres and SQLite):
 from __future__ import annotations
 
 import hashlib
+import re
 import secrets
 from datetime import datetime
 
@@ -77,19 +78,28 @@ def new_idempotency_key() -> str:
     return secrets.token_hex(24)
 
 
-# Current fingerprint algorithm version (v2 = selection-aware).
+# Fingerprint algorithm versions. v1 = selection-unaware (pre-Stage-2c),
+# v2 = selection-aware. Current for new rows is v2. An attempt whose stored
+# version is outside this set is unverifiable and fails closed on idempotency.
 CURRENT_FP_VERSION = 2
+SUPPORTED_FP_VERSIONS = frozenset({1, 2})
+
+# A paid-item id is a run of ASCII digits — deliberately narrow, so a Unicode
+# digit-like character (accepted by str.isdigit) can't slip through and leak a
+# raw parser error (slice-1-fix² #3).
+_ITEM_ID_RE = re.compile(r"^[0-9]+$")
 
 
 def _strict_item_id(v) -> int:
     """A paid-item id is a positive integer. Reject bool/float/decimal-string/
-    zero/negative/objects rather than silently coercing (slice-1-fix review #3).
-    A digit-only string is accepted (form inputs arrive as strings)."""
+    non-ASCII-digit/zero/negative/objects with a domain error — never a silent
+    coercion or a raw parser exception (slice-1-fix review #3 / slice-1-fix² #3).
+    An ASCII digit-only string is accepted (form inputs arrive as strings)."""
     if isinstance(v, bool):
         raise PaymentAttemptError(f"invalid item id {v!r} (bool is not an id)")
     if isinstance(v, int):
         n = v
-    elif isinstance(v, str) and v.strip().isdigit():
+    elif isinstance(v, str) and _ITEM_ID_RE.match(v.strip()):
         n = int(v.strip())
     else:
         raise PaymentAttemptError(f"invalid item id {v!r} (expected a positive integer)")
@@ -126,12 +136,14 @@ def intent_fingerprint(
     selects the algorithm: v1 (pre-Stage-2c) excludes the paid-item selection, v2
     includes it. A durable attempt is always re-matched using its stored version,
     so introducing v2 never turns a legacy v1 intent into a false conflict."""
+    if version not in SUPPORTED_FP_VERSIONS:
+        raise PaymentAttemptError(f"unsupported fingerprint version {version!r}")
     fields = [
         provider, order_id, seat_id, staff_id, currency.upper(),
         expected_total_cents, subtotal_cents, tax_cents, tip_cents,
         service_charge_cents, discount_cents, surcharge_cents,
     ]
-    if version >= 2:
+    if version == 2:
         fields.append(line_selection)
     canonical = "|".join(str(x) for x in fields)
     return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:64]
@@ -183,9 +195,15 @@ def _by_key(db: Session, key: str) -> PaymentAttempt | None:
 def _assert_same_intent(existing: PaymentAttempt, intent: dict) -> None:
     """Re-match the requested intent against a stored attempt using the stored
     row's own fingerprint version (slice-1-fix #1) — so a v1 legacy row is compared
-    with the v1 (selection-unaware) algorithm and a v2 row with v2."""
+    with the v1 (selection-unaware) algorithm and a v2 row with v2. An unsupported
+    stored version (e.g. an unclassified/corrupt row) fails closed (slice-1-fix² #2),
+    and an empty stored fingerprint is never treated as 'accept any intent'."""
+    if existing.fingerprint_version not in SUPPORTED_FP_VERSIONS:
+        raise IdempotencyConflict(
+            f"attempt {existing.id} has an unsupported/unverified fingerprint version "
+            f"{existing.fingerprint_version!r}; refusing to reuse its idempotency key.")
     expected = intent_fingerprint(**intent, version=existing.fingerprint_version)
-    if existing.intent_fingerprint and existing.intent_fingerprint != expected:
+    if existing.intent_fingerprint != expected:
         raise IdempotencyConflict(
             f"idempotency key {existing.idempotency_key!r} was already used for a "
             f"different payment intent (attempt {existing.id})."
diff --git a/tests/test_payment_attempts.py b/tests/test_payment_attempts.py
index 762f86d..0da684d 100644
--- a/tests/test_payment_attempts.py
+++ b/tests/test_payment_attempts.py
@@ -163,6 +163,22 @@ def test_external_approval_requires_evidence():
     check(m.status == S.PROCESSOR_APPROVED, "manual provider approval remains valid (#2)")
 
 
+def test_unsupported_fingerprint_version_fails_closed():
+    from sqlalchemy import update as _upd
+    for bad in (0, 3, 99):
+        db, ids = _db()
+        a = _mk(db, ids, key="v")
+        db.execute(_upd(PaymentAttempt).where(PaymentAttempt.id == a.id)
+                   .values(fingerprint_version=bad))
+        db.commit()
+        raised = False
+        try:
+            _mk(db, ids, key="v")   # same key; _assert_same_intent sees the bad version
+        except pa.IdempotencyConflict:
+            raised = True
+        check(raised, f"stored fingerprint_version {bad} fails closed on idempotency (#2)")
+
+
 def test_currency_defaults_to_venue():
     db, ids = _db()
     old = os.environ.get("VENUE_CURRENCY")
@@ -280,6 +296,7 @@ if __name__ == "__main__":
         test_write_once_provider_id_via_cas,
         test_snapshot_immutable_across_transitions,
         test_external_approval_requires_evidence,
+        test_unsupported_fingerprint_version_fails_closed,
         test_currency_defaults_to_venue,
         test_processor_evidence_is_write_once,
         test_reconciliation_validation_and_automatic,
diff --git a/tests/test_pg_migration.py b/tests/test_pg_migration.py
index be20406..85f5ae1 100644
--- a/tests/test_pg_migration.py
+++ b/tests/test_pg_migration.py
@@ -300,6 +300,107 @@ def test_legacy_fingerprint_idempotency_survives_upgrade(engine):
     s2.close()
 
 
+_ATTEMPT_COLS = (
+    "order_id, staff_id, provider, idempotency_key, intent_fingerprint, line_selection, "
+    "subtotal_cents, tax_cents, tip_cents, service_charge_cents, discount_cents, surcharge_cents, "
+    "expected_total_cents, currency, status, last_error, reconciled_by, reconciliation_note, "
+    "created_at, updated_at")
+
+
+def _fields(ids, provider, expected, subtotal, line_selection):
+    return dict(provider=provider, order_id=ids["order_id"], seat_id=None, staff_id=ids["staff_id"],
+                currency="CAD", expected_total_cents=expected, subtotal_cents=subtotal, tax_cents=0,
+                tip_cents=0, service_charge_cents=0, discount_cents=0, surcharge_cents=0,
+                line_selection=line_selection)
+
+
+def _insert_intermediate(engine, ids, *, key, fp, ls, provider="manual", expected=1000, subtotal=1000):
+    with engine.begin() as c:
+        c.execute(text(
+            f"INSERT INTO payment_attempt ({_ATTEMPT_COLS}) VALUES "
+            "(:o,:s,:p,:k,:fp,:ls,:sub,0,0,0,0,0,:tot,'CAD','created','','','',now(),now())"),
+            {"o": ids["order_id"], "s": ids["staff_id"], "p": provider, "k": key, "fp": fp,
+             "ls": ls, "sub": subtotal, "tot": expected})
+
+
+def _version_of(engine, key):
+    with engine.connect() as c:
+        return c.execute(text("SELECT fingerprint_version, intent_fingerprint FROM payment_attempt "
+                              "WHERE idempotency_key=:k"), {"k": key}).one()
+
+
+def test_mixed_fingerprint_version_backfill(engine):
+    """A DB with BOTH pre-selection (v1) and intermediate (v2) rows plus an
+    empty-fingerprint row must be classified PER ROW, not by a blanket default
+    (slice-1-fix² #1)."""
+    Base.metadata.drop_all(engine)
+    Base.metadata.create_all(engine)
+    s = Session(engine)()
+    ids = seed_parents(s)
+    s.close()
+    # Intermediate schema: fingerprint_version does not exist yet.
+    with engine.begin() as c:
+        c.execute(text("ALTER TABLE payment_attempt DROP COLUMN fingerprint_version"))
+
+    fpA = pa.intent_fingerprint(**_fields(ids, "manual", 1000, 1000, ""), version=1)   # v1
+    fpB = pa.intent_fingerprint(**_fields(ids, "manual", 2000, 2000, "1,2,3"), version=2)  # v2
+    _insert_intermediate(engine, ids, key="rowA", fp=fpA, ls="", expected=1000, subtotal=1000)
+    _insert_intermediate(engine, ids, key="rowB", fp=fpB, ls="1,2,3", expected=2000, subtotal=2000)
+    _insert_intermediate(engine, ids, key="rowC", fp="", ls="", expected=3000, subtotal=3000)  # empty fp
+
+    migrate.run(engine, strict=True)
+
+    check(_version_of(engine, "rowA")[0] == 1, "pre-selection row classified v1 (#1)")
+    check(_version_of(engine, "rowB")[0] == 2, "intermediate selection-aware row classified v2 (#1)")
+    vc = _version_of(engine, "rowC")
+    check(vc[0] == 1 and vc[1] != "", "empty-fingerprint row reconstructed to a v1 hash (#1)")
+
+    db = Session(engine)()
+
+    def attempt(key, expected, subtotal, item_ids=None):
+        return pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
+                                 staff_id=ids["staff_id"], expected_total_cents=expected,
+                                 subtotal_cents=subtotal, currency="CAD", item_ids=item_ids,
+                                 idempotency_key=key)
+
+    def conflicts(fn):
+        try:
+            fn(); return False
+        except pa.IdempotencyConflict:
+            return True
+
+    check(attempt("rowA", 1000, 1000).idempotency_key == "rowA", "v1 legacy retry returns the row (#1)")
+    check(conflicts(lambda: attempt("rowA", 9999, 9999)), "v1 row: changed amount conflicts (#1)")
+    check(attempt("rowB", 2000, 2000, item_ids=[1, 2, 3]).idempotency_key == "rowB",
+          "v2 row: same selection retry returns the row (#1)")
+    check(conflicts(lambda: attempt("rowB", 2000, 2000, item_ids=[1, 2, 4])),
+          "v2 row: different selection conflicts (#1)")
+    check(attempt("rowC", 3000, 3000).idempotency_key == "rowC", "reconstructed row retry returns it (#1)")
+    check(conflicts(lambda: attempt("rowC", 1, 1)), "reconstructed row: changed intent does not silently pass (#1)")
+    db.close()
+
+    again = migrate.run(engine, strict=True)   # rerun is idempotent (no rows left at version 0)
+    check(not any("classified" in a for a in again), "fingerprint backfill re-run is a no-op (#1)")
+
+
+def test_unverifiable_fingerprint_fails_strict(engine):
+    """A stored hash matching neither v1 nor v2 must fail closed, not be guessed (#1)."""
+    Base.metadata.drop_all(engine)
+    Base.metadata.create_all(engine)
+    s = Session(engine)()
+    ids = seed_parents(s)
+    s.close()
+    with engine.begin() as c:
+        c.execute(text("ALTER TABLE payment_attempt DROP COLUMN fingerprint_version"))
+    _insert_intermediate(engine, ids, key="bogus", fp="deadbeef" * 8, ls="", expected=1000, subtotal=1000)
+    raised = False
+    try:
+        migrate.run(engine, strict=True)
+    except migrate.MigrationError as exc:
+        raised = "neither v1 nor v2" in str(exc)
+    check(raised, "an unclassifiable stored fingerprint fails closed under strict (#1)")
+
+
 def test_line_selection_varchar_to_text_upgrade(engine):
     """A DB that ran the Slice-1 intermediate schema has line_selection VARCHAR(500);
     the migration must widen it to TEXT in place (slice-1-fix #2)."""
@@ -408,6 +509,8 @@ if __name__ == "__main__":
                test_card_terminal_instrument_backfilled, test_card_terminal_preserves_explicit_provider,
                test_hardening_is_idempotent, test_hardening_atomic_rollback,
                test_legacy_fingerprint_idempotency_survives_upgrade,
+               test_mixed_fingerprint_version_backfill,
+               test_unverifiable_fingerprint_fails_strict,
                test_line_selection_varchar_to_text_upgrade,
                test_nonnull_provider_refund_id_fails_strict,
                test_upgrade_blocks_on_duplicate_payment_id,
diff --git a/tests/test_settlement.py b/tests/test_settlement.py
index a10ef9a..073c014 100644
--- a/tests/test_settlement.py
+++ b/tests/test_settlement.py
@@ -138,7 +138,8 @@ def test_item_id_validation_is_strict():
     check(make(["2"], "b").line_selection == "2", "digit string accepted (#3)")
     for bad, label in ((True, "bool True"), (False, "bool False"), (1.9, "float"),
                        ("1.9", "decimal string"), (0, "zero"), (-1, "negative"),
-                       (object(), "object")):
+                       (object(), "object"), ("²", "unicode superscript-2"),
+                       ("٣", "arabic-indic digit")):
         raised = False
         try:
             make([bad], f"k_{label}")
```
