# Review Handoff #2 — Stage 2a/2b Blockers Resolved

**For:** external reviewer (ChatGPT). **Stage 2c is NOT started** (as instructed).
This handoff covers only the corrections requested in
`CLAUDE_STAGE_2AB_REVIEW_FEEDBACK.md` (items #1–#20), applied while the payment
abstraction is still additive and not wired into live money routes.

## Commit range
- **Base SHA:** `07ee4f1af26aa383b7de06377f18caa98b153e4b` (main)
- **HEAD SHA:** `a6addbe059660b478cc65d92a7d30ab9892df8a2`
- Branch: `fix/p0-security-and-payments`
- Four review commits on top of the two original Stage 2a/2b commits:
  - `1d88455` A/B — concurrency-safe attempts + separate refund lifecycle
  - `555dc62` C — provider contract generalization + key forwarding
  - `69860db` D — generic /readyz, Square config consistency
  - `a6addbe` E — real-FK fixtures + Postgres concurrency proofs

## `git status --short`
```
?? .github/                      (pre-existing untracked CI dir, unrelated)
?? REVIEW_HANDOFF.md             (this document)
?? restaurant-app-review.zip     (earlier audit archive, unrelated)
```
**No uncommitted source changes.** Everything under `app/` and `tests/` is
committed. The three untracked entries are non-source.

## Finding-by-finding status
| # | Severity | Finding | Status | Where |
|---|---|---|---|---|
| 1 | CRIT | Forward persisted idempotency key into Square charge | ✅ | `square.create_checkout(idempotency_key=…)`, `SquareTerminalProvider.charge` |
| 2 | CRIT | Atomic idempotent create under concurrency | ✅ | `payment_attempts.create_attempt` (catch `IntegrityError`, re-read) + PG test |
| 3 | CRIT | Reject key reuse with different intent | ✅ | `intent_fingerprint` + `IdempotencyConflict` |
| 4 | CRIT | Concurrency-safe transitions | ✅ | `transition()` compare-and-swap on expected status + PG test |
| 5 | CRIT | Uniqueness for external processor ids | ✅ | `UNIQUE(provider, provider_payment_id)` / `…checkout_id` + PG test |
| 6 | CRIT | Separate refund lifecycle | ✅ | new `RefundAttempt` model + `refund_attempts` service |
| 7 | HIGH | Expand provider contract | ✅ | `capabilities` set + `Capability` vocabulary |
| 8 | HIGH | Processor amount/currency in results | ✅ | `ChargeResult.processor_amount_cents/currency`, stored on attempt |
| 9 | HIGH | COMPLETED without payment id ≠ approval | ✅ | `SquareTerminalProvider.poll` → `REQUIRES_RECONCILIATION` |
| 10 | HIGH | cancel() must not swallow failures | ✅ | `CancelResult(requires_reconciliation=…)` |
| 11 | HIGH | Map all Square refund states | ✅ | `_map_square_refund` explicit table |
| 12 | HIGH | Restrict RECONCILIATION resolution | ✅ | `resolve_reconciliation()` requires evidence; plain `transition()` blocked |
| 13 | MED | Terminal-state semantics | ✅ | `SETTLED` terminal; refund states removed from charge attempt |
| 14 | HIGH | Remove unsafe default provider | ✅ | `provider` mandatory, validated against registry |
| 15 | MED | Normalize provider field lengths | ✅ | shared `PROVIDER_KEY_LEN` |
| 16 | HIGH | Postgres integration/concurrency tests | ✅ | `test_pg_concurrency.py` + real-FK fixture |
| 17 | HIGH | Concurrent (not just sequential) idempotency tests | ✅ | `test_concurrent_same_key_create` (8 threads) |
| 18 | MED | Clarify snapshot immutability | ✅ | docstring: "service-layer contract, not DB-enforced" |
| 19 | MED | /readyz must not expose raw exceptions | ✅ | generic 503, error logged server-side |
| 20 | LOW | Square APPLICATION_ID doc/validation | ✅ | documented optional/unvalidated; three-var server set |

## Schema changes
New table **`refund_attempt`** (id, payment_id→payment, charge_attempt_id→
payment_attempt, staff_id, provider, provider_refund_id, idempotency_key,
amount_cents, currency, status, last_error, refund_id→refund, reconciled_at,
reconciled_by, reconciliation_note, created_at, updated_at).
Constraints: `UNIQUE(idempotency_key)`, `UNIQUE(provider, provider_refund_id)`,
`CHECK(amount_cents > 0)`.

**`payment_attempt`** changed: `provider` now `NOT NULL` no default;
added `intent_fingerprint`, `processor_amount_cents`, `processor_currency`,
`reconciled_at`, `reconciled_by`, `reconciliation_note`; removed
`provider_refund_id`; added `UNIQUE(provider, provider_payment_id)` and
`UNIQUE(provider, provider_checkout_id)`. Existing-DB upgrade via additive
`migrate.py` ADD COLUMNs (fresh DBs get full schema + constraints from
`create_all`; the new UNIQUE constraints ship on fresh DBs).

## Exact state machines
**Charge (`PaymentAttemptStatus`)** — terminal: SETTLED, FAILED, CANCELLED.
```
CREATED              -> PROCESSOR_PENDING | CANCELLED | FAILED | REQUIRES_RECONCILIATION
PROCESSOR_PENDING    -> PROCESSOR_APPROVED | FAILED | CANCELLED | REQUIRES_RECONCILIATION
PROCESSOR_APPROVED   -> SETTLED | REQUIRES_RECONCILIATION
REQUIRES_RECONCILIATION -> SETTLED | FAILED | CANCELLED   (only via resolve_reconciliation, with evidence)
SETTLED | FAILED | CANCELLED -> (none)
```
**Refund (`RefundAttemptStatus`)** — terminal: COMPLETED, FAILED, REJECTED.
```
CREATED           -> PROCESSOR_PENDING | COMPLETED | FAILED | REJECTED | REQUIRES_RECONCILIATION
PROCESSOR_PENDING -> COMPLETED | FAILED | REJECTED | REQUIRES_RECONCILIATION
REQUIRES_RECONCILIATION -> COMPLETED | FAILED | REJECTED
COMPLETED | FAILED | REJECTED -> (none)
```

## Provider interface / capabilities
`PaymentProvider` (ABC): `charge()`, `poll()`, `refund()`, `cancel()`; class
attrs `key`, `label`, `is_external`, `capabilities: frozenset[str]`; derived
`needs_polling`. Results: `ChargeResult` (status + provider ids + processor
amount/currency + tip/card), `RefundResult` (RefundAttemptStatus + provider
refund id + external flag), `CancelResult` (ok / provider_status /
requires_reconciliation / error). Capabilities vocabulary: `polling, webhooks,
authorize, capture, partial_capture, refund, partial_refund, lookup`. Registry:
`register/get_provider/available`; unknown key raises `UnknownProvider`.
Adapters: `ManualProvider` (local, instant approve, ledger refund),
`SquareTerminalProvider` (polling + real Refunds API).

## Idempotency semantics
- Client key persisted on the attempt **before** the processor is called, and
  the **same** key is forwarded to Square (`create_checkout`) and to refunds.
- `create_attempt(idempotency_key=K)`: if an attempt with K exists and its
  `intent_fingerprint` matches → return it; if the fingerprint differs →
  `IdempotencyConflict`. Concurrent inserts race on `UNIQUE(idempotency_key)`;
  the loser catches `IntegrityError`, re-reads, and returns the winner's row.
- Fingerprint = sha256 of (provider, order_id, seat_id, staff_id, currency,
  expected_total, subtotal, tax, tip, service_charge, discount, surcharge).

## Transaction / concurrency strategy
- **Transitions are compare-and-swap:** one `UPDATE … WHERE id=:id AND
  status=:expected` (+ write-once `col IS NULL OR col=:val` guards). rowcount 1 =
  win; rowcount 0 = `TransitionConflict`. `IntegrityError`/`OperationalError`
  (unique clash, lock, deadlock) are also surfaced as `TransitionConflict` so a
  losing writer never leaks a raw driver error.
- **Under a race, a loser can legitimately observe one of two typed refusals**
  (both subclasses of `PaymentAttemptError`): a CAS `TransitionConflict`, or —
  if it re-read after the winner committed — an "illegal transition from
  terminal". The concurrency test asserts exactly one winner, two typed
  refusals, and an uncorrupted persisted status.
- One-Payment-per-attempt via `UNIQUE(payment_id)`; one-external-txn-per-attempt
  via `UNIQUE(provider, provider_payment_id/checkout_id)`.

## PostgreSQL vs SQLite notes
- Functional tests run on **both**: SQLite (file, `PRAGMA foreign_keys=ON`, real
  parent rows) by default; Postgres when `PG_TEST_DSN` is set.
- Concurrency proofs (`test_pg_concurrency.py`) require real row locking and
  **skip (exit 0) without Postgres** — SQLite green is not treated as concurrency
  proof (per #16). They were executed against Postgres 16 (see below).
- `NULL` stays distinct in `UNIQUE` on both engines, so many in-flight attempts
  with null provider/payment ids coexist while set values are unique.

## Tests executed (exact commands + results)
Environment: Python 3.14, SQLAlchemy 2.0.51, psycopg 3.3.4 (test-only),
Postgres 16-alpine in Docker at `localhost:5433`.

```
# SQLite (default):
python tests/test_config.py              -> 9 checks ok
python tests/test_payment_attempts.py    -> 15 checks ok
python tests/test_refund_attempts.py     -> 9 checks ok
python tests/test_payment_providers.py   -> 24 checks ok
python tests/test_pg_concurrency.py      -> SKIP (no PG_TEST_DSN)
python tests/test_templates.py           -> PASS (all templates compile)
python tests/test_security.py            -> PASS
python tests/test_admin.py               -> PASS
python tests/test_money.py               -> PASS
python tests/test_reconciliation.py      -> PASS
python tests/test_schedule.py            -> PASS

# PostgreSQL (PG_TEST_DSN=postgresql+psycopg://rms:rms@localhost:5433/rms_test):
python tests/test_payment_attempts.py    -> 15 checks ok
python tests/test_refund_attempts.py     -> 9 checks ok
python tests/test_pg_concurrency.py      -> 9 checks ok
```
**Totals:** payment-core explicit checks all green (SQLite 57 + PG 33, overlapping
suites) + 6 legacy suites pass by exit code. **0 failures.** Concurrency
stability: `test_pg_concurrency.py` ran **10/10** consecutive passes after the
transition hardening.

## Failure-injection matrix (proven now vs. deferred to 2c)
| Scenario | Proven now | By |
|---|---|---|
| Concurrent same idempotency key (create) | ✅ one row, all callers same id | `test_concurrent_same_key_create` (8 threads, PG) |
| Same key, different intent | ✅ explicit conflict | `test_same_key_different_intent_conflicts` |
| Concurrent conflicting transitions | ✅ one winner, losers refuse, no corruption | `test_concurrent_conflicting_transition` (PG) |
| Duplicate provider payment id | ✅ rejected | `test_provider_payment_id_unique` (PG) |
| One settlement per attempt | ✅ 2nd rejected | `test_one_settlement_per_attempt` (PG) |
| FK integrity | ✅ enforced | `test_fk_enforced` (PG) |
| Processor COMPLETED w/o payment id | ✅ → reconciliation | `test_square_completed_without_payment_id_reconciles` |
| Refund PENDING/COMPLETED/REJECTED/FAILED/unknown | ✅ explicit mapping | `test_square_refund_state_mapping` |
| Cancel API failure | ✅ → reconciliation, not swallowed | `test_cancel_is_not_swallowed` |
| **Processor success + DB write failure mid-settlement** | ⛔ deferred to 2c (needs live wiring) | — |
| **Crash between charge and settle / restart recovery** | ⛔ deferred to 2c/2d (recovery worker) | — |
| **Duplicate poll/callback settling twice** | partial (UNIQUE guards) — full proof in 2c | — |

## Areas I am least confident about
1. **Fingerprint field set (#3).** Once 2c introduces immutable selected-item/
   allocation identity, the fingerprint should include it; today it does not (no
   line identity exists yet). Please sanity-check the chosen field set.
2. **`resolve_reconciliation` authority.** It records `resolved_by` as free text
   and does not itself re-query the processor (`lookup`); it trusts the caller's
   evidence. Whether resolution should *require* a provider lookup result before
   settling is a policy call I deferred to 2c.
3. **CAS vs. SELECT FOR UPDATE.** I chose status-guarded compare-and-swap (uniform
   on SQLite and PG) over `FOR UPDATE`. Correct for single-row status moves; if 2c
   needs multi-row invariants (refundable-balance across sibling refunds) I expect
   to add explicit `FOR UPDATE` there.
4. **Refund over-balance protection** is only a running-total helper today
   (`refunded_and_pending_cents`); the transactional guard that rejects an
   over-refund belongs in 2c and is not yet enforced.
5. **psycopg version drift.** Prod pins `psycopg[binary]==3.2.3`; the test venv
   installed `3.3.4` (3.2.3 has no cp314 wheel). Only basic DBAPI is used, so low
   risk, but noting the mismatch.

---
## Full diff (`git diff main...HEAD`)
```diff
diff --git a/.env.example b/.env.example
index fd8448e..a0e9747 100644
--- a/.env.example
+++ b/.env.example
@@ -9,17 +9,23 @@ POSTGRES_USER=rms
 POSTGRES_PASSWORD=CHANGE_ME
 POSTGRES_DB=restaurant
 
+# --- Deployment environment ----------------------------------------------
+# "development" (default) allows the public dev session key for zero-config local
+# runs. Any other value (e.g. "production") makes the app FAIL CLOSED on a
+# missing SECRET_KEY or a partial Square config (see app/config.py).
+APP_ENV=production
+
 # --- First-run owner account ---------------------------------------------
 # Used once by `python -m app.bootstrap` to create the single owner login on a
 # fresh database. 4–8 digits. Change it after first sign-in under Manage.
 OWNER_PIN=CHANGE_ME
 
 # --- Session/PIN signing key ---------------------------------------------
-# Signs the login cookie and salts PIN hashes (app/security.py). REQUIRED in
-# production — set it to a long random string, e.g.:
+# Signs the login cookie and salts PIN hashes (app/security.py). REQUIRED when
+# APP_ENV is not development — set it to a long random string, e.g.:
 #   python -c "import secrets; print(secrets.token_urlsafe(48))"
-# Without it the app falls back to a public development key (fine locally, unsafe
-# in prod). Changing it simply signs everyone out on their next request.
+# In production a missing key aborts startup (no public-key fallback). Changing
+# it simply signs everyone out on their next request.
 SECRET_KEY=CHANGE_ME
 
 # Set to 1 in production (behind HTTPS, e.g. Render) so the session cookie is
@@ -31,6 +37,20 @@ SECRET_KEY=CHANGE_ME
 # venue's zone or "today" can be off by a day. Use a TZ database name.
 # TZ=America/Vancouver
 
+# --- Square terminal payments (app/services/square.py) --------------------
+# Cash-only venue? Leave these unset. To enable card payments set the THREE
+# server-side vars together — ACCESS_TOKEN + LOCATION_ID + DEVICE_ID — a partial
+# set aborts startup so card payments never silently fail.
+# SQUARE_APPLICATION_ID is optional and NOT validated: the server-side Terminal
+# flow never reads it (it's only a client-side SDK value); kept here for docs.
+SQUARE_ENV=sandbox
+SQUARE_ACCESS_TOKEN=
+SQUARE_LOCATION_ID=
+SQUARE_DEVICE_ID=
+# SQUARE_APPLICATION_ID=   # optional, client-SDK only
+# ISO currency charged at the terminal. Defaults to CAD.
+# SQUARE_CURRENCY=CAD
+
 # --- Optional -------------------------------------------------------------
 # Connection pool sizing (Postgres). Defaults shown.
 # DB_POOL_SIZE=10
diff --git a/REMEDIATION_PLAN.md b/REMEDIATION_PLAN.md
new file mode 100644
index 0000000..86106c7
--- /dev/null
+++ b/REMEDIATION_PLAN.md
@@ -0,0 +1,57 @@
+# Remediation Plan — Restaurant App
+
+Derived from the consolidated external audit (39 findings) and **verified against
+the current code** before scheduling. Executed on branch
+`fix/p0-security-and-payments`. Each stage is committed separately and stops for
+review. Financial correctness, payment recovery, concurrency safety, and
+production security are prioritized over features.
+
+**Correction carried in:** PINs are already PBKDF2-HMAC-SHA256 (200k rounds,
+salted) with legacy migration — this is a strength to preserve, NOT a bug. Any
+doc that says "plaintext PINs" is wrong.
+
+## Staging (cost-aware order)
+
+| Stage | Findings | Risk of change | Cost |
+|-------|----------|----------------|------|
+| **1 — Deploy/config fail-closed** | #16, #17, #18, #19, #20 | Low (no money paths) | small |
+| **2 — Payment durability core** | #1, #2, #3, #4, #5, #6 | High (money) | large |
+| **3 — Financial correctness** | #7, #8, #9, #10, #11, #22, #27 | Medium | medium |
+| **4 — Concurrency & business day** | #12, #13, #14, #15, #36 | Medium | medium |
+| **5 — Security hardening** | #29, #30, #31, #32 | Low/Med | small-med |
+| **6 — Validation & state machines** | #23, #24, #25, #26, #28, #34, #35 | Low/Med | medium |
+| **7 — Reliability/CI/tests** | #21, #33, #37, #38, #39 | Low | medium |
+| **P4 — Delivery adapters** | #34(plan) | — | deferred |
+
+Every fix ships with a regression test proving the prior failure mode, per the
+audit's acceptance criteria. Delivery-platform integrations are frozen until
+Stage 2–4 pass under failure injection.
+
+---
+
+## Stage 1 — Deploy/config fail-closed (in progress)
+
+- **#17** `SECRET_KEY`: stop returning the public `_DEV_SECRET` in production.
+  Add `APP_ENV` (default `development`). In production, a missing `SECRET_KEY`
+  raises at startup — fail closed.
+- **#16** `docker-compose.yml`: explicitly pass `SECRET_KEY` (required, `:?`),
+  `APP_ENV`, `COOKIE_SECURE`, `TZ`, and all `SQUARE_*` into the app container.
+- **#18** `docker-entrypoint.sh`: bootstrap failure is fatal — do not `|| echo …`
+  and continue.
+- **#19** `migrate.py`: in production run `strict=True`; a real ALTER failure
+  raises instead of being appended as `SKIPPED …` and swallowed.
+- **#20** split liveness/readiness: keep `/healthz` (liveness); add `/readyz`
+  that verifies DB connectivity + a core table exists.
+- New `app/config.py` centralizes `app_env()`, `is_production()`, and
+  `validate_startup_config()` (called from `main.py`).
+
+## Stage 2 — Payment durability core (next, needs your go-ahead)
+Durable immutable `PaymentAttempt` (order/seat/amounts/currency/idempotency +
+provider checkout/payment/refund IDs + status state machine), created and
+committed **before** contacting Square; lock+snapshot payable state before the
+attempt; idempotent settlement; recovery/reconciliation for processor-success +
+local-failure; real Square **Refunds API** for refund/void; refund locking to
+prevent over-refund. This is the real-money blocker set.
+
+## Stages 3–7
+As tabled above; detail expanded when each stage begins.
diff --git a/app/config.py b/app/config.py
new file mode 100644
index 0000000..a5e4ba7
--- /dev/null
+++ b/app/config.py
@@ -0,0 +1,77 @@
+"""Environment/config helpers and startup validation.
+
+Kept dependency-free (stdlib only) and importable by ``security.py`` without a
+cycle: this module never imports app code. ``validate_startup_config`` is called
+once from ``main.py`` so a misconfigured production process fails fast and loud
+rather than silently running with an insecure session key or half-configured
+payment provider.
+"""
+from __future__ import annotations
+
+import os
+
+# Environments treated as non-production (the public dev session key is allowed).
+_DEV_ENVS = {"development", "dev", "local", "test", "testing", "ci"}
+
+
+def app_env() -> str:
+    """The deployment environment, from ``APP_ENV``. Defaults to development so
+    the app still runs zero-config locally and in the test suite."""
+    return os.environ.get("APP_ENV", "development").strip().lower()
+
+
+def is_production() -> bool:
+    """True for any environment that is not explicitly a dev/test one. Used to
+    decide when to fail closed on missing secrets/config."""
+    return app_env() not in _DEV_ENVS
+
+
+class ConfigError(RuntimeError):
+    """Raised at startup when required production configuration is missing."""
+
+
+# Square is optional (a cash-only venue needs none), but a *partial* Square
+# config is a deployment mistake — it silently disables card payments. Require
+# all or nothing. Note: SQUARE_APPLICATION_ID is deliberately NOT here — the
+# server-side Terminal flow (app/services/square.py) never reads it; it is only
+# a client-side SDK value. So the required server set is exactly these three.
+_SQUARE_REQUIRED = (
+    "SQUARE_ACCESS_TOKEN",
+    "SQUARE_LOCATION_ID",
+    "SQUARE_DEVICE_ID",
+)
+
+
+def validate_startup_config() -> list[str]:
+    """Check production configuration. Raises ``ConfigError`` on a fatal problem;
+    returns a list of non-fatal warnings. A no-op outside production."""
+    warnings: list[str] = []
+    if not is_production():
+        return warnings
+
+    if not (os.environ.get("SECRET_KEY") or "").strip():
+        raise ConfigError(
+            "SECRET_KEY is required in production (APP_ENV=%s). Set it to a long "
+            "random value, e.g. python -c \"import secrets; "
+            "print(secrets.token_urlsafe(48))\"." % app_env()
+        )
+
+    if (os.environ.get("COOKIE_SECURE", "").strip() not in ("1", "true", "True")):
+        warnings.append(
+            "COOKIE_SECURE is not set — session cookies will be sent over plain "
+            "HTTP. Set COOKIE_SECURE=1 in production (behind HTTPS)."
+        )
+
+    square_set = [v for v in _SQUARE_REQUIRED if (os.environ.get(v) or "").strip()]
+    if square_set and len(square_set) != len(_SQUARE_REQUIRED):
+        missing = [v for v in _SQUARE_REQUIRED if v not in square_set]
+        raise ConfigError(
+            "Square is partially configured — card payments would silently fail. "
+            "Missing: " + ", ".join(missing) + ". Set all Square variables or none."
+        )
+    if not square_set:
+        warnings.append(
+            "Square is not configured (no SQUARE_ACCESS_TOKEN) — card/terminal "
+            "payments are unavailable; only cash/other instruments will work."
+        )
+    return warnings
diff --git a/app/main.py b/app/main.py
index 1dfb0e7..7c8c57e 100644
--- a/app/main.py
+++ b/app/main.py
@@ -29,17 +29,24 @@ from fastapi.staticfiles import StaticFiles
 from starlette.exceptions import HTTPException as StarletteHTTPException
 
 from app import migrate
+from app.config import is_production, validate_startup_config
 from app.database import Base, SessionLocal, engine
 from app.deps import WEB_DIR, templates
 from app.routers import admin, analytics, auth, pay, reservations, sales, schedule
 
 app = FastAPI(title="Restaurant Management System", version="1.0")
 
+# Fail closed on missing production config (SECRET_KEY, partial Square) before
+# doing anything else; log non-fatal warnings.
+for _warning in validate_startup_config():
+    print(f"[config] WARNING: {_warning}", flush=True)
+
 Base.metadata.create_all(engine)
 # create_all adds tables, never columns — see migrate.py. Log what changed so a
 # schema migration on deploy (esp. on Postgres/Render) is visible and verifiable
-# in the service logs rather than silent.
-_migrated = migrate.run(engine)
+# in the service logs rather than silent. In production a real migration failure
+# is fatal (strict) rather than silently skipped.
+_migrated = migrate.run(engine, strict=is_production())
 if _migrated:
     print(f"[migrate] applied: {', '.join(_migrated)}", flush=True)
 else:
@@ -93,4 +100,28 @@ def http_error(request: Request, exc: StarletteHTTPException):
 
 @app.get("/healthz", response_class=HTMLResponse)
 def healthz():
+    """Liveness: the process is up and serving. Does not touch the database, so
+    an orchestrator restarts a wedged process without being fooled by a slow DB."""
     return "ok"
+
+
+@app.get("/readyz", response_class=HTMLResponse)
+def readyz():
+    """Readiness: prove the app can actually serve traffic — the database is
+    reachable and the core schema exists. Returns 503 until it can, so a load
+    balancer/orchestrator does not route to an instance that cannot transact.
+
+    The response body is deliberately generic; the underlying error (SQL, driver,
+    host, schema) is logged server-side only, never returned to the caller."""
+    from sqlalchemy import text
+    try:
+        with engine.connect() as conn:
+            conn.execute(text("SELECT 1"))
+            # A core table proves migrations/bootstrap ran, not just that a DB
+            # socket answered.
+            conn.execute(text('SELECT 1 FROM staff LIMIT 1'))
+    except Exception:  # noqa: BLE001
+        import logging
+        logging.getLogger("readyz").exception("readiness check failed")
+        return HTMLResponse("not ready", status_code=503)
+    return "ready"
diff --git a/app/migrate.py b/app/migrate.py
index 58e5b2e..fd50944 100644
--- a/app/migrate.py
+++ b/app/migrate.py
@@ -82,6 +82,19 @@ ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
     # on both Postgres and SQLite, so the ADD COLUMN succeeds on the live DB.
     ("swap_request", "new_starts_at", "TIMESTAMP"),
     ("swap_request", "new_ends_at", "TIMESTAMP"),
+    # Pluggable payment providers. Defaulting to 'manual' leaves every existing
+    # instrument settling exactly as before (staff-recorded, no processor).
+    ("payment_instrument", "provider", "VARCHAR(30) NOT NULL DEFAULT 'manual'"),
+    # PaymentAttempt hardening (review Stages 2a/2b): intent fingerprint,
+    # processor-confirmed amount/currency, and reconciliation evidence. New
+    # UNIQUE(provider, provider_*_id) constraints ship on fresh databases via
+    # create_all; an already-created payment_attempt table only needs the columns.
+    ("payment_attempt", "intent_fingerprint", "VARCHAR(64) NOT NULL DEFAULT ''"),
+    ("payment_attempt", "processor_amount_cents", "INTEGER"),
+    ("payment_attempt", "processor_currency", "VARCHAR(3)"),
+    ("payment_attempt", "reconciled_at", "TIMESTAMP"),
+    ("payment_attempt", "reconciled_by", "VARCHAR(60) NOT NULL DEFAULT ''"),
+    ("payment_attempt", "reconciliation_note", "VARCHAR(300) NOT NULL DEFAULT ''"),
 )
 
 # (table, column, min_length, new DDL type). Columns whose type/length GREW
@@ -98,7 +111,12 @@ WIDENED_COLUMNS: tuple[tuple[str, str, int, str], ...] = (
 DEFAULT_FLOOR = "1st floor"
 
 
-def run(engine: Engine) -> list[str]:
+class MigrationError(RuntimeError):
+    """A schema migration failed. Raised in strict mode so deployment aborts
+    rather than starting the app against a mismatched schema."""
+
+
+def run(engine: Engine, strict: bool = False) -> list[str]:
     """Apply any missing columns, then backfill. Returns what changed.
 
     These additive migrations exist to evolve an *existing* database whose
@@ -108,9 +126,13 @@ def run(engine: Engine) -> list[str]:
     find every column present and do nothing — but an existing Postgres created
     before a column was added still needs it, which is what the Postgres branch
     handles (previously it was skipped, so new columns never reached prod).
+
+    ``strict`` (production): a genuine ALTER failure raises ``MigrationError``
+    instead of being recorded as ``SKIPPED …`` and swallowed, so the app never
+    starts expecting a newer schema than the database actually has.
     """
     if engine.dialect.name != "sqlite":
-        return _run_postgres(engine)
+        return _run_postgres(engine, strict=strict)
     applied: list[str] = []
     with engine.begin() as conn:
         for table, column, ddl in ADDED_COLUMNS:
@@ -128,7 +150,7 @@ def run(engine: Engine) -> list[str]:
     return applied
 
 
-def _run_postgres(engine: Engine) -> list[str]:
+def _run_postgres(engine: Engine, strict: bool = False) -> list[str]:
     """Additive ADD COLUMN for an existing Postgres database (Render).
 
     Only columns that are genuinely missing are added, checked against
@@ -166,7 +188,9 @@ def _run_postgres(engine: Engine) -> list[str]:
                     text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS {column} {ddl}')
                 )
             applied.append(f"{table}.{column}")
-        except Exception as exc:               # noqa: BLE001 — never block startup
+        except Exception as exc:               # noqa: BLE001
+            if strict:
+                raise MigrationError(f"failed to add {table}.{column}: {exc}") from exc
             applied.append(f"SKIPPED {table}.{column}: {exc}")
 
     # Widen any column that outgrew its original length (e.g. pin_code now holds
@@ -192,7 +216,9 @@ def _run_postgres(engine: Engine) -> list[str]:
                     text(f'ALTER TABLE "{table}" ALTER COLUMN {column} TYPE {ddl}')
                 )
             applied.append(f"widened {table}.{column} -> {ddl}")
-        except Exception as exc:               # noqa: BLE001 — never block startup
+        except Exception as exc:               # noqa: BLE001
+            if strict:
+                raise MigrationError(f"failed to widen {table}.{column}: {exc}") from exc
             applied.append(f"SKIPPED widen {table}.{column}: {exc}")
     return applied
 
diff --git a/app/models/oltp.py b/app/models/oltp.py
index 44df190..734ccd2 100644
--- a/app/models/oltp.py
+++ b/app/models/oltp.py
@@ -23,6 +23,11 @@ from sqlalchemy.orm import Mapped, mapped_column, relationship
 
 from app.database import Base
 
+# Shared length for a payment-provider registry key, used everywhere a provider
+# key is stored (PaymentInstrument, PaymentAttempt, RefundAttempt) so the column
+# definitions cannot drift apart.
+PROVIDER_KEY_LEN = 30
+
 
 # --------------------------------------------------------------------------
 # Reference / lookup data
@@ -584,6 +589,14 @@ class PaymentInstrument(Base):
     is_third_party: Mapped[bool] = mapped_column(Boolean, default=False)
     # UberEats/DoorDash are valid on delivery orders only (section 4.2.1).
     delivery_only: Mapped[bool] = mapped_column(Boolean, default=False)
+    # Which payment provider settles this instrument (app/services/payment_providers.py).
+    # "manual" = staff records it, no external processor (cash, e-transfer, keyed
+    # card, platform tender). "square_terminal", "stripe", … = an external adapter.
+    # Default manual (a real registered key) so every existing instrument keeps
+    # behaving exactly as before.
+    provider: Mapped[str] = mapped_column(
+        String(PROVIDER_KEY_LEN), nullable=False, default="manual"
+    )
 
 
 # --------------------------------------------------------------------------
@@ -907,6 +920,279 @@ class PaymentAllocation(Base):
     __table_args__ = (Index("ix_alloc_item", "order_item_id"),)
 
 
+class PaymentAttemptStatus:
+    """Lifecycle of a durable *charge* attempt (audit findings #1–#5).
+
+    An attempt is written and committed *before* the external processor is
+    contacted, so a processor success can never be lost if the browser, network,
+    or server dies before local settlement. Refunds are a *separate* lifecycle
+    (``RefundAttempt``): a charge attempt is terminal once SETTLED, so it no
+    longer carries refund state. Terminal states are SETTLED, FAILED, and
+    CANCELLED; REQUIRES_RECONCILIATION is the parking state a recovery worker/
+    human resolves when local and processor truth may disagree.
+    """
+    CREATED = "created"                       # persisted, processor not yet called
+    PROCESSOR_PENDING = "processor_pending"   # checkout sent to processor/terminal
+    PROCESSOR_APPROVED = "processor_approved" # processor reports captured/approved
+    SETTLED = "settled"                       # internal Payment written exactly once
+    FAILED = "failed"                         # processor declined / gave up
+    CANCELLED = "cancelled"                   # cancelled before capture
+    REQUIRES_RECONCILIATION = "requires_reconciliation"  # needs recovery/manual
+
+    # Terminal for the whole charge lifecycle. Refunds live on RefundAttempt.
+    TERMINAL = frozenset({SETTLED, FAILED, CANCELLED})
+
+
+# Allowed forward transitions for a charge attempt. Anything not listed is
+# rejected by services.payment_attempts.transition(), so an attempt can never
+# skip straight from CREATED to SETTLED without a recorded processor outcome, and
+# a terminal state cannot be reopened. REQUIRES_RECONCILIATION does NOT resolve
+# via a plain transition — see services.payment_attempts.resolve_reconciliation
+# (finding #12), which demands evidence.
+PAYMENT_ATTEMPT_TRANSITIONS: dict[str, set[str]] = {
+    PaymentAttemptStatus.CREATED: {
+        PaymentAttemptStatus.PROCESSOR_PENDING,
+        PaymentAttemptStatus.CANCELLED,
+        PaymentAttemptStatus.FAILED,
+        PaymentAttemptStatus.REQUIRES_RECONCILIATION,
+    },
+    PaymentAttemptStatus.PROCESSOR_PENDING: {
+        PaymentAttemptStatus.PROCESSOR_APPROVED,
+        PaymentAttemptStatus.FAILED,
+        PaymentAttemptStatus.CANCELLED,
+        PaymentAttemptStatus.REQUIRES_RECONCILIATION,
+    },
+    PaymentAttemptStatus.PROCESSOR_APPROVED: {
+        PaymentAttemptStatus.SETTLED,
+        PaymentAttemptStatus.REQUIRES_RECONCILIATION,
+    },
+    # Resolution out of reconciliation is gated by resolve_reconciliation(), not
+    # this table, so plain transition() cannot arbitrarily settle an ambiguous
+    # attempt. Listed here only so the resolver's own guarded move is legal.
+    PaymentAttemptStatus.REQUIRES_RECONCILIATION: {
+        PaymentAttemptStatus.SETTLED,
+        PaymentAttemptStatus.FAILED,
+        PaymentAttemptStatus.CANCELLED,
+    },
+    # Terminal — no outgoing transitions.
+    PaymentAttemptStatus.SETTLED: set(),
+    PaymentAttemptStatus.FAILED: set(),
+    PaymentAttemptStatus.CANCELLED: set(),
+}
+
+
+class RefundAttemptStatus:
+    """Lifecycle of a single durable *refund* against a Payment (finding #6).
+
+    A Payment can have many RefundAttempts (partial + repeated refunds), each
+    with its own idempotency key, provider refund id, amount, and status, so they
+    reconcile independently.
+    """
+    CREATED = "created"
+    PROCESSOR_PENDING = "processor_pending"   # sent to processor, not yet final
+    COMPLETED = "completed"                   # processor confirmed the reversal
+    FAILED = "failed"                         # processor error / gave up
+    REJECTED = "rejected"                     # processor declined the refund
+    REQUIRES_RECONCILIATION = "requires_reconciliation"
+
+    TERMINAL = frozenset({COMPLETED, FAILED, REJECTED})
+
+
+REFUND_ATTEMPT_TRANSITIONS: dict[str, set[str]] = {
+    RefundAttemptStatus.CREATED: {
+        RefundAttemptStatus.PROCESSOR_PENDING,
+        RefundAttemptStatus.COMPLETED,   # a manual/local refund settles at once
+        RefundAttemptStatus.FAILED,
+        RefundAttemptStatus.REJECTED,
+        RefundAttemptStatus.REQUIRES_RECONCILIATION,
+    },
+    RefundAttemptStatus.PROCESSOR_PENDING: {
+        RefundAttemptStatus.COMPLETED,
+        RefundAttemptStatus.FAILED,
+        RefundAttemptStatus.REJECTED,
+        RefundAttemptStatus.REQUIRES_RECONCILIATION,
+    },
+    RefundAttemptStatus.REQUIRES_RECONCILIATION: {
+        RefundAttemptStatus.COMPLETED,
+        RefundAttemptStatus.FAILED,
+        RefundAttemptStatus.REJECTED,
+    },
+    RefundAttemptStatus.COMPLETED: set(),
+    RefundAttemptStatus.FAILED: set(),
+    RefundAttemptStatus.REJECTED: set(),
+}
+
+
+class PaymentAttempt(Base):
+    """Durable charge attempt: a crash-safe record of an intent to charge.
+
+    Created and committed *before* contacting the processor, carrying the exact
+    payable snapshot (locked upstream), an idempotency key, and provider
+    identifiers, so that:
+
+    * a processor success is never lost if the local app fails mid-flow — a
+      recovery worker finds the attempt and completes/flags it;
+    * repeating the same processor completion produces exactly one internal
+      ``Payment`` (``payment_id`` is set once, under a unique constraint);
+    * a single external transaction can never map to two attempts
+      (``UNIQUE(provider, provider_payment_id)`` / ``…checkout_id``);
+    * every internal payment is traceable to a processor transaction.
+
+    Immutability of the amount snapshot is a **service-layer contract**, not a
+    database constraint: services.payment_attempts never rewrites these fields,
+    but the DB would accept a stray UPDATE. Only ``status``, provider IDs,
+    ``payment_id``, ``last_error``, reconciliation evidence, and ``updated_at``
+    change over the attempt's life. Refunds are a separate lifecycle
+    (``RefundAttempt``); a settled charge attempt is terminal.
+    """
+    __tablename__ = "payment_attempt"
+
+    id: Mapped[int] = mapped_column(primary_key=True)
+    order_id: Mapped[int] = mapped_column(
+        ForeignKey("order.id", ondelete="CASCADE"), nullable=False
+    )
+    seat_id: Mapped[int | None] = mapped_column(ForeignKey("seat.id"), nullable=True)
+    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
+
+    # Mandatory registry key — no silent default (finding #14). The service
+    # rejects an unregistered provider at create time.
+    provider: Mapped[str] = mapped_column(String(PROVIDER_KEY_LEN), nullable=False)
+    # Provider identifiers — write-once, then permanent.
+    provider_checkout_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
+    provider_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
+    # Client-generated idempotency key sent to the processor; also our dedupe key.
+    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
+    # Fingerprint of the immutable intent behind idempotency_key (finding #3), so
+    # reusing a key with different order/amount/currency is a conflict, not a
+    # silent wrong-attempt hit.
+    intent_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
+
+    # Immutable payable snapshot (integer cents), locked before creation.
+    subtotal_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
+    tax_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
+    tip_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
+    service_charge_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
+    discount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
+    surcharge_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
+    expected_total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
+    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CAD")
+
+    # Processor-confirmed amount/currency (finding #8), so settlement can verify
+    # the processor charged what the local snapshot expected.
+    processor_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
+    processor_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
+
+    status: Mapped[str] = mapped_column(
+        String(30), nullable=False, default=PaymentAttemptStatus.CREATED
+    )
+    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
+
+    # Reconciliation evidence (finding #12) — preserved when an ambiguous attempt
+    # is resolved, so the resolution is auditable and not a bare state flip.
+    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
+    reconciled_by: Mapped[str] = mapped_column(String(60), nullable=False, default="")
+    reconciliation_note: Mapped[str] = mapped_column(String(300), nullable=False, default="")
+
+    # Set exactly once when the attempt settles into a real Payment. The unique
+    # constraint guarantees one attempt maps to at most one Payment (idempotent
+    # settlement); NULLs stay distinct so many unsettled attempts coexist.
+    payment_id: Mapped[int | None] = mapped_column(
+        ForeignKey("payment.id"), nullable=True
+    )
+
+    created_at: Mapped[datetime] = mapped_column(
+        DateTime, default=datetime.now, nullable=False
+    )
+    updated_at: Mapped[datetime] = mapped_column(
+        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
+    )
+
+    order: Mapped["Order"] = relationship(lazy="joined")
+    seat: Mapped["Seat | None"] = relationship(lazy="joined")
+    staff: Mapped["Staff"] = relationship(lazy="joined", foreign_keys=[staff_id])
+    payment: Mapped["Payment | None"] = relationship(lazy="joined")
+    refunds: Mapped[list["RefundAttempt"]] = relationship(
+        back_populates="charge_attempt", lazy="selectin"
+    )
+
+    __table_args__ = (
+        # Idempotent create: a retried request with the same key finds the
+        # existing attempt instead of charging twice.
+        UniqueConstraint("idempotency_key", name="uq_attempt_idempotency_key"),
+        # One settled Payment per attempt.
+        UniqueConstraint("payment_id", name="uq_attempt_payment"),
+        # One external transaction maps to at most one attempt (finding #5).
+        # NULLs stay distinct on both SQLite and Postgres, so many in-flight
+        # attempts with no id yet coexist.
+        UniqueConstraint("provider", "provider_payment_id", name="uq_attempt_provider_payment"),
+        UniqueConstraint("provider", "provider_checkout_id", name="uq_attempt_provider_checkout"),
+        CheckConstraint("expected_total_cents >= 0", name="ck_attempt_total_nonneg"),
+        Index("ix_attempt_status", "status"),
+        Index("ix_attempt_order", "order_id"),
+    )
+
+
+class RefundAttempt(Base):
+    """Durable record of one refund against a Payment (finding #6).
+
+    A Payment can accumulate many of these — partial refunds, repeated refunds,
+    and retries — each with its own idempotency key, provider refund id, amount,
+    and independent status, so they charge exactly once and reconcile
+    independently. The refundable-balance invariant (never refund more than was
+    captured) is enforced transactionally in the service (Stage 2c).
+    """
+    __tablename__ = "refund_attempt"
+
+    id: Mapped[int] = mapped_column(primary_key=True)
+    payment_id: Mapped[int] = mapped_column(
+        ForeignKey("payment.id", ondelete="CASCADE"), nullable=False
+    )
+    # The charge attempt this refund reverses, when known (gives the provider
+    # payment id + traceability). Nullable for refunds of legacy payments that
+    # predate PaymentAttempt.
+    charge_attempt_id: Mapped[int | None] = mapped_column(
+        ForeignKey("payment_attempt.id"), nullable=True
+    )
+    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
+
+    provider: Mapped[str] = mapped_column(String(PROVIDER_KEY_LEN), nullable=False)
+    provider_refund_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
+    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
+
+    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
+    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CAD")
+
+    status: Mapped[str] = mapped_column(
+        String(30), nullable=False, default=RefundAttemptStatus.CREATED
+    )
+    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
+
+    # Links to the local Refund ledger row, set once when the refund is booked.
+    refund_id: Mapped[int | None] = mapped_column(ForeignKey("refund.id"), nullable=True)
+
+    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
+    reconciled_by: Mapped[str] = mapped_column(String(60), nullable=False, default="")
+    reconciliation_note: Mapped[str] = mapped_column(String(300), nullable=False, default="")
+
+    created_at: Mapped[datetime] = mapped_column(
+        DateTime, default=datetime.now, nullable=False
+    )
+    updated_at: Mapped[datetime] = mapped_column(
+        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
+    )
+
+    charge_attempt: Mapped["PaymentAttempt | None"] = relationship(back_populates="refunds")
+    staff: Mapped["Staff"] = relationship(lazy="joined", foreign_keys=[staff_id])
+
+    __table_args__ = (
+        UniqueConstraint("idempotency_key", name="uq_refund_idempotency_key"),
+        UniqueConstraint("provider", "provider_refund_id", name="uq_refund_provider_refund"),
+        CheckConstraint("amount_cents > 0", name="ck_refund_amount_pos"),
+        Index("ix_refund_attempt_payment", "payment_id"),
+        Index("ix_refund_attempt_status", "status"),
+    )
+
+
 class Receipt(Base):
     """Sections 4.2.3 / 4.2.4 / 4.2.7 — sub-receipt per guest, receipt per seat."""
     __tablename__ = "receipt"
diff --git a/app/routers/pay.py b/app/routers/pay.py
index bec4821..593455a 100644
--- a/app/routers/pay.py
+++ b/app/routers/pay.py
@@ -406,7 +406,8 @@ def _terminal_instrument(db: Session) -> PaymentInstrument:
     ).scalars().first()
     if inst is None:
         inst = PaymentInstrument(
-            code="card_terminal", name="Card (terminal)", instrument_type="card"
+            code="card_terminal", name="Card (terminal)", instrument_type="card",
+            provider="square_terminal",
         )
         db.add(inst)
         db.flush()
diff --git a/app/security.py b/app/security.py
index 624e720..432e9ff 100644
--- a/app/security.py
+++ b/app/security.py
@@ -26,6 +26,8 @@ import hmac
 import os
 import secrets
 
+from app.config import ConfigError, is_production
+
 # Public, non-secret default so local dev needs no configuration. Production
 # overrides it with a real SECRET_KEY; if it is ever left as this in prod, the
 # sessions are only as safe as a value printed in the source — hence the name.
@@ -35,7 +37,18 @@ _DEV_SECRET = "dev-insecure-secret-set-SECRET_KEY-in-production"
 def _secret() -> bytes:
     # Read at call time so a changed env var takes effect without touching import
     # order, mirroring how the Square client reads its config.
-    return (os.environ.get("SECRET_KEY") or _DEV_SECRET).encode("utf-8")
+    key = (os.environ.get("SECRET_KEY") or "").strip()
+    if key:
+        return key.encode("utf-8")
+    # Fail closed in production: never sign sessions with the public dev key.
+    # validate_startup_config() catches this at boot; this is the defence in
+    # depth if something reaches here anyway.
+    if is_production():
+        raise ConfigError(
+            "SECRET_KEY is required in production — refusing to sign sessions "
+            "with the public development key."
+        )
+    return _DEV_SECRET.encode("utf-8")
 
 
 def cookie_secure() -> bool:
diff --git a/app/services/payment_attempts.py b/app/services/payment_attempts.py
new file mode 100644
index 0000000..e7fdef7
--- /dev/null
+++ b/app/services/payment_attempts.py
@@ -0,0 +1,311 @@
+"""Durable payment-attempt lifecycle (audit findings #1–#5, hardened per review).
+
+A ``PaymentAttempt`` is the crash-safe spine of a charge: written and committed
+*before* the processor is contacted, then walked through an explicit state machine
+via concurrency-safe transitions. This module owns creation and every transition;
+nothing else mutates ``attempt.status`` directly.
+
+Concurrency/idempotency guarantees (Postgres and SQLite):
+
+* **Atomic create** — concurrent requests reusing one idempotency key resolve to
+  the *same* attempt; the DB unique constraint is caught and re-read, never
+  surfaced as an ``IntegrityError`` (finding #2).
+* **Intent fingerprint** — reusing a key with a *different* order/amount/currency
+  is an explicit conflict, not a silent wrong-attempt hit (finding #3).
+* **Compare-and-swap transitions** — a transition is a single guarded UPDATE
+  conditioned on the expected current status, so two conflicting transitions
+  cannot both win (finding #4).
+* **Reconciliation resolution** — leaving REQUIRES_RECONCILIATION demands
+  evidence and goes through ``resolve_reconciliation``, never a bare transition
+  (finding #12).
+"""
+from __future__ import annotations
+
+import hashlib
+import secrets
+from datetime import datetime
+
+from sqlalchemy import and_, or_, select, update
+from sqlalchemy.exc import IntegrityError, OperationalError
+from sqlalchemy.orm import Session
+
+from app.models.oltp import (
+    PAYMENT_ATTEMPT_TRANSITIONS,
+    PaymentAttempt,
+    PaymentAttemptStatus,
+)
+
+
+class PaymentAttemptError(RuntimeError):
+    """Illegal transition or a settlement invariant breach."""
+
+
+class IdempotencyConflict(PaymentAttemptError):
+    """An idempotency key was reused for a materially different intent."""
+
+
+class TransitionConflict(PaymentAttemptError):
+    """A concurrent writer moved the attempt out from under this transition."""
+
+
+def new_idempotency_key() -> str:
+    return secrets.token_hex(24)
+
+
+def intent_fingerprint(
+    *,
+    provider: str,
+    order_id: int,
+    seat_id: int | None,
+    staff_id: int,
+    currency: str,
+    expected_total_cents: int,
+    subtotal_cents: int,
+    tax_cents: int,
+    tip_cents: int,
+    service_charge_cents: int,
+    discount_cents: int,
+    surcharge_cents: int,
+) -> str:
+    """Stable hash of the immutable intent behind an idempotency key. Reusing a
+    key with a different fingerprint is rejected as a conflict."""
+    canonical = "|".join(str(x) for x in (
+        provider, order_id, seat_id, staff_id, currency.upper(),
+        expected_total_cents, subtotal_cents, tax_cents, tip_cents,
+        service_charge_cents, discount_cents, surcharge_cents,
+    ))
+    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:64]
+
+
+def _validate_provider(provider: str) -> None:
+    if not provider:
+        raise PaymentAttemptError("provider is required (no silent default).")
+    # Lazy import avoids any import-order coupling with the provider registry.
+    from app.services.payment_providers import UnknownProvider, get_provider
+    try:
+        get_provider(provider)
+    except UnknownProvider as exc:
+        raise PaymentAttemptError(str(exc)) from exc
+
+
+def _by_key(db: Session, key: str) -> PaymentAttempt | None:
+    return db.execute(
+        select(PaymentAttempt).where(PaymentAttempt.idempotency_key == key)
+    ).scalar_one_or_none()
+
+
+def _assert_same_intent(existing: PaymentAttempt, fingerprint: str) -> None:
+    if existing.intent_fingerprint and existing.intent_fingerprint != fingerprint:
+        raise IdempotencyConflict(
+            f"idempotency key {existing.idempotency_key!r} was already used for a "
+            f"different payment intent (attempt {existing.id})."
+        )
+
+
+def create_attempt(
+    db: Session,
+    *,
+    provider: str,
+    order_id: int,
+    staff_id: int,
+    expected_total_cents: int,
+    seat_id: int | None = None,
+    subtotal_cents: int = 0,
+    tax_cents: int = 0,
+    tip_cents: int = 0,
+    service_charge_cents: int = 0,
+    discount_cents: int = 0,
+    surcharge_cents: int = 0,
+    currency: str = "CAD",
+    idempotency_key: str | None = None,
+) -> PaymentAttempt:
+    """Persist a CREATED attempt from an already-locked payable snapshot. Commits.
+
+    Idempotent and concurrency-safe: a repeated key (sequential or concurrent)
+    returns the existing attempt; a repeated key with a different intent raises
+    ``IdempotencyConflict``; an unregistered provider is rejected.
+    """
+    _validate_provider(provider)
+    if expected_total_cents < 0:
+        raise PaymentAttemptError("expected_total_cents cannot be negative.")
+
+    fingerprint = intent_fingerprint(
+        provider=provider, order_id=order_id, seat_id=seat_id, staff_id=staff_id,
+        currency=currency, expected_total_cents=expected_total_cents,
+        subtotal_cents=subtotal_cents, tax_cents=tax_cents, tip_cents=tip_cents,
+        service_charge_cents=service_charge_cents, discount_cents=discount_cents,
+        surcharge_cents=surcharge_cents,
+    )
+
+    if idempotency_key:
+        existing = _by_key(db, idempotency_key)
+        if existing is not None:
+            _assert_same_intent(existing, fingerprint)
+            return existing
+    else:
+        idempotency_key = new_idempotency_key()
+
+    attempt = PaymentAttempt(
+        order_id=order_id, seat_id=seat_id, staff_id=staff_id, provider=provider,
+        idempotency_key=idempotency_key, intent_fingerprint=fingerprint,
+        subtotal_cents=subtotal_cents, tax_cents=tax_cents, tip_cents=tip_cents,
+        service_charge_cents=service_charge_cents, discount_cents=discount_cents,
+        surcharge_cents=surcharge_cents, expected_total_cents=expected_total_cents,
+        currency=currency, status=PaymentAttemptStatus.CREATED,
+    )
+    db.add(attempt)
+    try:
+        db.commit()
+    except IntegrityError:
+        # A concurrent request inserted the same idempotency key first. Re-read
+        # and return that attempt instead of surfacing the uniqueness violation.
+        db.rollback()
+        existing = _by_key(db, idempotency_key)
+        if existing is None:
+            raise
+        _assert_same_intent(existing, fingerprint)
+        return existing
+    db.refresh(attempt)
+    return attempt
+
+
+def transition(
+    db: Session,
+    attempt: PaymentAttempt,
+    new_status: str,
+    *,
+    provider_checkout_id: str | None = None,
+    provider_payment_id: str | None = None,
+    payment_id: int | None = None,
+    processor_amount_cents: int | None = None,
+    processor_currency: str | None = None,
+    last_error: str | None = None,
+    commit: bool = True,
+    _from_resolver: bool = False,
+) -> PaymentAttempt:
+    """Concurrency-safe compare-and-swap transition.
+
+    The move is a single UPDATE guarded on the expected current status, so a
+    racing writer cannot also succeed — the loser gets ``TransitionConflict``.
+    Provider identifiers and ``payment_id`` are write-once (guarded in the same
+    UPDATE). Leaving REQUIRES_RECONCILIATION must go through
+    ``resolve_reconciliation`` (finding #12).
+    """
+    if attempt.status == PaymentAttemptStatus.REQUIRES_RECONCILIATION and not _from_resolver:
+        raise PaymentAttemptError(
+            "resolve REQUIRES_RECONCILIATION via resolve_reconciliation(), not transition()."
+        )
+    allowed = PAYMENT_ATTEMPT_TRANSITIONS.get(attempt.status, set())
+    if new_status not in allowed:
+        raise PaymentAttemptError(
+            f"illegal transition {attempt.status} -> {new_status} "
+            f"(allowed: {sorted(allowed) or 'none — terminal state'})"
+        )
+    if new_status == PaymentAttemptStatus.SETTLED and payment_id is None:
+        raise PaymentAttemptError("settling an attempt requires a payment_id.")
+
+    expected = attempt.status
+    values: dict = {"status": new_status, "updated_at": datetime.now()}
+    if last_error is not None:
+        values["last_error"] = last_error
+    if processor_amount_cents is not None:
+        values["processor_amount_cents"] = processor_amount_cents
+    if processor_currency is not None:
+        values["processor_currency"] = processor_currency
+
+    conds = [PaymentAttempt.id == attempt.id, PaymentAttempt.status == expected]
+    for field, val in (
+        ("provider_checkout_id", provider_checkout_id),
+        ("provider_payment_id", provider_payment_id),
+        ("payment_id", payment_id),
+    ):
+        if val is not None:
+            values[field] = val
+            col = getattr(PaymentAttempt, field)
+            conds.append(or_(col.is_(None), col == val))  # write-once
+
+    # A uniqueness violation (duplicate payment/provider id) or a lock/deadlock
+    # under concurrency both mean this transition did not win — surface either as
+    # an explicit TransitionConflict, never a leaked driver error.
+    try:
+        applied = db.execute(
+            update(PaymentAttempt).where(and_(*conds)).values(**values)
+        ).rowcount
+        if applied == 1 and commit:
+            db.commit()
+    except (IntegrityError, OperationalError) as exc:
+        db.rollback()
+        raise TransitionConflict(
+            f"transition {expected} -> {new_status} conflicted "
+            f"(uniqueness or lock): {getattr(exc, 'orig', exc)}"
+        ) from exc
+    if applied != 1:
+        db.rollback()
+        try:
+            fresh = db.get(PaymentAttempt, attempt.id)
+            actual = fresh.status if fresh else "<deleted>"
+        except Exception:  # noqa: BLE001 — never mask the conflict with a read error
+            actual = "<unknown>"
+        raise TransitionConflict(
+            f"transition {expected} -> {new_status} did not apply "
+            f"(current status is {actual!r}, or a write-once id differs)."
+        )
+    db.refresh(attempt)
+    return attempt
+
+
+def resolve_reconciliation(
+    db: Session,
+    attempt: PaymentAttempt,
+    *,
+    resolved_status: str,
+    resolved_by: str,
+    note: str,
+    payment_id: int | None = None,
+    provider_payment_id: str | None = None,
+) -> PaymentAttempt:
+    """Resolve an ambiguous attempt with recorded evidence (finding #12).
+
+    Only valid from REQUIRES_RECONCILIATION. Requires a non-empty ``resolved_by``
+    and ``note``; settling additionally requires a ``payment_id``. The evidence is
+    persisted alongside the state change so the resolution is auditable, never a
+    bare flip.
+    """
+    if attempt.status != PaymentAttemptStatus.REQUIRES_RECONCILIATION:
+        raise PaymentAttemptError(
+            "resolve_reconciliation only applies to a REQUIRES_RECONCILIATION attempt."
+        )
+    if resolved_status not in (
+        PaymentAttemptStatus.SETTLED,
+        PaymentAttemptStatus.FAILED,
+        PaymentAttemptStatus.CANCELLED,
+    ):
+        raise PaymentAttemptError(f"cannot resolve reconciliation to {resolved_status!r}.")
+    if not (resolved_by or "").strip() or not (note or "").strip():
+        raise PaymentAttemptError("reconciliation resolution requires resolved_by and a note.")
+
+    attempt.reconciled_at = datetime.now()
+    attempt.reconciled_by = resolved_by.strip()[:60]
+    attempt.reconciliation_note = note.strip()[:300]
+    return transition(
+        db, attempt, resolved_status,
+        payment_id=payment_id, provider_payment_id=provider_payment_id,
+        _from_resolver=True,
+    )
+
+
+def requires_reconciliation(db: Session) -> list[PaymentAttempt]:
+    """Charge attempts a recovery worker/human must resolve — flagged, or
+    approved by the processor but never settled locally (the 'processor charged,
+    app lost it' case). Consumed by the Stage 2d recovery worker."""
+    stuck = {
+        PaymentAttemptStatus.REQUIRES_RECONCILIATION,
+        PaymentAttemptStatus.PROCESSOR_APPROVED,
+    }
+    return list(
+        db.execute(
+            select(PaymentAttempt)
+            .where(PaymentAttempt.status.in_(stuck))
+            .order_by(PaymentAttempt.created_at)
+        ).scalars()
+    )
diff --git a/app/services/payment_providers.py b/app/services/payment_providers.py
new file mode 100644
index 0000000..37fb7b8
--- /dev/null
+++ b/app/services/payment_providers.py
@@ -0,0 +1,292 @@
+"""Pluggable payment providers (hardened per review).
+
+The durable ``PaymentAttempt``/``RefundAttempt`` state machines are the neutral
+spine; each real-world payment method is a small adapter implementing
+``PaymentProvider`` and registered by a string key. ``PaymentInstrument.provider``
+names which adapter settles an instrument.
+
+Adding a brand-new machine/processor for a new client is:
+
+    1. subclass PaymentProvider (charge/poll/refund/cancel + a capabilities set),
+    2. register(MyProvider()),
+    3. set instrument.provider = "my_key".
+
+No change to routers, settlement, refunds, or the state machines.
+
+Capabilities (finding #7) let the settlement core ask what a provider supports
+(polling, webhooks, auth/capture split, partial capture/refund, lookup) instead
+of assuming Square-terminal shape. Results carry the processor-confirmed amount
+and currency (finding #8) so settlement can verify or reconcile against the local
+snapshot, and every provider outcome maps to an explicit
+``PaymentAttemptStatus``/``RefundAttemptStatus`` — including the ambiguous cases
+(finding #9/#10/#11).
+"""
+from __future__ import annotations
+
+from abc import ABC, abstractmethod
+from dataclasses import dataclass, field
+
+from app.models.oltp import PaymentAttemptStatus, RefundAttemptStatus
+from app.services import square
+
+
+# --------------------------------------------------------------------------
+# Capability vocabulary (finding #7)
+# --------------------------------------------------------------------------
+
+class Capability:
+    POLLING = "polling"
+    WEBHOOKS = "webhooks"
+    AUTHORIZE = "authorize"          # auth without capture
+    CAPTURE = "capture"              # later capture of an auth
+    PARTIAL_CAPTURE = "partial_capture"
+    REFUND = "refund"
+    PARTIAL_REFUND = "partial_refund"
+    LOOKUP = "lookup"                # provider-side reconciliation lookup
+
+
+# --------------------------------------------------------------------------
+# Result value objects — provider-neutral, in the attempts' vocabulary
+# --------------------------------------------------------------------------
+
+@dataclass
+class ChargeResult:
+    """Outcome of asking a provider to charge. ``status`` is a
+    ``PaymentAttemptStatus`` value fed straight into the state machine."""
+    status: str
+    provider_checkout_id: str | None = None
+    provider_payment_id: str | None = None
+    # Processor-confirmed amount/currency (finding #8) for settlement to verify.
+    processor_amount_cents: int | None = None
+    processor_currency: str | None = None
+    tip_cents: int = 0
+    card_brand: str | None = None
+    card_last4: str | None = None
+    error: str = ""
+
+
+@dataclass
+class RefundResult:
+    status: str                       # a RefundAttemptStatus value
+    provider_refund_id: str | None = None
+    external: bool = False            # did an external processor reverse funds?
+    error: str = ""
+
+    @property
+    def ok(self) -> bool:
+        return self.status in (RefundAttemptStatus.COMPLETED,
+                               RefundAttemptStatus.PROCESSOR_PENDING)
+
+
+@dataclass
+class CancelResult:
+    """Outcome of a pre-capture cancel (finding #10). Never silently swallowed:
+    an ambiguous/failed cancel flags for reconciliation instead of assuming
+    success."""
+    ok: bool
+    provider_status: str = ""
+    requires_reconciliation: bool = False
+    error: str = ""
+
+
+# --------------------------------------------------------------------------
+# The interface
+# --------------------------------------------------------------------------
+
+class PaymentProvider(ABC):
+    key: str = ""
+    label: str = ""
+    is_external: bool = False
+    capabilities: frozenset[str] = frozenset()
+
+    @property
+    def needs_polling(self) -> bool:
+        return Capability.POLLING in self.capabilities
+
+    def is_configured(self) -> bool:
+        return True
+
+    @abstractmethod
+    def charge(self, *, amount_cents: int, currency: str, idempotency_key: str,
+               reference: str = "", note: str = "", tip_cents: int = 0) -> ChargeResult:
+        ...
+
+    def poll(self, provider_checkout_id: str) -> ChargeResult:
+        raise NotImplementedError(f"{self.key} does not support polling")
+
+    @abstractmethod
+    def refund(self, *, amount_cents: int, currency: str, idempotency_key: str,
+               provider_payment_id: str | None = None) -> RefundResult:
+        ...
+
+    def cancel(self, *, provider_checkout_id: str) -> CancelResult:
+        return CancelResult(ok=True)
+
+
+# --------------------------------------------------------------------------
+# Adapters
+# --------------------------------------------------------------------------
+
+class ManualProvider(PaymentProvider):
+    """No external processor: staff attest the money moved. Charge is approved
+    immediately; refund is a local ledger reversal. Covers cash, e-transfer,
+    keyed cards, and platform tenders."""
+    key = "manual"
+    label = "Manual / cash"
+    is_external = False
+    capabilities = frozenset({Capability.REFUND, Capability.PARTIAL_REFUND})
+
+    def charge(self, *, amount_cents, currency, idempotency_key,
+               reference="", note="", tip_cents=0) -> ChargeResult:
+        return ChargeResult(
+            status=PaymentAttemptStatus.PROCESSOR_APPROVED,
+            processor_amount_cents=amount_cents, processor_currency=currency,
+            tip_cents=tip_cents,
+        )
+
+    def refund(self, *, amount_cents, currency, idempotency_key,
+               provider_payment_id=None) -> RefundResult:
+        # Nothing to call — the local Refund ledger entry is the reversal.
+        return RefundResult(status=RefundAttemptStatus.COMPLETED, external=False)
+
+
+class SquareTerminalProvider(PaymentProvider):
+    """Square card terminal: asynchronous card-present charge with tip on the
+    machine, plus a real Refunds API reversal."""
+    key = "square_terminal"
+    label = "Square terminal"
+    is_external = True
+    capabilities = frozenset({
+        Capability.POLLING, Capability.CAPTURE, Capability.REFUND,
+        Capability.PARTIAL_REFUND, Capability.LOOKUP,
+    })
+
+    def is_configured(self) -> bool:
+        return square.is_configured()
+
+    def charge(self, *, amount_cents, currency, idempotency_key,
+               reference="", note="", tip_cents=0) -> ChargeResult:
+        try:
+            checkout = square.create_checkout(
+                amount_cents, reference_id=reference, note=note,
+                idempotency_key=idempotency_key,   # finding #1: forward the key
+            )
+        except square.SquareError as exc:
+            return ChargeResult(status=PaymentAttemptStatus.FAILED, error=str(exc))
+        return ChargeResult(
+            status=PaymentAttemptStatus.PROCESSOR_PENDING,
+            provider_checkout_id=checkout.get("id"),
+        )
+
+    def poll(self, provider_checkout_id: str) -> ChargeResult:
+        try:
+            checkout = square.get_checkout(provider_checkout_id)
+        except square.SquareError as exc:
+            return ChargeResult(status=PaymentAttemptStatus.PROCESSOR_PENDING,
+                                provider_checkout_id=provider_checkout_id, error=str(exc))
+        status = checkout.get("status")
+        if status == square.COMPLETED:
+            tip_cents, brand, last4 = square.tip_and_card(checkout)
+            payment_ids = checkout.get("payment_ids") or []
+            if not payment_ids:
+                # Completed but no authoritative payment id — cannot reconcile,
+                # refund, or link. Do NOT treat as ordinary approval (finding #9).
+                return ChargeResult(
+                    status=PaymentAttemptStatus.REQUIRES_RECONCILIATION,
+                    provider_checkout_id=provider_checkout_id,
+                    error="Square COMPLETED without a payment id",
+                )
+            amount = _completed_amount(checkout)
+            return ChargeResult(
+                status=PaymentAttemptStatus.PROCESSOR_APPROVED,
+                provider_checkout_id=provider_checkout_id,
+                provider_payment_id=payment_ids[0],
+                processor_amount_cents=amount,
+                processor_currency=(square.currency()),
+                tip_cents=tip_cents, card_brand=brand, card_last4=last4,
+            )
+        if status == square.CANCELED:
+            return ChargeResult(status=PaymentAttemptStatus.CANCELLED,
+                                provider_checkout_id=provider_checkout_id)
+        return ChargeResult(status=PaymentAttemptStatus.PROCESSOR_PENDING,
+                            provider_checkout_id=provider_checkout_id)
+
+    def refund(self, *, amount_cents, currency, idempotency_key,
+               provider_payment_id=None) -> RefundResult:
+        if not provider_payment_id:
+            return RefundResult(status=RefundAttemptStatus.REQUIRES_RECONCILIATION,
+                                external=True,
+                                error="no Square payment id to refund against")
+        try:
+            refund = square.create_refund(provider_payment_id, amount_cents, idempotency_key)
+        except square.SquareError as exc:
+            # Transport/API failure: unknown processor outcome → reconcile.
+            return RefundResult(status=RefundAttemptStatus.REQUIRES_RECONCILIATION,
+                                external=True, error=str(exc))
+        # Explicit mapping of every known Square refund state (finding #11).
+        return _map_square_refund(refund)
+
+    def cancel(self, *, provider_checkout_id: str) -> CancelResult:
+        try:
+            checkout = square.cancel_checkout(provider_checkout_id)
+        except square.SquareError as exc:
+            # Do not swallow: an ambiguous cancel must be reconciled (finding #10).
+            return CancelResult(ok=False, requires_reconciliation=True, error=str(exc))
+        return CancelResult(ok=True, provider_status=checkout.get("status", ""))
+
+
+def _completed_amount(checkout: dict) -> int | None:
+    money = checkout.get("amount_money") or {}
+    amt = money.get("amount")
+    return int(amt) if amt is not None else None
+
+
+_SQUARE_REFUND_MAP = {
+    square.REFUND_COMPLETED: (RefundAttemptStatus.COMPLETED, ""),
+    square.REFUND_PENDING: (RefundAttemptStatus.PROCESSOR_PENDING, ""),
+    square.REFUND_REJECTED: (RefundAttemptStatus.REJECTED, "Square rejected the refund"),
+    square.REFUND_FAILED: (RefundAttemptStatus.FAILED, "Square refund failed"),
+}
+
+
+def _map_square_refund(refund: dict) -> RefundResult:
+    status = refund.get("status")
+    mapped, err = _SQUARE_REFUND_MAP.get(
+        status, (RefundAttemptStatus.REQUIRES_RECONCILIATION, f"unknown Square refund status {status!r}")
+    )
+    return RefundResult(status=mapped, provider_refund_id=refund.get("id"),
+                        external=True, error=err)
+
+
+# --------------------------------------------------------------------------
+# Registry
+# --------------------------------------------------------------------------
+
+class UnknownProvider(KeyError):
+    """Raised when an instrument names a provider that isn't registered."""
+
+
+_REGISTRY: dict[str, PaymentProvider] = {}
+
+
+def register(provider: PaymentProvider) -> None:
+    if not provider.key:
+        raise ValueError("payment provider must define a non-empty key")
+    _REGISTRY[provider.key] = provider
+
+
+def get_provider(key: str) -> PaymentProvider:
+    try:
+        return _REGISTRY[key]
+    except KeyError as exc:
+        raise UnknownProvider(
+            f"no payment provider registered for {key!r} (known: {sorted(_REGISTRY)})"
+        ) from exc
+
+
+def available() -> list[PaymentProvider]:
+    return list(_REGISTRY.values())
+
+
+register(ManualProvider())
+register(SquareTerminalProvider())
diff --git a/app/services/refund_attempts.py b/app/services/refund_attempts.py
new file mode 100644
index 0000000..fd2cedd
--- /dev/null
+++ b/app/services/refund_attempts.py
@@ -0,0 +1,160 @@
+"""Durable refund-attempt lifecycle (finding #6).
+
+A Payment can accumulate many refunds — partial, repeated, and retried — so each
+is its own ``RefundAttempt`` with an independent idempotency key, provider refund
+id, amount, and status. Mirrors payment_attempts' concurrency guarantees: atomic
+idempotent create and compare-and-swap transitions.
+
+The refundable-balance invariant (the sum of a payment's refunds never exceeds
+what it captured) is enforced transactionally where refunds are initiated in
+Stage 2c; this module provides the durable records and the running total.
+"""
+from __future__ import annotations
+
+import secrets
+from datetime import datetime
+
+from sqlalchemy import and_, func, or_, select, update
+from sqlalchemy.exc import IntegrityError, OperationalError
+from sqlalchemy.orm import Session
+
+from app.models.oltp import (
+    REFUND_ATTEMPT_TRANSITIONS,
+    RefundAttempt,
+    RefundAttemptStatus,
+)
+from app.services.payment_attempts import (
+    PaymentAttemptError,
+    TransitionConflict,
+    _validate_provider,
+)
+
+
+def new_idempotency_key() -> str:
+    return secrets.token_hex(24)
+
+
+# Refund states that still count against the refundable balance (money that has
+# gone back or is on its way). REJECTED/FAILED free the amount up again.
+COUNTS_AGAINST_BALANCE = (
+    RefundAttemptStatus.CREATED,
+    RefundAttemptStatus.PROCESSOR_PENDING,
+    RefundAttemptStatus.COMPLETED,
+    RefundAttemptStatus.REQUIRES_RECONCILIATION,
+)
+
+
+def refunded_and_pending_cents(db: Session, payment_id: int) -> int:
+    """Sum of a payment's refunds that are completed or still in flight, used to
+    protect the refundable balance before creating another refund."""
+    return db.execute(
+        select(func.coalesce(func.sum(RefundAttempt.amount_cents), 0)).where(
+            RefundAttempt.payment_id == payment_id,
+            RefundAttempt.status.in_(COUNTS_AGAINST_BALANCE),
+        )
+    ).scalar_one()
+
+
+def create_refund_attempt(
+    db: Session,
+    *,
+    payment_id: int,
+    staff_id: int,
+    provider: str,
+    amount_cents: int,
+    currency: str = "CAD",
+    charge_attempt_id: int | None = None,
+    idempotency_key: str | None = None,
+) -> RefundAttempt:
+    """Persist a CREATED refund attempt. Commits. Idempotent and concurrency-safe
+    on the idempotency key (a repeat resolves to the same row)."""
+    _validate_provider(provider)
+    if amount_cents <= 0:
+        raise PaymentAttemptError("refund amount must be positive.")
+
+    if idempotency_key:
+        existing = _by_key(db, idempotency_key)
+        if existing is not None:
+            return existing
+    else:
+        idempotency_key = new_idempotency_key()
+
+    refund = RefundAttempt(
+        payment_id=payment_id, charge_attempt_id=charge_attempt_id,
+        staff_id=staff_id, provider=provider, amount_cents=amount_cents,
+        currency=currency, idempotency_key=idempotency_key,
+        status=RefundAttemptStatus.CREATED,
+    )
+    db.add(refund)
+    try:
+        db.commit()
+    except IntegrityError:
+        db.rollback()
+        existing = _by_key(db, idempotency_key)
+        if existing is None:
+            raise
+        return existing
+    db.refresh(refund)
+    return refund
+
+
+def _by_key(db: Session, key: str) -> RefundAttempt | None:
+    return db.execute(
+        select(RefundAttempt).where(RefundAttempt.idempotency_key == key)
+    ).scalar_one_or_none()
+
+
+def transition_refund(
+    db: Session,
+    refund: RefundAttempt,
+    new_status: str,
+    *,
+    provider_refund_id: str | None = None,
+    refund_id: int | None = None,
+    last_error: str | None = None,
+    commit: bool = True,
+) -> RefundAttempt:
+    """Concurrency-safe compare-and-swap transition for a refund attempt."""
+    allowed = REFUND_ATTEMPT_TRANSITIONS.get(refund.status, set())
+    if new_status not in allowed:
+        raise PaymentAttemptError(
+            f"illegal refund transition {refund.status} -> {new_status} "
+            f"(allowed: {sorted(allowed) or 'none — terminal state'})"
+        )
+    expected = refund.status
+    values: dict = {"status": new_status, "updated_at": datetime.now()}
+    if last_error is not None:
+        values["last_error"] = last_error
+
+    conds = [RefundAttempt.id == refund.id, RefundAttempt.status == expected]
+    for field, val in (("provider_refund_id", provider_refund_id), ("refund_id", refund_id)):
+        if val is not None:
+            values[field] = val
+            col = getattr(RefundAttempt, field)
+            conds.append(or_(col.is_(None), col == val))  # write-once
+
+    try:
+        applied = db.execute(
+            update(RefundAttempt).where(and_(*conds)).values(**values)
+        ).rowcount
+        if applied == 1 and commit:
+            db.commit()
+    except (IntegrityError, OperationalError) as exc:
+        db.rollback()
+        raise TransitionConflict(
+            f"refund transition {expected} -> {new_status} conflicted "
+            f"(uniqueness or lock): {getattr(exc, 'orig', exc)}"
+        ) from exc
+    if applied != 1:
+        db.rollback()
+        try:
+            fresh = db.get(RefundAttempt, refund.id)
+            actual = fresh.status if fresh else "<deleted>"
+        except Exception:  # noqa: BLE001
+            actual = "<unknown>"
+        raise TransitionConflict(
+            f"refund transition {expected} -> {new_status} did not apply "
+            f"(current status is {actual!r}, or a write-once id differs)."
+        )
+    db.refresh(refund)
+    return refund
diff --git a/app/services/square.py b/app/services/square.py
index 3809d81..4aca9d2 100644
--- a/app/services/square.py
+++ b/app/services/square.py
@@ -130,12 +130,18 @@ def create_checkout(
     note: str = "",
     allow_tip: bool = True,
     device_id: str | None = None,
+    idempotency_key: str | None = None,
 ) -> dict:
     """Send an amount to the terminal for the customer to pay + tip on.
 
     amount_cents is the pre-tip total to charge (items + tax + any service
     charge). The terminal adds the tip on top. Returns the created checkout
     (status PENDING); poll get_checkout / wait_for_checkout for the result.
+
+    ``idempotency_key`` is the caller's durable key (the PaymentAttempt's), so a
+    retry after a crash reaches Square with the *same* key and cannot double
+    charge. Only when no key is supplied do we mint one (never for a real
+    attempt-backed charge).
     """
     # tip_settings lives inside device_options (DeviceCheckoutOptions) — that's
     # what drives the tip prompt shown to the customer on the terminal.
@@ -149,7 +155,7 @@ def create_checkout(
     else:
         device_options["tip_settings"] = {"allow_tipping": False}
     body = {
-        "idempotency_key": str(uuid.uuid4()),
+        "idempotency_key": idempotency_key or str(uuid.uuid4()),
         "checkout": {
             "amount_money": {"amount": int(amount_cents), "currency": currency()},
             "device_options": device_options,
@@ -179,6 +185,37 @@ def get_payment(payment_id: str) -> dict:
     return _request("GET", f"/v2/payments/{payment_id}")["payment"]
 
 
+# Refund lifecycle (Square). COMPLETED means the money is on its way back.
+REFUND_PENDING = "PENDING"
+REFUND_COMPLETED = "COMPLETED"
+REFUND_REJECTED = "REJECTED"
+REFUND_FAILED = "FAILED"
+
+
+def create_refund(
+    payment_id: str,
+    amount_cents: int,
+    idempotency_key: str,
+    reason: str = "",
+) -> dict:
+    """Reverse money to the card via the Square Refunds API (RefundPayment).
+
+    ``idempotency_key`` makes the call safe to retry: Square returns the same
+    refund for a repeated key instead of refunding twice. Returns the refund dict
+    (``id``, ``status`` — PENDING/COMPLETED/REJECTED/FAILED). Raises SquareError
+    on transport/API failure so the caller can flag the attempt for reconciliation
+    rather than marking it refunded.
+    """
+    body = {
+        "idempotency_key": idempotency_key,
+        "payment_id": payment_id,
+        "amount_money": {"amount": int(amount_cents), "currency": currency()},
+    }
+    if reason:
+        body["reason"] = reason[:192]
+    return _request("POST", "/v2/refunds", body)["refund"]
+
+
 def wait_for_checkout(checkout_id: str, timeout_s: float = 120.0, interval_s: float = 1.5) -> dict:
     """Poll until the checkout completes, cancels, or we give up.
 
diff --git a/docker-compose.yml b/docker-compose.yml
index de95949..8cafd8e 100644
--- a/docker-compose.yml
+++ b/docker-compose.yml
@@ -30,6 +30,25 @@ services:
       DATABASE_URL: postgresql+psycopg://${POSTGRES_USER:-rms}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB:-restaurant}
       # Used only to seed the owner on a brand-new database (idempotent).
       OWNER_PIN: ${OWNER_PIN:-}
+      # This compose file is the on-prem PRODUCTION stack, so fail closed on a
+      # missing session key rather than falling back to the public dev key.
+      APP_ENV: ${APP_ENV:-production}
+      # Required: `docker compose up` refuses to start without it. Generate with
+      #   python -c "import secrets; print(secrets.token_urlsafe(48))"
+      SECRET_KEY: ${SECRET_KEY:?set SECRET_KEY in .env}
+      # Caddy terminates HTTPS in front of the app, so the session cookie is
+      # HTTPS-only by default here.
+      COOKIE_SECURE: ${COOKIE_SECURE:-1}
+      TZ: ${TZ:-America/Vancouver}
+      # Square terminal payments. Leave the four blanks unset for a cash-only
+      # venue; set all of them together to enable card payments (a partial set
+      # fails startup — see app/config.py).
+      SQUARE_ENV: ${SQUARE_ENV:-sandbox}
+      SQUARE_APPLICATION_ID: ${SQUARE_APPLICATION_ID:-}
+      SQUARE_ACCESS_TOKEN: ${SQUARE_ACCESS_TOKEN:-}
+      SQUARE_LOCATION_ID: ${SQUARE_LOCATION_ID:-}
+      SQUARE_DEVICE_ID: ${SQUARE_DEVICE_ID:-}
+      SQUARE_CURRENCY: ${SQUARE_CURRENCY:-CAD}
     depends_on:
       db:
         condition: service_healthy
diff --git a/docker-entrypoint.sh b/docker-entrypoint.sh
index 63fe9bb..17d3e7c 100644
--- a/docker-entrypoint.sh
+++ b/docker-entrypoint.sh
@@ -4,10 +4,13 @@ set -e
 
 # Initialise a fresh database: schema + reference data (menu, tables, floors,
 # channels, payment instruments) + a single owner account. Idempotent — a no-op
-# once staff exist — so it is safe to run on every cold start. Never blocks
-# startup: if it errors (e.g. OWNER_PIN unset on a brand-new DB) the server
-# still comes up so the problem is visible in the logs and the URL responds.
-python -m app.bootstrap || echo "entrypoint: bootstrap reported an error (continuing)"
+# once staff exist — so it is safe to run on every cold start.
+#
+# Fail closed: if bootstrap errors (e.g. OWNER_PIN unset on a brand-new DB, or a
+# migration failure) the container exits non-zero instead of starting the web
+# server against a partially initialised or mismatched schema. `set -e` above
+# turns the failure below into an abort.
+python -m app.bootstrap
 
 # Cloud Run injects PORT (usually 8080) and expects the app to listen on it.
 # Locally PORT is unset, so we default to 8000 — the port the compose Caddy
diff --git a/tests/_pay_fixture.py b/tests/_pay_fixture.py
new file mode 100644
index 0000000..39dbc91
--- /dev/null
+++ b/tests/_pay_fixture.py
@@ -0,0 +1,97 @@
+"""Shared test fixture for the payment core.
+
+Builds an engine against PostgreSQL when ``PG_TEST_DSN`` is set (real FK / row
+locking / unique races), otherwise a throwaway file-backed SQLite with
+``PRAGMA foreign_keys=ON`` so even the SQLite runs create and honour real parent
+rows (review finding #16 — no dangling FK ids). Provides ``seed_parents`` which
+inserts a real Staff / Channel / Order / PaymentInstrument / Payment graph so
+payment/refund attempts reference rows that actually exist.
+"""
+import os
+import sys
+import tempfile
+import uuid
+
+sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
+
+from sqlalchemy import create_engine, event
+from sqlalchemy.orm import sessionmaker
+
+from app.database import Base
+from app.models import oltp  # noqa: F401  register tables
+from app.models.oltp import Channel, Order, Payment, PaymentInstrument, Staff
+
+
+def pg_dsn() -> str | None:
+    return os.environ.get("PG_TEST_DSN")
+
+
+def make_engine():
+    """Return (engine, is_postgres). SQLite path enables FK enforcement."""
+    dsn = pg_dsn()
+    if dsn:
+        return create_engine(dsn, future=True), True
+    path = os.path.join(tempfile.gettempdir(), f"paytest_{uuid.uuid4().hex}.db")
+    engine = create_engine(f"sqlite:///{path}", future=True)
+
+    @event.listens_for(engine, "connect")
+    def _fk_on(dbapi_con, _rec):  # noqa: ANN001
+        dbapi_con.execute("PRAGMA foreign_keys=ON")
+
+    return engine, False
+
+
+def fresh_schema(engine):
+    """Clean slate: drop then create the whole schema (safe on the disposable
+    test Postgres and on a throwaway SQLite file)."""
+    Base.metadata.drop_all(engine)
+    Base.metadata.create_all(engine)
+
+
+def Session(engine):
+    return sessionmaker(bind=engine, future=True)
+
+
+# Per-test isolation helper. On a shared Postgres database, the previous test's
+# open session holds locks that would block the next test's drop_all/create_all
+# (a hang). new_db() closes and disposes the prior engine/session before
+# rebuilding, and seeds a fresh parent graph.
+_state: dict = {"session": None, "engine": None}
+
+
+def new_db():
+    if _state["session"] is not None:
+        try:
+            _state["session"].close()
+        except Exception:
+            pass
+    if _state["engine"] is not None:
+        _state["engine"].dispose()
+    engine, _is_pg = make_engine()
+    fresh_schema(engine)
+    session = Session(engine)()
+    ids = seed_parents(session)
+    _state["session"], _state["engine"] = session, engine
+    return session, ids
+
+
+def seed_parents(session) -> dict:
+    """Insert a minimal, valid parent graph and return the ids attempts reference."""
+    staff = Staff(name="Tester", role="owner", pin_code="pbkdf2_sha256$1$x$y")
+    channel = Channel(code=f"ch_{uuid.uuid4().hex[:6]}", name="Dine", channel_type="dine_in")
+    session.add_all([staff, channel])
+    session.flush()
+    order = Order(code=f"O{uuid.uuid4().hex[:8]}", channel_id=channel.id)
+    inst = PaymentInstrument(
+        code=f"cash_{uuid.uuid4().hex[:6]}", name="Cash",
+        instrument_type="cash", provider="manual",
+    )
+    session.add_all([order, inst])
+    session.flush()
+    payment = Payment(order_id=order.id, instrument_id=inst.id, staff_id=staff.id,
+                      total_cents=10000)
+    session.add(payment)
+    session.flush()
+    session.commit()
+    return {"staff_id": staff.id, "order_id": order.id,
+            "payment_id": payment.id, "instrument_id": inst.id}
diff --git a/tests/test_config.py b/tests/test_config.py
new file mode 100644
index 0000000..81e62c2
--- /dev/null
+++ b/tests/test_config.py
@@ -0,0 +1,95 @@
+"""Stage 1 regression tests — production config fails closed.
+
+Covers audit findings #17 (no public dev SECRET_KEY in production) and #16/#20
+supporting behaviour. Run: python tests/test_config.py
+"""
+import os
+import sys
+
+# Import the app package regardless of CWD.
+sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
+
+from app import config, security  # noqa: E402
+
+_failures = []
+
+
+def check(cond, label):
+    if cond:
+        print(f"  ok   {label}")
+    else:
+        _failures.append(label)
+        print(f"  FAIL {label}")
+
+
+def _clear(*names):
+    for n in names:
+        os.environ.pop(n, None)
+
+
+def test_dev_allows_fallback_secret():
+    _clear("APP_ENV", "SECRET_KEY")
+    os.environ["APP_ENV"] = "development"
+    check(not config.is_production(), "development is not production")
+    # Dev may use the public fallback key without raising.
+    check(security._secret() == security._DEV_SECRET.encode(), "dev uses fallback key")
+
+
+def test_production_without_secret_fails_closed():
+    _clear("SECRET_KEY")
+    os.environ["APP_ENV"] = "production"
+    check(config.is_production(), "production is production")
+    # startup validation raises
+    raised = False
+    try:
+        config.validate_startup_config()
+    except config.ConfigError:
+        raised = True
+    check(raised, "validate_startup_config raises without SECRET_KEY in prod")
+    # defence in depth: signing also refuses
+    raised2 = False
+    try:
+        security._secret()
+    except config.ConfigError:
+        raised2 = True
+    check(raised2, "_secret() refuses the dev key in production")
+
+
+def test_production_with_secret_ok():
+    os.environ["APP_ENV"] = "production"
+    os.environ["SECRET_KEY"] = "x" * 48
+    _clear("SQUARE_ACCESS_TOKEN", "SQUARE_LOCATION_ID", "SQUARE_DEVICE_ID", "COOKIE_SECURE")
+    warnings = config.validate_startup_config()
+    check(security._secret() == (b"x" * 48), "prod uses the real SECRET_KEY")
+    # Cash-only + no cookie-secure produce warnings, not errors.
+    check(any("Square" in w for w in warnings), "warns when Square unconfigured")
+    check(any("COOKIE_SECURE" in w for w in warnings), "warns when COOKIE_SECURE unset")
+
+
+def test_partial_square_fails_closed():
+    os.environ["APP_ENV"] = "production"
+    os.environ["SECRET_KEY"] = "x" * 48
+    os.environ["SQUARE_ACCESS_TOKEN"] = "tok"
+    _clear("SQUARE_LOCATION_ID", "SQUARE_DEVICE_ID")
+    raised = False
+    try:
+        config.validate_startup_config()
+    except config.ConfigError as exc:
+        raised = "Square is partially configured" in str(exc)
+    check(raised, "partial Square config aborts startup")
+
+
+if __name__ == "__main__":
+    try:
+        test_dev_allows_fallback_secret()
+        test_production_without_secret_fails_closed()
+        test_production_with_secret_ok()
+        test_partial_square_fails_closed()
+    finally:
+        # Leave the environment clean for any test run after this one.
+        _clear("APP_ENV", "SECRET_KEY", "SQUARE_ACCESS_TOKEN", "SQUARE_LOCATION_ID",
+               "SQUARE_DEVICE_ID", "COOKIE_SECURE")
+    if _failures:
+        print(f"\n{len(_failures)} FAILED")
+        sys.exit(1)
+    print("\nall config tests passed")
diff --git a/tests/test_payment_attempts.py b/tests/test_payment_attempts.py
new file mode 100644
index 0000000..3994cf1
--- /dev/null
+++ b/tests/test_payment_attempts.py
@@ -0,0 +1,174 @@
+"""Stage 2a (hardened) — durable PaymentAttempt + concurrency-safe state machine.
+
+Runs against SQLite-with-FK by default, or Postgres if PG_TEST_DSN is set. Uses a
+real parent graph (finding #16). Concurrency proofs live in test_pg_concurrency.py.
+Run: python tests/test_payment_attempts.py
+"""
+import os
+import sys
+
+sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
+
+from tests._pay_fixture import new_db as _db
+
+from app.models.oltp import PaymentAttempt, PaymentAttemptStatus as S
+from app.services import payment_attempts as pa
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
+def _mk(db, ids, key=None, total=1000, provider="manual", order_id=None):
+    return pa.create_attempt(
+        db, provider=provider, order_id=order_id or ids["order_id"],
+        staff_id=ids["staff_id"], expected_total_cents=total,
+        subtotal_cents=total, idempotency_key=key,
+    )
+
+
+def test_provider_required_and_validated():
+    db, ids = _db()
+    raised = False
+    try:
+        pa.create_attempt(db, provider="nope", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=100)
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "unregistered provider is rejected (no silent default)")
+
+
+def test_idempotent_create_same_intent():
+    db, ids = _db()
+    a = _mk(db, ids, key="abc")
+    b = _mk(db, ids, key="abc")
+    check(a.id == b.id, "same key + same intent returns the same attempt")
+    check(db.query(PaymentAttempt).count() == 1, "no duplicate row")
+
+
+def test_same_key_different_intent_conflicts():
+    db, ids = _db()
+    _mk(db, ids, key="dup", total=1000)
+    raised = False
+    try:
+        _mk(db, ids, key="dup", total=9999)  # different amount => different intent
+    except pa.IdempotencyConflict:
+        raised = True
+    check(raised, "same key + different intent raises IdempotencyConflict")
+
+
+def test_legal_settlement_path():
+    db, ids = _db()
+    a = _mk(db, ids)
+    pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id="chk_1")
+    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="pay_1")
+    pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"])
+    check(a.status == S.SETTLED, "created->pending->approved->settled")
+    check(a.provider_payment_id == "pay_1", "provider id persisted")
+    check(a.payment_id == ids["payment_id"], "settled attempt links its Payment")
+
+
+def test_illegal_and_terminal():
+    db, ids = _db()
+    a = _mk(db, ids)
+    raised = False
+    try:
+        pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"])
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "created->settled rejected (no processor outcome)")
+    b = _mk(db, ids, key="k2")
+    pa.transition(db, b, S.FAILED, last_error="declined")
+    raised2 = False
+    try:
+        pa.transition(db, b, S.PROCESSOR_PENDING)
+    except pa.PaymentAttemptError:
+        raised2 = True
+    check(raised2, "FAILED is terminal, cannot reopen")
+
+
+def test_settle_requires_payment_id():
+    db, ids = _db()
+    a = _mk(db, ids)
+    pa.transition(db, a, S.PROCESSOR_PENDING)
+    pa.transition(db, a, S.PROCESSOR_APPROVED)
+    raised = False
+    try:
+        pa.transition(db, a, S.SETTLED)
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "settling without payment_id is rejected")
+
+
+def test_write_once_provider_id_via_cas():
+    db, ids = _db()
+    a = _mk(db, ids)
+    pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id="chk_1")
+    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_checkout_id="chk_1")  # same ok
+    raised = False
+    try:
+        pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"],
+                      provider_checkout_id="chk_2")  # different -> conflict
+    except pa.TransitionConflict:
+        raised = True
+    check(raised, "a set provider id cannot be overwritten with a different value")
+
+
+def test_snapshot_immutable_across_transitions():
+    db, ids = _db()
+    a = _mk(db, ids, total=1234)
+    snap = (a.subtotal_cents, a.expected_total_cents)
+    pa.transition(db, a, S.PROCESSOR_PENDING)
+    pa.transition(db, a, S.PROCESSOR_APPROVED)
+    pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"])
+    check(snap == (a.subtotal_cents, a.expected_total_cents),
+          "amount snapshot unchanged by transitions (service contract)")
+
+
+def test_reconciliation_needs_evidence():
+    db, ids = _db()
+    a = _mk(db, ids)
+    pa.transition(db, a, S.REQUIRES_RECONCILIATION, last_error="lost")
+    # plain transition out is blocked
+    raised = False
+    try:
+        pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"])
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "plain transition cannot leave REQUIRES_RECONCILIATION")
+    # resolution requires evidence
+    raised2 = False
+    try:
+        pa.resolve_reconciliation(db, a, resolved_status=S.SETTLED,
+                                  resolved_by="", note="", payment_id=ids["payment_id"])
+    except pa.PaymentAttemptError:
+        raised2 = True
+    check(raised2, "resolution without resolved_by/note is rejected")
+    pa.resolve_reconciliation(db, a, resolved_status=S.SETTLED, resolved_by="mgr",
+                              note="looked up in Square dashboard", payment_id=ids["payment_id"])
+    check(a.status == S.SETTLED and a.reconciled_by == "mgr" and a.reconciliation_note,
+          "resolution with evidence settles and records who/why")
+
+
+if __name__ == "__main__":
+    for fn in (
+        test_provider_required_and_validated,
+        test_idempotent_create_same_intent,
+        test_same_key_different_intent_conflicts,
+        test_legal_settlement_path,
+        test_illegal_and_terminal,
+        test_settle_requires_payment_id,
+        test_write_once_provider_id_via_cas,
+        test_snapshot_immutable_across_transitions,
+        test_reconciliation_needs_evidence,
+    ):
+        print(f"- {fn.__name__}")
+        fn()
+    if _failures:
+        print(f"\n{len(_failures)} FAILED")
+        sys.exit(1)
+    print("\nall payment-attempt tests passed")
diff --git a/tests/test_payment_providers.py b/tests/test_payment_providers.py
new file mode 100644
index 0000000..ae361b2
--- /dev/null
+++ b/tests/test_payment_providers.py
@@ -0,0 +1,181 @@
+"""Provider adapters (hardened) — capabilities, key forwarding, explicit state
+mapping, and non-swallowed cancel. Square adapter driven against a fake HTTP
+layer (no creds/network). Run: python tests/test_payment_providers.py
+"""
+import os
+import sys
+
+sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
+
+from app.models.oltp import PaymentAttemptStatus as S
+from app.models.oltp import RefundAttemptStatus as R
+from app.services import payment_providers as pp
+from app.services import square
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
+def _fake(fn):
+    orig = square._request
+    square._request = fn
+    return orig
+
+
+def test_registry_and_capabilities():
+    manual = pp.get_provider("manual")
+    sq = pp.get_provider("square_terminal")
+    check(not manual.is_external and sq.is_external, "external flag per provider")
+    check(sq.needs_polling and not manual.needs_polling, "polling capability drives needs_polling")
+    check(pp.Capability.PARTIAL_REFUND in manual.capabilities, "manual advertises partial_refund")
+    raised = False
+    try:
+        pp.get_provider("nope")
+    except pp.UnknownProvider:
+        raised = True
+    check(raised, "unknown provider raises, never guesses")
+
+
+def test_manual_is_local_with_amount():
+    m = pp.get_provider("manual")
+    c = m.charge(amount_cents=1500, currency="CAD", idempotency_key="k", tip_cents=200)
+    check(c.status == S.PROCESSOR_APPROVED, "manual charge instantly approved")
+    check(c.processor_amount_cents == 1500 and c.processor_currency == "CAD",
+          "manual charge echoes processor amount/currency")
+    r = m.refund(amount_cents=1500, currency="CAD", idempotency_key="k")
+    check(r.status == R.COMPLETED and not r.external, "manual refund is local + completed")
+
+
+def test_square_forwards_idempotency_key():
+    seen = {}
+
+    def fake(method, path, payload=None):
+        seen[path] = payload
+        return {"checkout": {"id": "chk_1", "status": "PENDING"}}
+
+    orig = _fake(fake)
+    try:
+        sq = pp.get_provider("square_terminal")
+        sq.charge(amount_cents=2000, currency="CAD", idempotency_key="IDEM-XYZ")
+        body = seen["/v2/terminals/checkouts"]
+        check(body["idempotency_key"] == "IDEM-XYZ",
+              "persisted idempotency key is forwarded into the Square charge (#1)")
+    finally:
+        square._request = orig
+
+
+def test_square_poll_completed_with_payment():
+    def fake(method, path, payload=None):
+        if path.endswith("/chk_1"):
+            return {"checkout": {"id": "chk_1", "status": "COMPLETED",
+                                 "payment_ids": ["pay_9"], "amount_money": {"amount": 2000}}}
+        if path.startswith("/v2/payments/"):
+            return {"payment": {"tip_money": {"amount": 300},
+                                "card_details": {"card": {"card_brand": "VISA", "last_4": "4242"}}}}
+        raise AssertionError(path)
+
+    orig = _fake(fake)
+    try:
+        res = pp.get_provider("square_terminal").poll("chk_1")
+        check(res.status == S.PROCESSOR_APPROVED, "COMPLETED+payment_id -> approved")
+        check(res.provider_payment_id == "pay_9", "captures payment id")
+        check(res.processor_amount_cents == 2000, "captures processor amount (#8)")
+        check(res.tip_cents == 300 and res.card_last4 == "4242", "reads tip + last4")
+    finally:
+        square._request = orig
+
+
+def test_square_completed_without_payment_id_reconciles():
+    def fake(method, path, payload=None):
+        return {"checkout": {"id": "chk_1", "status": "COMPLETED", "payment_ids": []}}
+
+    orig = _fake(fake)
+    try:
+        res = pp.get_provider("square_terminal").poll("chk_1")
+        check(res.status == S.REQUIRES_RECONCILIATION,
+              "COMPLETED without payment id -> reconciliation, not approval (#9)")
+    finally:
+        square._request = orig
+
+
+def test_square_refund_state_mapping():
+    cases = {
+        "COMPLETED": R.COMPLETED,
+        "PENDING": R.PROCESSOR_PENDING,
+        "REJECTED": R.REJECTED,
+        "FAILED": R.FAILED,
+        "WEIRD": R.REQUIRES_RECONCILIATION,
+    }
+    for sq_status, expected in cases.items():
+        def fake(method, path, payload=None, _s=sq_status):
+            return {"refund": {"id": "rf_1", "status": _s}}
+        orig = _fake(fake)
+        try:
+            res = pp.get_provider("square_terminal").refund(
+                amount_cents=500, currency="CAD", idempotency_key="k",
+                provider_payment_id="pay_9")
+            check(res.status == expected, f"Square refund {sq_status} -> {expected}")
+            if sq_status in ("FAILED", "WEIRD"):
+                check(bool(res.error), f"{sq_status} carries a non-empty error (#11)")
+        finally:
+            square._request = orig
+
+
+def test_square_refund_without_payment_id_reconciles():
+    res = pp.get_provider("square_terminal").refund(
+        amount_cents=500, currency="CAD", idempotency_key="k", provider_payment_id=None)
+    check(res.status == R.REQUIRES_RECONCILIATION, "refund w/o payment id -> reconcile, not lie")
+
+
+def test_cancel_is_not_swallowed():
+    def fake(method, path, payload=None):
+        raise square.SquareError("network blip")
+    orig = _fake(fake)
+    try:
+        res = pp.get_provider("square_terminal").cancel(provider_checkout_id="chk_1")
+        check(not res.ok and res.requires_reconciliation,
+              "a failed cancel flags reconciliation, never silent success (#10)")
+    finally:
+        square._request = orig
+
+
+def test_new_provider_plugs_in():
+    class AcmePay(pp.PaymentProvider):
+        key = "acme_pay"; label = "Acme"; is_external = True
+        capabilities = frozenset({pp.Capability.AUTHORIZE, pp.Capability.CAPTURE})
+
+        def charge(self, *, amount_cents, currency, idempotency_key, reference="", note="", tip_cents=0):
+            return pp.ChargeResult(status=S.PROCESSOR_APPROVED, provider_payment_id="acme_1")
+
+        def refund(self, *, amount_cents, currency, idempotency_key, provider_payment_id=None):
+            return pp.RefundResult(status=R.COMPLETED, external=True, provider_refund_id="acme_rf")
+
+    pp.register(AcmePay())
+    got = pp.get_provider("acme_pay")
+    check(got.label == "Acme", "new provider resolves by key")
+    check(pp.Capability.AUTHORIZE in got.capabilities, "new provider advertises capabilities")
+
+
+if __name__ == "__main__":
+    for fn in (
+        test_registry_and_capabilities,
+        test_manual_is_local_with_amount,
+        test_square_forwards_idempotency_key,
+        test_square_poll_completed_with_payment,
+        test_square_completed_without_payment_id_reconciles,
+        test_square_refund_state_mapping,
+        test_square_refund_without_payment_id_reconciles,
+        test_cancel_is_not_swallowed,
+        test_new_provider_plugs_in,
+    ):
+        print(f"- {fn.__name__}")
+        fn()
+    if _failures:
+        print(f"\n{len(_failures)} FAILED")
+        sys.exit(1)
+    print("\nall payment-provider tests passed")
diff --git a/tests/test_pg_concurrency.py b/tests/test_pg_concurrency.py
new file mode 100644
index 0000000..85e1cb0
--- /dev/null
+++ b/tests/test_pg_concurrency.py
@@ -0,0 +1,201 @@
+"""PostgreSQL concurrency proofs for the payment core (findings #16, #17).
+
+These exercise real row locking, unique races, and FK enforcement that SQLite
+cannot prove. They SKIP (exit 0) unless PG_TEST_DSN points at a disposable
+Postgres, e.g.:
+
+    PG_TEST_DSN=postgresql+psycopg://rms:rms@localhost:5433/rms_test \
+        python tests/test_pg_concurrency.py
+
+Run: python tests/test_pg_concurrency.py
+"""
+import os
+import sys
+import threading
+
+sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
+
+from tests._pay_fixture import Session, fresh_schema, make_engine, pg_dsn, seed_parents
+
+from app.models.oltp import PaymentAttempt, PaymentAttemptStatus as S
+from app.services import payment_attempts as pa
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
+def _run_concurrently(fn, n):
+    """Run fn(i) in n threads, collect (result, exception) per thread."""
+    out = [None] * n
+    barrier = threading.Barrier(n)
+
+    def wrap(i):
+        barrier.wait()  # maximise real contention
+        try:
+            out[i] = ("ok", fn(i))
+        except Exception as exc:  # noqa: BLE001
+            out[i] = ("err", exc)
+
+    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
+    for t in threads:
+        t.start()
+    for t in threads:
+        t.join()
+    return out
+
+
+def test_concurrent_same_key_create(engine, ids):
+    Sess = Session(engine)
+
+    def make(_i):
+        db = Sess()
+        try:
+            a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
+                                  staff_id=ids["staff_id"], expected_total_cents=1000,
+                                  subtotal_cents=1000, idempotency_key="concurrent-key")
+            return a.id
+        finally:
+            db.close()
+
+    results = _run_concurrently(make, 8)
+    ids_returned = {r[1] for r in results if r[0] == "ok"}
+    errs = [r[1] for r in results if r[0] == "err"]
+    check(not errs, f"no unhandled errors under concurrent same-key create ({errs[:1]})")
+    check(len(ids_returned) == 1, "all concurrent callers resolve to one attempt id")
+    verify = Sess()
+    n = verify.query(PaymentAttempt).filter_by(idempotency_key="concurrent-key").count()
+    verify.close()
+    check(n == 1, "exactly one PaymentAttempt row persisted")
+
+
+def test_concurrent_conflicting_transition(engine, ids):
+    Sess = Session(engine)
+    db0 = Sess()
+    a = pa.create_attempt(db0, provider="manual", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=1000,
+                          subtotal_cents=1000, idempotency_key="cas-key")
+    pa.transition(db0, a, S.PROCESSOR_PENDING)
+    aid = a.id
+    db0.close()
+
+    targets = [S.PROCESSOR_APPROVED, S.CANCELLED, S.FAILED]
+
+    def move(i):
+        db = Sess()
+        try:
+            att = db.get(PaymentAttempt, aid)
+            kwargs = {"payment_id": ids["payment_id"]} if targets[i] == S.PROCESSOR_APPROVED else {}
+            # approve needs no payment_id; use provider_payment_id to differentiate
+            if targets[i] == S.PROCESSOR_APPROVED:
+                kwargs = {"provider_payment_id": "pay_conc"}
+            pa.transition(db, att, targets[i], **kwargs)
+            return targets[i]
+        finally:
+            db.close()
+
+    results = _run_concurrently(move, 3)
+    wins = [r[1] for r in results if r[0] == "ok"]
+    # Losers refuse explicitly. Two legitimate, timing-dependent flavors, both
+    # subclasses of PaymentAttemptError and both meaning "did not apply":
+    #  - TransitionConflict: the CAS UPDATE lost the race (rowcount 0), or
+    #  - illegal-transition: the loser re-read the row *after* the winner
+    #    committed and saw a now-terminal status.
+    refusals = [r[1] for r in results
+                if r[0] == "err" and isinstance(r[1], pa.PaymentAttemptError)]
+    check(len(wins) == 1, f"exactly one transition wins ({wins})")
+    check(len(refusals) == 2, "both losers refuse with an explicit typed error")
+    # No corruption: the persisted status is exactly the single winner's target.
+    verify = Session(engine)()
+    final = verify.get(PaymentAttempt, aid).status
+    verify.close()
+    check(final == wins[0], "the persisted status is exactly the one winner's target")
+
+
+def test_provider_payment_id_unique(engine, ids):
+    Sess = Session(engine)
+    db = Sess()
+    a = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=1000,
+                          subtotal_cents=1000, idempotency_key="u1")
+    b = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=1000,
+                          subtotal_cents=1000, idempotency_key="u2")
+    pa.transition(db, a, S.PROCESSOR_PENDING, provider_payment_id="DUP")
+    raised = False
+    try:
+        pa.transition(db, b, S.PROCESSOR_PENDING, provider_payment_id="DUP")
+    except pa.TransitionConflict:
+        raised = True
+    db.close()
+    check(raised, "two attempts cannot share one provider_payment_id (#5)")
+
+
+def test_fk_enforced(engine, ids):
+    Sess = Session(engine)
+    db = Sess()
+    raised = False
+    try:
+        pa.create_attempt(db, provider="manual", order_id=999999,  # no such order
+                          staff_id=ids["staff_id"], expected_total_cents=100,
+                          subtotal_cents=100, idempotency_key="fk")
+    except Exception:  # IntegrityError (FK) — Postgres enforces it
+        raised = True
+        db.rollback()
+    db.close()
+    check(raised, "FK to a non-existent order is rejected by Postgres")
+
+
+def test_one_settlement_per_attempt(engine, ids):
+    Sess = Session(engine)
+    db = Sess()
+    a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=1000,
+                          subtotal_cents=1000, idempotency_key="settle1")
+    pa.transition(db, a, S.PROCESSOR_PENDING)
+    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="p1")
+    pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"])
+    # second attempt tries to settle to the SAME payment id
+    b = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=1000,
+                          subtotal_cents=1000, idempotency_key="settle2")
+    pa.transition(db, b, S.PROCESSOR_PENDING)
+    pa.transition(db, b, S.PROCESSOR_APPROVED, provider_payment_id="p2")
+    raised = False
+    try:
+        pa.transition(db, b, S.SETTLED, payment_id=ids["payment_id"])
+    except pa.TransitionConflict:
+        raised = True
+    db.close()
+    check(raised, "one Payment can back at most one settled attempt")
+
+
+if __name__ == "__main__":
+    if not pg_dsn():
+        print("SKIP: PG_TEST_DSN not set (Postgres concurrency tests not run)")
+        sys.exit(0)
+    print(f"Postgres: {pg_dsn()}")
+    tests = [
+        test_concurrent_same_key_create,
+        test_concurrent_conflicting_transition,
+        test_provider_payment_id_unique,
+        test_fk_enforced,
+        test_one_settlement_per_attempt,
+    ]
+    for fn in tests:
+        print(f"- {fn.__name__}")
+        engine, _ = make_engine()
+        fresh_schema(engine)          # clean slate per test for isolation
+        s = Session(engine)()
+        ids = seed_parents(s)
+        s.close()
+        fn(engine, ids)
+        engine.dispose()
+    if _failures:
+        print(f"\n{len(_failures)} FAILED")
+        sys.exit(1)
+    print("\nall Postgres concurrency tests passed")
diff --git a/tests/test_refund_attempts.py b/tests/test_refund_attempts.py
new file mode 100644
index 0000000..9d4f325
--- /dev/null
+++ b/tests/test_refund_attempts.py
@@ -0,0 +1,100 @@
+"""Refund-attempt lifecycle (finding #6) — multiple independent partial refunds.
+
+Run: python tests/test_refund_attempts.py
+"""
+import os
+import sys
+
+sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
+
+from tests._pay_fixture import new_db as _db
+
+from app.models.oltp import RefundAttempt, RefundAttemptStatus as R
+from app.services import payment_attempts as pa
+from app.services import refund_attempts as ra
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
+def _mkref(db, ids, amount, provider="square_terminal", key=None):
+    return ra.create_refund_attempt(
+        db, payment_id=ids["payment_id"], staff_id=ids["staff_id"],
+        provider=provider, amount_cents=amount, idempotency_key=key,
+    )
+
+
+def test_multiple_partial_refunds():
+    db, ids = _db()
+    r1 = _mkref(db, ids, 1500)
+    r2 = _mkref(db, ids, 2000)
+    r3 = _mkref(db, ids, 500)
+    check(len({r1.id, r2.id, r3.id}) == 3, "three independent refund attempts exist")
+    check(len({r1.idempotency_key, r2.idempotency_key, r3.idempotency_key}) == 3,
+          "each refund has its own idempotency key")
+
+
+def test_idempotent_refund_create():
+    db, ids = _db()
+    a = _mkref(db, ids, 1000, key="rk")
+    b = _mkref(db, ids, 1000, key="rk")
+    check(a.id == b.id, "same key returns the same refund attempt")
+    check(db.query(RefundAttempt).count() == 1, "no duplicate refund row")
+
+
+def test_refund_amount_must_be_positive():
+    db, ids = _db()
+    raised = False
+    try:
+        _mkref(db, ids, 0)
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "zero/negative refund amount is rejected")
+
+
+def test_running_total_counts_inflight_and_completed():
+    db, ids = _db()
+    r1 = _mkref(db, ids, 1500)
+    r2 = _mkref(db, ids, 2000)
+    ra.transition_refund(db, r1, R.COMPLETED, provider_refund_id="rf_1")
+    # r2 stays CREATED (in flight) — still counts against balance
+    total = ra.refunded_and_pending_cents(db, ids["payment_id"])
+    check(total == 3500, "refunded+pending total counts completed and in-flight")
+    ra.transition_refund(db, r2, R.REJECTED, last_error="declined")
+    total2 = ra.refunded_and_pending_cents(db, ids["payment_id"])
+    check(total2 == 1500, "a rejected refund frees its amount from the total")
+
+
+def test_refund_state_transitions():
+    db, ids = _db()
+    r = _mkref(db, ids, 500)
+    ra.transition_refund(db, r, R.PROCESSOR_PENDING, provider_refund_id="rf_x")
+    ra.transition_refund(db, r, R.COMPLETED)
+    check(r.status == R.COMPLETED, "created->pending->completed")
+    raised = False
+    try:
+        ra.transition_refund(db, r, R.FAILED)  # terminal
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "COMPLETED refund is terminal")
+
+
+if __name__ == "__main__":
+    for fn in (
+        test_multiple_partial_refunds,
+        test_idempotent_refund_create,
+        test_refund_amount_must_be_positive,
+        test_running_total_counts_inflight_and_completed,
+        test_refund_state_transitions,
+    ):
+        print(f"- {fn.__name__}")
+        fn()
+    if _failures:
+        print(f"\n{len(_failures)} FAILED")
+        sys.exit(1)
+    print("\nall refund-attempt tests passed")
```
