# Review Handoff #7 — Final Pre-Stage-2c Correction Round

**For:** external reviewer (ChatGPT). **Stage 2c is NOT started** (as instructed).
Covers only `CLAUDE_HANDOFF6_DEEP_REVIEW_FEEDBACK.md` findings #1–#2 — the final
pre-2c corrections.

## Commit range
- **main (pre-remediation) SHA:** `07ee4f1af26aa383b7de06377f18caa98b153e4b`
- **Handoff #6 HEAD:** `f44bb3f66562b6621cd6196996a0a695e46b5a2e`
- **HEAD (this handoff):** `2c2e6768f4970030b65d4319c10e15d00f308d2c`
- Branch: `fix/p0-security-and-payments`
- **Commits added this round (f44bb3f..HEAD):**
  - `3166c67` external refund needs a durable provider_refund_id to pend/complete (#1)
  - `2c2e676` strict processor-money parsing (reject float/bool/decimal) (#2)

## `git status --short`
No uncommitted **source** changes. Untracked non-source: `REVIEW_HANDOFF*.md`,
business docs/decks, `.github/`, `restaurant-app-review.zip`.

## Finding-by-finding
| # | Sev | Finding | Status | Where |
|---|---|---|---|---|
| 1 | CRIT | External refund needs a durable provider_refund_id to pend/complete | ✅ | `refund_attempts._require_external_refund_id` |
| 2 | HIGH | Strict processor-money parsing | ✅ | `square._safe_int` |

## External refund transition invariant (#1)
`_require_external_refund_id(refund, provider_refund_id, new_status)` — enforced in
`transition_refund()` when entering `PROCESSOR_PENDING` or `COMPLETED`:
- **External provider** (`get_provider(refund.provider).is_external`): requires an
  effective `provider_refund_id` (supplied in the transition OR already
  persisted). Without it → `PaymentAttemptError`; the caller must route to
  `REQUIRES_RECONCILIATION` if the processor outcome is unknown.
- **Manual/local provider**: `CREATED -> COMPLETED` remains valid with no id.

This is the refund equivalent of the external `PROCESSOR_APPROVED` invariant, so
a live external refund can never be marked `COMPLETED` without a durable processor
reference. `provider_refund_id` remains write-once (CAS guard) and
`UNIQUE(provider, provider_refund_id)` still rejects duplicates.

## Refund reconciliation completion requirements (#1)
`resolve_refund_reconciliation()` checks the same invariant **up front** for a
`COMPLETED` resolution (before writing the audit event / reconciliation fields),
so a rejection leaves no half-written state:
- external automatic resolution to `COMPLETED` requires both `provider_evidence`
  **and** an authoritative `provider_refund_id`;
- a missing id is rejected — never silently manufactured.

## Strict processor-money parsing contract (#2)
`_safe_int(v)` — processor money is an integer number of minor units:
- **Accepts:** a Python `int` (explicitly excluding `bool`), or an integer-only
  string (optional leading sign, digits only).
- **Rejects → `None`:** `float` (e.g. `1000.9`), `bool` (`True`/`False`), decimal
  string (`"10.5"`), `NaN`/`inf`, arbitrary objects.
A rejected value yields incomplete evidence → `REQUIRES_RECONCILIATION`; there is
no silent coercion (`1.9 -> 1`, `True -> 1` can no longer happen).

## Tests run — commands, counts (0 failures)
Env: Python 3.14, SQLAlchemy 2.0.51, psycopg 3.3.4 (test-only), Postgres 16 @ :5433.
```
# SQLite (default)
test_payment_attempts   26 ok   test_refund_attempts   37 ok
test_payment_providers  79 ok   test_config PASS
test_pg_concurrency / test_pg_migration  SKIP (no PG_TEST_DSN)
test_templates/security/admin/money/reconciliation/schedule  PASS

# PostgreSQL (PG_TEST_DSN set)
test_pg_concurrency     14 ok   test_pg_migration   25 ok
test_payment_attempts / test_refund_attempts  PASS
```
Concurrency stability: **6/6** consecutive full runs.

### New acceptance coverage this round
External refund invariant: external pending/completed without id rejected; with
id accepted; persisted id carries pending→completed; manual completes without id;
external reconciliation→completed requires id; write-once provider_refund_id;
duplicate `(provider, provider_refund_id)` rejected.
Strict money: `1000` and `"1000"`/`"-5"` accepted; `1000.9`, `True`, `False`,
`"10.5"`, `NaN`, arbitrary object all rejected.

### PostgreSQL migration + concurrency (unchanged, still green)
```
migration: clean upgrade, default removed, terminal backfill, idempotent re-run,
  atomic rollback on injected failure, non-null refund_id fails strict, dup fail-closed
concurrency: same-key create/refund -> one row; conflicting transition -> one winner;
  concurrent refund reconciliation -> one winner; provider-id uniqueness; FK; one settlement
```

## Areas I am least confident about
1. **Legacy refund currency source** (unchanged): `Payment` has no per-row
   currency; legacy validation falls back to `venue_currency()`.
2. **`provider_refund_id` auto-migration** still not implemented (non-null legacy
   value fails closed inside the atomic migration tx).
3. **Amount-match is still deferred to 2c:** the external approval/refund invariants
   check presence + structure of evidence, not that `processor_amount_cents`
   equals `expected_total_cents` / the refund amount — that reconciliation is
   Stage 2c settlement's responsibility.
4. **Manual reconciliation of an external refund to COMPLETED** also requires the
   refund id (same invariant) — I did not add a manager-only exception path;
   if the reviewer wants a documented exceptional manual override, that's a small
   follow-up.
5. **Capability validation proves presence, not correctness** (unchanged).

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
index 0000000..052aafc
--- /dev/null
+++ b/app/config.py
@@ -0,0 +1,86 @@
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
+def venue_currency() -> str:
+    """The venue's operating currency (ISO 4217). The app is single-currency;
+    this is the authoritative fallback for a legacy Payment that has no per-row
+    currency, e.g. when validating a refund currency. VENUE_CURRENCY wins, then
+    SQUARE_CURRENCY, else CAD."""
+    return (os.environ.get("VENUE_CURRENCY")
+            or os.environ.get("SQUARE_CURRENCY") or "CAD").strip().upper()
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
index 58e5b2e..ce63f03 100644
--- a/app/migrate.py
+++ b/app/migrate.py
@@ -82,6 +82,20 @@ ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
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
+    ("refund_attempt", "intent_fingerprint", "VARCHAR(64) NOT NULL DEFAULT ''"),
 )
 
 # (table, column, min_length, new DDL type). Columns whose type/length GREW
@@ -93,12 +107,33 @@ WIDENED_COLUMNS: tuple[tuple[str, str, int, str], ...] = (
     # plaintext, so the old VARCHAR(8) overflows on the first login that upgrades
     # a legacy PIN to a hash.
     ("staff", "pin_code", 128, "VARCHAR(128)"),
+    # PaymentAttempt.provider grew VARCHAR(20) -> VARCHAR(30) (shared
+    # PROVIDER_KEY_LEN) when 'square' stopped being a silent default.
+    ("payment_attempt", "provider", 30, "VARCHAR(30)"),
 )
 
+# Provider-scoped uniqueness added to *existing* payment tables. create_all()
+# builds these on a fresh DB but never adds a constraint to a table that already
+# exists, so an upgraded Stage-2a database needs them applied explicitly. Each is
+# duplicate-scanned and fail-closed before creation (see _migrate_payment_hardening).
+UNIQUE_CONSTRAINTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
+    ("payment_attempt", "uq_attempt_provider_payment", ("provider", "provider_payment_id")),
+    ("payment_attempt", "uq_attempt_provider_checkout", ("provider", "provider_checkout_id")),
+)
+
+# Canonical provider backfill: the retired 'square' default becomes the real
+# registry key before the provider-scoped constraints are applied.
+PROVIDER_BACKFILL = (("payment_attempt", "square", "square_terminal"),)
+
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
@@ -108,9 +143,13 @@ def run(engine: Engine) -> list[str]:
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
@@ -125,10 +164,11 @@ def run(engine: Engine) -> list[str]:
         applied.extend(_backfill_locations(conn))
     # SQLite does not enforce VARCHAR length, so no column ever needs widening
     # there — the model's new size applies to fresh databases via create_all.
+    applied.extend(_migrate_payment_hardening(engine, strict))
     return applied
 
 
-def _run_postgres(engine: Engine) -> list[str]:
+def _run_postgres(engine: Engine, strict: bool = False) -> list[str]:
     """Additive ADD COLUMN for an existing Postgres database (Render).
 
     Only columns that are genuinely missing are added, checked against
@@ -166,7 +206,9 @@ def _run_postgres(engine: Engine) -> list[str]:
                     text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS {column} {ddl}')
                 )
             applied.append(f"{table}.{column}")
-        except Exception as exc:               # noqa: BLE001 — never block startup
+        except Exception as exc:               # noqa: BLE001
+            if strict:
+                raise MigrationError(f"failed to add {table}.{column}: {exc}") from exc
             applied.append(f"SKIPPED {table}.{column}: {exc}")
 
     # Widen any column that outgrew its original length (e.g. pin_code now holds
@@ -192,8 +234,151 @@ def _run_postgres(engine: Engine) -> list[str]:
                     text(f'ALTER TABLE "{table}" ALTER COLUMN {column} TYPE {ddl}')
                 )
             applied.append(f"widened {table}.{column} -> {ddl}")
-        except Exception as exc:               # noqa: BLE001 — never block startup
+        except Exception as exc:               # noqa: BLE001
+            if strict:
+                raise MigrationError(f"failed to widen {table}.{column}: {exc}") from exc
             applied.append(f"SKIPPED widen {table}.{column}: {exc}")
+
+    applied.extend(_migrate_payment_hardening(engine, strict))
+    return applied
+
+
+def _column_exists(conn, table: str, column: str) -> bool:
+    if conn.engine.dialect.name == "sqlite":
+        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
+        return any(r[1] == column for r in rows)
+    return conn.execute(
+        text("SELECT 1 FROM information_schema.columns WHERE table_name=:t "
+             "AND column_name=:c AND table_schema=current_schema()"),
+        {"t": table, "c": column},
+    ).first() is not None
+
+
+def _constraint_exists(conn, table: str, name: str) -> bool:
+    if conn.engine.dialect.name == "sqlite":
+        # A named UNIQUE constraint surfaces as an index (from create_all's inline
+        # constraint) or as our upgrade index — either proves enforcement exists.
+        got = conn.execute(
+            text("SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name=:t "
+                 "AND (name=:n OR sql LIKE :like)"),
+            {"t": table, "n": name, "like": "%UNIQUE%"},
+        ).fetchall()
+        # Fall back to reading the table's own UNIQUE constraint list.
+        idx = conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
+        return bool(got) and any(r[2] for r in idx)  # any unique index present
+    return conn.execute(
+        text("SELECT 1 FROM information_schema.table_constraints WHERE constraint_name=:n "
+             "AND table_name=:t AND table_schema=current_schema()"),
+        {"n": name, "t": table},
+    ).first() is not None
+
+
+def _duplicates(conn, table: str, cols: tuple[str, ...]) -> list[tuple]:
+    """Rows that would violate a unique constraint on ``cols`` (NULLs excluded,
+    since NULL keeps a row distinct on both engines)."""
+    collist = ", ".join(cols)
+    notnull = " AND ".join(f"{c} IS NOT NULL" for c in cols)
+    q = (f"SELECT {collist}, COUNT(*) AS n FROM {table} WHERE {notnull} "
+         f"GROUP BY {collist} HAVING COUNT(*) > 1")
+    return conn.execute(text(q)).fetchall()
+
+
+def _migrate_payment_hardening(engine: Engine, strict: bool) -> list[str]:
+    """Upgrade an existing payment_attempt table to the hardened schema:
+    canonical provider backfill, retire the old provider_refund_id column, and
+    add the provider-scoped UNIQUE constraints — duplicate-scanned and fail-closed
+    so financial data is never silently rewritten. No-op on a fresh DB where
+    create_all already produced everything.
+
+    **Atomicity (#2):** the whole hardening runs in a *single* transaction
+    (Postgres has transactional DDL), so a failure at any step — a duplicate
+    constraint, non-null legacy data under strict — rolls back every earlier step
+    (default drop, backfills). The database is never left partially hardened. It
+    is also idempotent: a second run finds everything already applied and changes
+    nothing.
+    """
+    applied: list[str] = []
+    pg = engine.dialect.name != "sqlite"
+    q = (lambda s: f'"{s}"') if pg else (lambda s: s)
+
+    with engine.begin() as conn:
+        # 1. Canonical provider backfill (before the provider-scoped constraints).
+        for table, old, new in PROVIDER_BACKFILL:
+            if not _column_exists(conn, table, "provider"):
+                continue
+            n = conn.execute(text(f"UPDATE {q(table)} SET provider=:new WHERE provider=:old"),
+                             {"new": new, "old": old}).rowcount
+            if n:
+                applied.append(f"backfilled {table}.provider {old}->{new} ({n} rows)")
+
+        # 1b. A terminal-card instrument that predates the provider column takes the
+        # 'manual' default; repair ONLY that legacy state (#1). Narrowing to
+        # provider='manual' means a deliberately-chosen provider (e.g. a future
+        # stripe_terminal) is never overwritten, and a re-run is a no-op.
+        if _column_exists(conn, "payment_instrument", "provider"):
+            n = conn.execute(text(
+                "UPDATE payment_instrument SET provider='square_terminal' "
+                "WHERE code='card_terminal' AND provider='manual'")).rowcount
+            if n:
+                applied.append(f"backfilled payment_instrument card_terminal->square_terminal ({n})")
+
+        # 1c. Remove the retired legacy DEFAULT 'square' on payment_attempt.provider
+        # (widening the type does not drop it). Postgres only; SQLite's model has no
+        # server default and cannot DROP DEFAULT without a table rebuild.
+        if pg and _column_exists(conn, "payment_attempt", "provider"):
+            has_default = conn.execute(text(
+                "SELECT column_default FROM information_schema.columns "
+                "WHERE table_name='payment_attempt' AND column_name='provider' "
+                "AND table_schema=current_schema()")).scalar_one_or_none()
+            if has_default is not None:
+                conn.execute(text('ALTER TABLE payment_attempt ALTER COLUMN provider DROP DEFAULT'))
+                applied.append("dropped legacy default on payment_attempt.provider")
+
+        # 2. Retire the old provider_refund_id. Drop only when empty; non-null under
+        # strict fails closed (rolling back the whole tx).
+        if _column_exists(conn, "payment_attempt", "provider_refund_id"):
+            leftover = conn.execute(
+                text("SELECT COUNT(*) FROM payment_attempt WHERE provider_refund_id IS NOT NULL")
+            ).scalar_one()
+            if leftover:
+                msg = (f"payment_attempt.provider_refund_id still holds {leftover} non-null "
+                       "row(s); migrate them into refund_attempt before upgrading. Financially "
+                       "meaningful legacy data must not be left behind.")
+                if strict:
+                    raise MigrationError(msg)          # fail closed (rolls back — #6)
+                applied.append(f"KEPT (non-strict): {msg}")
+            else:
+                try:
+                    conn.execute(text('ALTER TABLE payment_attempt DROP COLUMN '
+                                      + ("IF EXISTS " if pg else "") + "provider_refund_id"))
+                    applied.append("dropped retired payment_attempt.provider_refund_id")
+                except Exception as exc:  # noqa: BLE001 — SQLite <3.35 can't drop
+                    if strict:
+                        raise MigrationError(f"failed to drop provider_refund_id: {exc}") from exc
+                    applied.append(f"SKIPPED drop provider_refund_id: {exc}")
+
+        # 3. Provider-scoped uniqueness — dup-scan, fail closed, then create.
+        for table, name, cols in UNIQUE_CONSTRAINTS:
+            if not all(_column_exists(conn, table, c) for c in cols):
+                continue
+            if _constraint_exists(conn, table, name):
+                continue
+            dupes = _duplicates(conn, table, cols)
+            if dupes:
+                msg = (f"cannot add {name}: {len(dupes)} duplicate group(s) in "
+                       f"{table}({', '.join(cols)}) — e.g. {dupes[0]}. Resolve before upgrading.")
+                if strict:
+                    raise MigrationError(msg)          # fail closed (rolls back)
+                applied.append(f"BLOCKED {msg}")
+                continue
+            if pg:
+                conn.execute(text(f'ALTER TABLE {q(table)} ADD CONSTRAINT {name} '
+                                  f'UNIQUE ({", ".join(cols)})'))
+            else:
+                conn.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS {name} '
+                                  f'ON {table} ({", ".join(cols)})'))
+            applied.append(f"added {name} on {table}({', '.join(cols)})")
+
     return applied
 
 
diff --git a/app/models/oltp.py b/app/models/oltp.py
index 44df190..6f06e92 100644
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
@@ -907,6 +920,282 @@ class PaymentAllocation(Base):
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
+    # Fingerprint of the immutable refund intent behind idempotency_key (#3), so
+    # reusing a key for a different payment/amount/currency is a conflict.
+    intent_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
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
index 0000000..e06ce5b
--- /dev/null
+++ b/app/services/payment_attempts.py
@@ -0,0 +1,408 @@
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
+from app.config import venue_currency
+from app.models.oltp import (
+    PAYMENT_ATTEMPT_TRANSITIONS,
+    AuditEvent,
+    PaymentAttempt,
+    PaymentAttemptStatus,
+    Role,
+    Staff,
+)
+
+# Roles permitted to resolve an ambiguous payment by hand.
+_RECON_ROLES = frozenset({Role.OWNER, Role.MANAGER})
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
+# Postgres SQLSTATEs that mean "a concurrency race", not "infrastructure is down":
+# deadlock_detected, serialization_failure, lock_not_available.
+_LOCK_SQLSTATES = frozenset({"40P01", "40001", "55P03"})
+
+
+def is_lock_conflict(exc: Exception) -> bool:
+    """True when an OperationalError is a lock/deadlock race (a transition
+    conflict) rather than a genuine infrastructure failure (DB down, connection
+    reset). Infra failures must propagate, not masquerade as a conflict (#12)."""
+    orig = getattr(exc, "orig", None)
+    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
+    if sqlstate in _LOCK_SQLSTATES:
+        return True
+    msg = str(orig or exc).lower()
+    return ("deadlock" in msg or "database is locked" in msg
+            or "database table is locked" in msg)
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
+def _valid_currency(cur: str | None) -> bool:
+    return isinstance(cur, str) and len(cur) == 3 and cur.isalpha()
+
+
+def _require_approval_evidence(
+    attempt: PaymentAttempt, provider_payment_id: str | None,
+    processor_amount_cents: int | None, processor_currency: str | None,
+) -> None:
+    """External providers cannot enter PROCESSOR_APPROVED without authoritative
+    evidence — enforced at the state-machine boundary, not just in the Square
+    adapter (finding #2). Manual/local providers approve instantly with no
+    external evidence. Effective values combine what's already persisted with
+    what this transition supplies."""
+    from app.services.payment_providers import get_provider
+    if not get_provider(attempt.provider).is_external:
+        return
+    eff_pay = provider_payment_id or attempt.provider_payment_id
+    eff_amt = (processor_amount_cents if processor_amount_cents is not None
+               else attempt.processor_amount_cents)
+    eff_cur = processor_currency or attempt.processor_currency
+    if not eff_pay or eff_amt is None or eff_amt < 0 or not _valid_currency(eff_cur):
+        raise PaymentAttemptError(
+            "an external provider cannot enter PROCESSOR_APPROVED without a provider "
+            "payment id, a non-negative processor amount, and a valid processor currency (#2).")
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
+    currency: str | None = None,
+    idempotency_key: str | None = None,
+) -> PaymentAttempt:
+    """Persist a CREATED attempt from an already-locked payable snapshot. Commits.
+
+    ``currency`` defaults to the venue currency (``config.venue_currency()``) when
+    omitted — never a hard-coded CAD — so a USD venue does not silently create a
+    CAD intent (#3). Idempotent and concurrency-safe: a repeated key returns the
+    existing attempt; a repeated key with a different intent raises
+    ``IdempotencyConflict``; an unregistered provider is rejected.
+    """
+    _validate_provider(provider)
+    currency = (currency or venue_currency()).upper()
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
+    if new_status == PaymentAttemptStatus.PROCESSOR_APPROVED:
+        _require_approval_evidence(attempt, provider_payment_id,
+                                   processor_amount_cents, processor_currency)
+
+    expected = attempt.status
+    values: dict = {"status": new_status, "updated_at": datetime.now()}
+    if last_error is not None:
+        values["last_error"] = last_error
+
+    # All write-once: NULL accepts a first value, the same value is idempotent, a
+    # different value fails the guarded UPDATE (rowcount 0) -> TransitionConflict.
+    # Processor amount/currency are evidence and must never be overwritten (#8).
+    conds = [PaymentAttempt.id == attempt.id, PaymentAttempt.status == expected]
+    for field, val in (
+        ("provider_checkout_id", provider_checkout_id),
+        ("provider_payment_id", provider_payment_id),
+        ("payment_id", payment_id),
+        ("processor_amount_cents", processor_amount_cents),
+        ("processor_currency", processor_currency),
+    ):
+        if val is not None:
+            values[field] = val
+            col = getattr(PaymentAttempt, field)
+            conds.append(or_(col.is_(None), col == val))
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
+    except IntegrityError as exc:
+        db.rollback()
+        raise TransitionConflict(
+            f"transition {expected} -> {new_status} conflicted "
+            f"(uniqueness): {getattr(exc, 'orig', exc)}"
+        ) from exc
+    except OperationalError as exc:
+        db.rollback()
+        if is_lock_conflict(exc):
+            raise TransitionConflict(
+                f"transition {expected} -> {new_status} lost a lock/deadlock race: "
+                f"{getattr(exc, 'orig', exc)}"
+            ) from exc
+        raise  # genuine infrastructure failure — propagate, do not mask as a conflict
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
+class ReconciliationAuthorityError(PaymentAttemptError):
+    """The caller lacks the authority/evidence to resolve an ambiguous attempt."""
+
+
+def resolve_reconciliation(
+    db: Session,
+    attempt: PaymentAttempt,
+    *,
+    resolved_status: str,
+    note: str,
+    actor: Staff | None = None,
+    automatic: bool = False,
+    provider_evidence: str | None = None,
+    payment_id: int | None = None,
+    provider_payment_id: str | None = None,
+) -> PaymentAttempt:
+    """Resolve an ambiguous attempt under explicit authority, with an audit trail
+    (findings #12, #13). The only exit from REQUIRES_RECONCILIATION.
+
+    Two authorities, and nothing else settles an ambiguous payment:
+
+    * **Manual** — ``actor`` must be a Staff with an OWNER/MANAGER role. The
+      resolution records who, when, why, and (when present) provider evidence.
+    * **Automatic** — ``automatic=True`` (a recovery worker) MUST supply
+      ``provider_evidence`` (e.g. a processor lookup result / transaction id). A
+      free-text note is never enough for automated settlement.
+
+    Always writes an ``audit_event`` and persists the evidence on the attempt.
+    """
+    if attempt.status != PaymentAttemptStatus.REQUIRES_RECONCILIATION:
+        raise PaymentAttemptError(
+            "resolve_reconciliation only applies to a REQUIRES_RECONCILIATION attempt.")
+    if resolved_status not in (
+        PaymentAttemptStatus.SETTLED,
+        PaymentAttemptStatus.FAILED,
+        PaymentAttemptStatus.CANCELLED,
+    ):
+        raise PaymentAttemptError(f"cannot resolve reconciliation to {resolved_status!r}.")
+    if not (note or "").strip():
+        raise PaymentAttemptError("reconciliation resolution requires a note.")
+
+    if automatic:
+        if not (provider_evidence or "").strip():
+            raise ReconciliationAuthorityError(
+                "automatic reconciliation must supply provider_evidence — a note alone "
+                "cannot settle an ambiguous payment (#13).")
+        actor_id, resolved_by = None, "system:auto"
+    else:
+        if actor is None or actor.role not in _RECON_ROLES:
+            raise ReconciliationAuthorityError(
+                "manual reconciliation requires an OWNER/MANAGER actor (#13).")
+        actor_id, resolved_by = actor.id, actor.name
+
+    detail = f"attempt {attempt.id} -> {resolved_status}"
+    if provider_evidence:
+        detail += f" [evidence: {provider_evidence}]"
+    detail = f"{detail}: {note.strip()}"[:300]
+    db.add(AuditEvent(staff_id=actor_id, action="reconcile_payment_attempt",
+                      detail=detail, order_id=attempt.order_id))
+
+    attempt.reconciled_at = datetime.now()
+    attempt.reconciled_by = resolved_by[:60]
+    attempt.reconciliation_note = (
+        (note.strip() + (f" | evidence: {provider_evidence}" if provider_evidence else ""))[:300])
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
index 0000000..eac6691
--- /dev/null
+++ b/app/services/payment_providers.py
@@ -0,0 +1,445 @@
+"""Pluggable payment providers (hardened per review).
+
+The durable ``PaymentAttempt``/``RefundAttempt`` state machines are the neutral
+spine; each real-world payment method is a small adapter implementing
+``PaymentProvider`` and registered by a string key. ``PaymentInstrument.provider``
+names which adapter settles an instrument.
+
+Adding a charge / poll / refund / cancel provider is additive:
+
+    1. subclass PaymentProvider (implement the methods for the capabilities you
+       advertise — register() rejects a provider that claims more than it backs),
+    2. register(MyProvider()),
+    3. set instrument.provider = "my_key".
+
+Scope, honestly stated: the auth/capture, webhook, and lookup contracts are
+*defined and registration-validated* but not yet consumed by settlement, and the
+providers are not yet wired into the live charge/refund routes — that is Stage 2c.
+So this is "plug in a new charge/refund/polling provider without touching the
+state machines", not yet "plug in any processor shape with zero core work".
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
+    CANCEL = "cancel"                # cancel a pre-capture charge
+
+
+# The complete, closed capability vocabulary. A provider advertising anything
+# outside this set (a typo, an unknown value) is rejected at registration (#7).
+ALL_CAPABILITIES = frozenset({
+    Capability.POLLING, Capability.WEBHOOKS, Capability.AUTHORIZE, Capability.CAPTURE,
+    Capability.PARTIAL_CAPTURE, Capability.REFUND, Capability.PARTIAL_REFUND, Capability.LOOKUP,
+    Capability.CANCEL,
+})
+
+# A capability is only advertisable if its backing method is actually implemented
+# (finding #10). register() validates this — a provider may not claim behavior it
+# does not provide. REFUND/PARTIAL_REFUND share the abstract refund() (always
+# implemented on a concrete provider), so they need no override check here.
+_CAPABILITY_METHOD = {
+    Capability.POLLING: "poll",
+    Capability.LOOKUP: "lookup",
+    Capability.AUTHORIZE: "authorize",
+    Capability.CAPTURE: "capture",
+    Capability.PARTIAL_CAPTURE: "capture",
+    Capability.WEBHOOKS: "handle_webhook",
+    Capability.CANCEL: "cancel",
+}
+
+
+# --------------------------------------------------------------------------
+# Result value objects — provider-neutral, in the attempts' vocabulary
+# --------------------------------------------------------------------------
+
+@dataclass
+class ChargeResult:
+    """Outcome of asking a provider to charge. ``status`` is a
+    ``PaymentAttemptStatus`` value fed straight into the state machine.
+
+    Amount/tip semantics (finding #17), so settlement compares like with like:
+      * the attempt's ``expected_total_cents`` is the **pre-tip** amount we asked
+        the terminal to charge (items + tax + service charge + surcharge - discount);
+      * the guest adds a tip on the terminal, so the processor captures
+        base + tip;
+      * ``processor_amount_cents`` here is the processor's **pre-tip base**
+        (captured total - tip), directly comparable to ``expected_total_cents``;
+      * ``tip_cents`` is the processor-confirmed tip.
+    ``processor_amount_cents``/``processor_currency`` are read from authoritative
+    processor evidence, never local config, and are None when unreadable so
+    settlement never verifies against a fabricated value (findings #6/#8).
+    """
+    status: str
+    provider_checkout_id: str | None = None
+    provider_payment_id: str | None = None
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
+        # Never report a successful cancel by default — a provider that does not
+        # support cancellation must fail explicitly, not inherit false success (#3).
+        raise NotImplementedError(f"{self.key} does not support CANCEL")
+
+    # Optional, capability-gated methods. A provider that advertises the matching
+    # capability MUST override the method; register() enforces it (#10). The base
+    # versions exist only so the contract is discoverable and the override check
+    # has something to compare against.
+    def lookup(self, *, provider_payment_id: str | None = None,
+               provider_checkout_id: str | None = None) -> dict:
+        raise NotImplementedError(f"{self.key} does not support LOOKUP")
+
+    def authorize(self, *, amount_cents: int, currency: str, idempotency_key: str) -> ChargeResult:
+        raise NotImplementedError(f"{self.key} does not support AUTHORIZE")
+
+    def capture(self, *, provider_payment_id: str, amount_cents: int | None = None) -> ChargeResult:
+        raise NotImplementedError(f"{self.key} does not support CAPTURE")
+
+    def handle_webhook(self, payload: dict) -> dict:
+        raise NotImplementedError(f"{self.key} does not support WEBHOOKS")
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
+    # Only what the adapter actually backs with a method. Square Terminal is
+    # immediate-capture, so no AUTHORIZE/CAPTURE split is advertised (#10/#19).
+    capabilities = frozenset({
+        Capability.POLLING, Capability.REFUND,
+        Capability.PARTIAL_REFUND, Capability.LOOKUP, Capability.CANCEL,
+    })
+
+    def is_configured(self) -> bool:
+        return square.is_configured()
+
+    def lookup(self, *, provider_payment_id=None, provider_checkout_id=None) -> dict:
+        """Provider-side reconciliation read: fetch the authoritative Payment or
+        terminal checkout so a recovery worker can resolve an ambiguous attempt."""
+        if provider_payment_id:
+            return square.get_payment(provider_payment_id)
+        if provider_checkout_id:
+            return square.get_checkout(provider_checkout_id)
+        raise ValueError("lookup needs a provider payment id or checkout id")
+
+    def charge(self, *, amount_cents, currency, idempotency_key,
+               reference="", note="", tip_cents=0) -> ChargeResult:
+        try:
+            checkout = square.create_checkout(
+                amount_cents, reference_id=reference, note=note,
+                idempotency_key=idempotency_key,   # finding #1: forward the key
+                currency_code=currency,            # finding #5: per-operation currency
+            )
+        except square.SquareApiError as exc:
+            # Classify the 4xx (finding #8). Only a definitive financial DECLINE is
+            # a safe "no charge, FAILED". A conflict may mean the charge exists; an
+            # auth/config/invalid-target/unexpected 4xx is not proof no charge
+            # happened and must not be treated as safe-to-retry -> reconcile.
+            category = square.classify_charge_error(exc)
+            if category == square.CHARGE_DECLINE:
+                return ChargeResult(status=PaymentAttemptStatus.FAILED,
+                                    error=f"declined: {exc}")
+            return ChargeResult(status=PaymentAttemptStatus.REQUIRES_RECONCILIATION,
+                                error=f"{category}: {exc}")
+        except square.SquareError as exc:
+            # Transport/unknown (timeout, dropped connection, 5xx). Square may have
+            # accepted the checkout — must reconcile, never assume FAILED (#4).
+            return ChargeResult(status=PaymentAttemptStatus.REQUIRES_RECONCILIATION,
+                                error=f"unknown submission outcome: {exc}")
+        return ChargeResult(
+            status=PaymentAttemptStatus.PROCESSOR_PENDING,
+            provider_checkout_id=checkout.get("id"),
+        )
+
+    def poll(self, provider_checkout_id: str) -> ChargeResult:
+        try:
+            checkout = square.get_checkout(provider_checkout_id)
+        except square.SquareTransportError as exc:
+            # Transient — keep polling.
+            return ChargeResult(status=PaymentAttemptStatus.PROCESSOR_PENDING,
+                                provider_checkout_id=provider_checkout_id,
+                                error=f"transient lookup error: {exc}")
+        except square.SquareError as exc:
+            # Definitive lookup error (auth/config/not-found). Do not stay PENDING
+            # forever — hand it to reconciliation (#9).
+            return ChargeResult(status=PaymentAttemptStatus.REQUIRES_RECONCILIATION,
+                                provider_checkout_id=provider_checkout_id,
+                                error=f"definitive lookup error: {exc}")
+        status = checkout.get("status")
+        if status == square.COMPLETED:
+            payment_ids = checkout.get("payment_ids") or []
+            if not payment_ids:
+                # Completed but no authoritative payment id — cannot reconcile,
+                # refund, or link. Do NOT treat as ordinary approval (finding #9).
+                return ChargeResult(
+                    status=PaymentAttemptStatus.REQUIRES_RECONCILIATION,
+                    provider_checkout_id=provider_checkout_id,
+                    error="Square COMPLETED without a payment id",
+                )
+            # Authoritative amount/currency from the Payment object, not local
+            # config (findings #6/#17). processor_amount_cents is the PRE-TIP base
+            # so it compares to our pre-tip expected_total_cents.
+            ev = square.completed_payment_evidence(checkout)
+            if not _evidence_coherent(ev):
+                # COMPLETED but the evidence is missing OR internally incoherent
+                # (negative amount, tip > total, malformed currency). Do NOT approve
+                # on evidence we cannot trust — reconcile (findings #3/#4).
+                return ChargeResult(
+                    status=PaymentAttemptStatus.REQUIRES_RECONCILIATION,
+                    provider_checkout_id=provider_checkout_id,
+                    provider_payment_id=payment_ids[0],
+                    error="COMPLETED but processor evidence is incomplete or incoherent",
+                )
+            return ChargeResult(
+                status=PaymentAttemptStatus.PROCESSOR_APPROVED,
+                provider_checkout_id=provider_checkout_id,
+                provider_payment_id=payment_ids[0],
+                processor_amount_cents=ev["base_cents"],
+                processor_currency=ev["currency"],
+                tip_cents=ev["tip_cents"], card_brand=ev["brand"], card_last4=ev["last4"],
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
+            refund = square.create_refund(provider_payment_id, amount_cents,
+                                          idempotency_key, currency_code=currency)
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
+        # A 2xx does not by itself mean cancellation succeeded — validate the
+        # returned checkout status explicitly (finding #11).
+        status = checkout.get("status") or ""
+        if status == square.CANCELED:
+            return CancelResult(ok=True, provider_status=status)
+        if status == square.COMPLETED:
+            return CancelResult(ok=False, provider_status=status, requires_reconciliation=True,
+                                error="checkout already COMPLETED — a payment likely exists")
+        if status in (square.PENDING, square.IN_PROGRESS, square.CANCEL_REQUESTED):
+            return CancelResult(ok=False, provider_status=status, requires_reconciliation=True,
+                                error="cancellation not yet authoritative")
+        return CancelResult(ok=False, provider_status=status, requires_reconciliation=True,
+                            error=f"unexpected cancel status {status!r}")
+
+
+def _evidence_coherent(ev: dict) -> bool:
+    """Structural + arithmetic sanity of processor evidence before approval (#4).
+    Requires: captured total and base present and >= 0, tip >= 0 and <= total,
+    and a structurally valid 3-letter currency. Anything off -> reconcile."""
+    total = ev.get("captured_total_cents")
+    tip = ev.get("tip_cents")
+    base = ev.get("base_cents")
+    cur = ev.get("currency")
+    if total is None or base is None or tip is None:  # missing/malformed -> incoherent
+        return False
+    if total < 0 or tip < 0 or base < 0 or tip > total:
+        return False
+    return isinstance(cur, str) and len(cur) == 3 and cur.isalpha()
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
+def _validate_capabilities(provider: PaymentProvider) -> None:
+    """A provider may only advertise known capabilities (#7), each backed by an
+    implemented method (#10)."""
+    unknown = set(provider.capabilities) - ALL_CAPABILITIES
+    if unknown:
+        raise ValueError(
+            f"provider {provider.key!r} advertises unknown capabilities {sorted(unknown)}; "
+            f"allowed: {sorted(ALL_CAPABILITIES)}")
+    for cap in provider.capabilities:
+        method_name = _CAPABILITY_METHOD.get(cap)
+        if method_name is None:
+            continue  # REFUND/PARTIAL_REFUND ride the abstract refund()
+        impl = getattr(type(provider), method_name, None)
+        base = getattr(PaymentProvider, method_name, None)
+        if impl is None or impl is base:
+            raise ValueError(
+                f"provider {provider.key!r} advertises capability {cap!r} but does not "
+                f"implement {method_name}()")
+
+
+def register(provider: PaymentProvider, *, override: bool = False) -> None:
+    if not provider.key:
+        raise ValueError("payment provider must define a non-empty key")
+    if provider.key in _REGISTRY and not override:
+        raise ValueError(
+            f"a payment provider is already registered for {provider.key!r}; "
+            f"pass override=True to replace it deliberately (#18)")
+    _validate_capabilities(provider)
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
index 0000000..b4b6313
--- /dev/null
+++ b/app/services/refund_attempts.py
@@ -0,0 +1,383 @@
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
+import hashlib
+import secrets
+from datetime import datetime
+
+from sqlalchemy import and_, func, or_, select, update
+from sqlalchemy.exc import IntegrityError, OperationalError
+from sqlalchemy.orm import Session
+
+from app.config import venue_currency
+from app.models.oltp import (
+    REFUND_ATTEMPT_TRANSITIONS,
+    AuditEvent,
+    Payment,
+    PaymentAttempt,
+    PaymentAttemptStatus,
+    RefundAttempt,
+    RefundAttemptStatus,
+    Staff,
+)
+from app.services.payment_attempts import (
+    IdempotencyConflict,
+    PaymentAttemptError,
+    ReconciliationAuthorityError,
+    TransitionConflict,
+    _RECON_ROLES,
+    _validate_provider,
+    is_lock_conflict,
+)
+
+
+def refund_intent_fingerprint(
+    *, payment_id: int, charge_attempt_id: int | None, provider: str,
+    amount_cents: int, currency: str,
+) -> str:
+    """Stable hash of the immutable refund intent behind an idempotency key."""
+    canonical = "|".join(str(x) for x in (
+        payment_id, charge_attempt_id, provider, amount_cents, currency.upper()))
+    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:64]
+
+
+def _resolve_charge_attempt(
+    db: Session, *, payment_id: int, charge_attempt_id: int | None, provider: str,
+) -> PaymentAttempt | None:
+    """Tie the refund to its charge attempt and validate the relationship (#7).
+
+    Prefers deriving the charge attempt from ``payment_id`` (a Payment backs at
+    most one settled attempt) rather than trusting a caller-supplied id. When a
+    charge attempt is found it must match on payment, be SETTLED, and share the
+    refund's provider. Returns the validated PaymentAttempt, or None for a legacy
+    payment with no attempt — in which case the provider is derived from the
+    Payment's instrument and a caller mismatch is rejected (#4).
+    """
+    derived = db.execute(
+        select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
+    ).scalar_one_or_none()
+
+    if charge_attempt_id is not None:
+        supplied = db.get(PaymentAttempt, charge_attempt_id)
+        if supplied is None:
+            raise PaymentAttemptError(f"charge attempt {charge_attempt_id} does not exist.")
+        if derived is not None and derived.id != supplied.id:
+            raise PaymentAttemptError(
+                f"charge attempt {charge_attempt_id} does not back payment {payment_id}.")
+        attempt = supplied
+    else:
+        attempt = derived
+
+    if attempt is None:
+        # Legacy payment predating PaymentAttempt: derive the canonical provider
+        # from the original Payment's instrument and reject a caller mismatch —
+        # never trust the caller to say what a historical payment was (#4). Fail
+        # closed if the provider cannot actually be derived (#5): a missing
+        # instrument, a blank provider, or an unregistered provider must error, not
+        # let caller input silently become authoritative.
+        payment = db.get(Payment, payment_id)
+        if payment is None:
+            raise PaymentAttemptError(f"payment {payment_id} does not exist.")
+        if payment.instrument is None:
+            raise PaymentAttemptError(
+                f"cannot derive a provider for legacy payment {payment_id}: no instrument.")
+        inst_provider = (payment.instrument.provider or "").strip()
+        if not inst_provider:
+            raise PaymentAttemptError(
+                f"cannot derive a provider for legacy payment {payment_id}: "
+                "instrument provider is blank.")
+        _validate_provider(inst_provider)  # the derived provider must be registered
+        if inst_provider != provider:
+            raise PaymentAttemptError(
+                f"refund provider {provider!r} != payment instrument provider "
+                f"{inst_provider!r} (legacy payment {payment_id}).")
+        return None
+
+    if attempt.payment_id != payment_id:
+        raise PaymentAttemptError(
+            f"charge attempt {attempt.id} is not for payment {payment_id}.")
+    if attempt.status != PaymentAttemptStatus.SETTLED:
+        raise PaymentAttemptError(
+            f"cannot refund against a non-settled charge attempt (status {attempt.status!r}).")
+    if attempt.provider != provider:
+        raise PaymentAttemptError(
+            f"refund provider {provider!r} != charge provider {attempt.provider!r}.")
+    return attempt
+
+
+def _validate_refund_currency(currency: str, charge: PaymentAttempt | None) -> None:
+    """A refund must be in the same currency as the money it reverses (#5).
+
+    Attempt-backed: match the charge attempt's currency and, when present, its
+    processor-confirmed currency. Legacy (no attempt): match the venue currency,
+    the best authoritative record when a Payment carries no per-row currency."""
+    want = currency.upper()
+    if charge is not None:
+        if want != charge.currency.upper():
+            raise PaymentAttemptError(
+                f"refund currency {want} != charge currency {charge.currency.upper()}.")
+        if charge.processor_currency and want != charge.processor_currency.upper():
+            raise PaymentAttemptError(
+                f"refund currency {want} != processor currency {charge.processor_currency.upper()}.")
+    else:
+        venue = venue_currency()
+        if want != venue:
+            raise PaymentAttemptError(
+                f"refund currency {want} != venue currency {venue} (legacy payment).")
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
+    currency: str | None = None,
+    charge_attempt_id: int | None = None,
+    idempotency_key: str | None = None,
+) -> RefundAttempt:
+    """Persist a CREATED refund attempt. Commits. ``currency`` defaults to the
+    venue currency when omitted — never a hard-coded CAD (#3). Idempotent and
+    concurrency-safe on the idempotency key: a repeat with the same intent returns
+    the same row; a *different* intent raises ``IdempotencyConflict`` (#3). The
+    charge-attempt linkage is validated (#7)."""
+    _validate_provider(provider)
+    currency = (currency or venue_currency()).upper()
+    if amount_cents <= 0:
+        raise PaymentAttemptError("refund amount must be positive.")
+
+    charge = _resolve_charge_attempt(
+        db, payment_id=payment_id, charge_attempt_id=charge_attempt_id, provider=provider)
+    _validate_refund_currency(currency, charge)
+    charge_attempt_id = charge.id if charge is not None else None
+    fingerprint = refund_intent_fingerprint(
+        payment_id=payment_id, charge_attempt_id=charge_attempt_id, provider=provider,
+        amount_cents=amount_cents, currency=currency)
+
+    if idempotency_key:
+        existing = _by_key(db, idempotency_key)
+        if existing is not None:
+            _assert_same_intent(existing, fingerprint)
+            return existing
+    else:
+        idempotency_key = new_idempotency_key()
+
+    refund = RefundAttempt(
+        payment_id=payment_id, charge_attempt_id=charge_attempt_id,
+        staff_id=staff_id, provider=provider, amount_cents=amount_cents,
+        currency=currency, idempotency_key=idempotency_key,
+        intent_fingerprint=fingerprint, status=RefundAttemptStatus.CREATED,
+    )
+    db.add(refund)
+    try:
+        db.commit()
+    except IntegrityError:
+        db.rollback()
+        existing = _by_key(db, idempotency_key)
+        if existing is None:
+            raise
+        _assert_same_intent(existing, fingerprint)
+        return existing
+    db.refresh(refund)
+    return refund
+
+
+def _assert_same_intent(existing: RefundAttempt, fingerprint: str) -> None:
+    if existing.intent_fingerprint and existing.intent_fingerprint != fingerprint:
+        raise IdempotencyConflict(
+            f"refund idempotency key {existing.idempotency_key!r} was already used for a "
+            f"different refund intent (refund attempt {existing.id}).")
+
+
+def _by_key(db: Session, key: str) -> RefundAttempt | None:
+    return db.execute(
+        select(RefundAttempt).where(RefundAttempt.idempotency_key == key)
+    ).scalar_one_or_none()
+
+
+def _require_external_refund_id(
+    refund: RefundAttempt, provider_refund_id: str | None, new_status: str,
+) -> None:
+    """An external refund cannot enter PROCESSOR_PENDING or COMPLETED without a
+    durable provider_refund_id — the refund equivalent of the external-approval
+    invariant (finding #1). Manual/local refunds need none. If the outcome is
+    genuinely unknown, route to REQUIRES_RECONCILIATION instead. Effective id
+    combines what's persisted with what this transition supplies."""
+    from app.services.payment_providers import get_provider
+    if not get_provider(refund.provider).is_external:
+        return
+    if not (provider_refund_id or refund.provider_refund_id):
+        raise PaymentAttemptError(
+            f"an external refund cannot enter {new_status} without a provider_refund_id "
+            "(#1); use REQUIRES_RECONCILIATION if the processor outcome is unknown.")
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
+    _from_resolver: bool = False,
+) -> RefundAttempt:
+    """Concurrency-safe compare-and-swap transition for a refund attempt.
+
+    Leaving REQUIRES_RECONCILIATION is gated by ``resolve_refund_reconciliation``
+    (finding #1): a plain transition cannot resolve an ambiguous refund."""
+    if refund.status == RefundAttemptStatus.REQUIRES_RECONCILIATION and not _from_resolver:
+        raise PaymentAttemptError(
+            "resolve a REQUIRES_RECONCILIATION refund via resolve_refund_reconciliation(), "
+            "not transition_refund().")
+    if new_status in (RefundAttemptStatus.PROCESSOR_PENDING, RefundAttemptStatus.COMPLETED):
+        _require_external_refund_id(refund, provider_refund_id, new_status)
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
+    except IntegrityError as exc:
+        db.rollback()
+        raise TransitionConflict(
+            f"refund transition {expected} -> {new_status} conflicted "
+            f"(uniqueness): {getattr(exc, 'orig', exc)}"
+        ) from exc
+    except OperationalError as exc:
+        db.rollback()
+        if is_lock_conflict(exc):
+            raise TransitionConflict(
+                f"refund transition {expected} -> {new_status} lost a lock/deadlock race: "
+                f"{getattr(exc, 'orig', exc)}"
+            ) from exc
+        raise  # genuine infrastructure failure — propagate
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
+
+
+def resolve_refund_reconciliation(
+    db: Session,
+    refund: RefundAttempt,
+    *,
+    resolved_status: str,
+    note: str,
+    actor: Staff | None = None,
+    automatic: bool = False,
+    provider_evidence: str | None = None,
+    provider_refund_id: str | None = None,
+) -> RefundAttempt:
+    """Resolve an ambiguous refund under explicit authority, with an audit trail
+    (finding #1) — mirrors resolve_reconciliation for charge attempts. The only
+    exit from a REQUIRES_RECONCILIATION refund.
+
+    * **Manual** — ``actor`` must be a Staff with an OWNER/MANAGER role.
+    * **Automatic** — ``automatic=True`` (a recovery worker) MUST supply
+      ``provider_evidence``; a note alone cannot resolve.
+    """
+    if refund.status != RefundAttemptStatus.REQUIRES_RECONCILIATION:
+        raise PaymentAttemptError(
+            "resolve_refund_reconciliation only applies to a REQUIRES_RECONCILIATION refund.")
+    if resolved_status not in (
+        RefundAttemptStatus.COMPLETED,
+        RefundAttemptStatus.FAILED,
+        RefundAttemptStatus.REJECTED,
+    ):
+        raise PaymentAttemptError(f"cannot resolve a refund to {resolved_status!r}.")
+    if not (note or "").strip():
+        raise PaymentAttemptError("refund reconciliation resolution requires a note.")
+    # Resolving an external refund to COMPLETED needs an authoritative refund id —
+    # check up front so a rejection leaves no half-written audit/reconciliation.
+    if resolved_status == RefundAttemptStatus.COMPLETED:
+        _require_external_refund_id(refund, provider_refund_id, resolved_status)
+
+    if automatic:
+        if not (provider_evidence or "").strip():
+            raise ReconciliationAuthorityError(
+                "automatic refund reconciliation must supply provider_evidence — a note "
+                "alone cannot resolve an ambiguous refund (#1).")
+        actor_id, resolved_by = None, "system:auto"
+    else:
+        if actor is None or actor.role not in _RECON_ROLES:
+            raise ReconciliationAuthorityError(
+                "manual refund reconciliation requires an OWNER/MANAGER actor (#1).")
+        actor_id, resolved_by = actor.id, actor.name
+
+    detail = f"refund {refund.id} -> {resolved_status}"
+    if provider_evidence:
+        detail += f" [evidence: {provider_evidence}]"
+    detail = f"{detail}: {note.strip()}"[:300]
+    db.add(AuditEvent(staff_id=actor_id, action="reconcile_refund_attempt", detail=detail))
+
+    refund.reconciled_at = datetime.now()
+    refund.reconciled_by = resolved_by[:60]
+    refund.reconciliation_note = (
+        (note.strip() + (f" | evidence: {provider_evidence}" if provider_evidence else ""))[:300])
+    return transition_refund(
+        db, refund, resolved_status, provider_refund_id=provider_refund_id, _from_resolver=True)
diff --git a/app/services/square.py b/app/services/square.py
index 3809d81..7e149d3 100644
--- a/app/services/square.py
+++ b/app/services/square.py
@@ -43,6 +43,63 @@ class SquareError(Exception):
     """Any non-2xx from Square, or a checkout that ended without completing."""
 
 
+class SquareTransportError(SquareError):
+    """The request may or may not have reached Square — a timeout, a dropped
+    connection, or a 5xx/throttle. The processor outcome is UNKNOWN, so the caller
+    must reconcile rather than assume the operation did not happen."""
+
+
+class SquareApiError(SquareError):
+    """Square returned a definitive error response (a 4xx). ``status_code``,
+    ``code`` (Square error code) and ``error_category`` let the caller classify a
+    genuine decline vs an auth/config problem, a request conflict, or a bad
+    device/location — see ``classify_charge_error``."""
+
+    def __init__(self, message: str, status_code: int,
+                 code: str | None = None, error_category: str | None = None):
+        super().__init__(message)
+        self.status_code = status_code
+        self.code = code
+        self.error_category = error_category
+
+
+# HTTP statuses whose outcome is ambiguous/retryable rather than a definitive
+# rejection (server errors + throttling + request timeout).
+_TRANSIENT_STATUS = frozenset({408, 425, 429})
+
+# Normalized create-charge error categories (finding #8). Only DECLINE is a
+# definitive "no charge, financial rejection"; everything else must NOT be
+# treated as safe-to-retry — the request may have conflicted, or config is broken.
+CHARGE_DECLINE = "decline"
+CHARGE_CONFLICT = "conflict"          # idempotency/request conflict — charge may exist
+CHARGE_AUTH_CONFIG = "auth_config"    # auth/permission/config failure
+CHARGE_INVALID_TARGET = "invalid_target"  # bad device/location/not-found
+CHARGE_UNEXPECTED = "unexpected"
+
+_DECLINE_CODES = frozenset({
+    "CARD_DECLINED", "GENERIC_DECLINE", "INSUFFICIENT_FUNDS", "CVV_FAILURE",
+    "ADDRESS_VERIFICATION_FAILURE", "CARD_EXPIRED", "INVALID_CARD",
+    "CARD_DECLINED_VERIFICATION_REQUIRED", "PAN_FAILURE",
+})
+
+
+def classify_charge_error(exc: SquareApiError) -> str:
+    """Map a definitive Square 4xx to a normalized charge-error category so the
+    core never infers a new charge is safe merely because the status was 4xx."""
+    code = (exc.code or "").upper()
+    cat = (exc.error_category or "").upper()
+    sc = exc.status_code
+    if sc == 409 or code in ("IDEMPOTENCY_KEY_REUSED", "CONFLICT"):
+        return CHARGE_CONFLICT
+    if cat == "PAYMENT_METHOD_ERROR" or sc == 402 or code in _DECLINE_CODES:
+        return CHARGE_DECLINE
+    if cat == "AUTHENTICATION_ERROR" or sc in (401, 403) or code in ("UNAUTHORIZED", "FORBIDDEN"):
+        return CHARGE_AUTH_CONFIG
+    if sc == 404 or code in ("NOT_FOUND", "DEVICE_UNAVAILABLE", "INVALID_LOCATION"):
+        return CHARGE_INVALID_TARGET
+    return CHARGE_UNEXPECTED
+
+
 # --------------------------------------------------------------------------
 # Configuration (read at call time so env changes need no restart of imports)
 # --------------------------------------------------------------------------
@@ -103,10 +160,23 @@ def _request(method: str, path: str, payload: dict | None = None) -> dict:
     try:
         resp = httpx.request(method, url, headers=_headers(), json=payload, timeout=_TIMEOUT)
     except httpx.HTTPError as exc:
-        raise SquareError(f"Could not reach the card terminal service: {exc}") from exc
-    if resp.status_code // 100 != 2:
-        raise SquareError(_error_message(resp))
-    return resp.json()
+        # Never reached a response — the request may still have been processed.
+        raise SquareTransportError(f"Could not reach the card terminal service: {exc}") from exc
+    code = resp.status_code
+    if code // 100 == 2:
+        return resp.json()
+    if code >= 500 or code in _TRANSIENT_STATUS:
+        # Server-side/throttle: the operation may have taken effect. Ambiguous.
+        raise SquareTransportError(_error_message(resp))
+    # A definitive 4xx: capture the Square error code/category for classification.
+    err = {}
+    try:
+        errs = resp.json().get("errors") or []
+        err = errs[0] if errs else {}
+    except Exception:  # noqa: BLE001
+        pass
+    raise SquareApiError(_error_message(resp), status_code=code,
+                         code=err.get("code"), error_category=err.get("category"))
 
 
 def _error_message(resp: httpx.Response) -> str:
@@ -130,12 +200,19 @@ def create_checkout(
     note: str = "",
     allow_tip: bool = True,
     device_id: str | None = None,
+    idempotency_key: str | None = None,
+    currency_code: str | None = None,
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
@@ -149,9 +226,10 @@ def create_checkout(
     else:
         device_options["tip_settings"] = {"allow_tipping": False}
     body = {
-        "idempotency_key": str(uuid.uuid4()),
+        "idempotency_key": idempotency_key or str(uuid.uuid4()),
         "checkout": {
-            "amount_money": {"amount": int(amount_cents), "currency": currency()},
+            "amount_money": {"amount": int(amount_cents),
+                             "currency": (currency_code or currency()).upper()},
             "device_options": device_options,
             "deadline_duration": "PT5M",
         },
@@ -179,6 +257,39 @@ def get_payment(payment_id: str) -> dict:
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
+    currency_code: str | None = None,
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
+        "amount_money": {"amount": int(amount_cents),
+                         "currency": (currency_code or currency()).upper()},
+    }
+    if reason:
+        body["reason"] = reason[:192]
+    return _request("POST", "/v2/refunds", body)["refund"]
+
+
 def wait_for_checkout(checkout_id: str, timeout_s: float = 120.0, interval_s: float = 1.5) -> dict:
     """Poll until the checkout completes, cancels, or we give up.
 
@@ -195,6 +306,67 @@ def wait_for_checkout(checkout_id: str, timeout_s: float = 120.0, interval_s: fl
     return checkout
 
 
+def _safe_int(v) -> int | None:
+    """Strictly parse an untrusted processor money amount (minor units), which
+    must be an integer number of cents. Returns None (never a crash, never a
+    silent coercion) for anything that isn't an exact integer — so a float, bool,
+    decimal string, or arbitrary object maps to incomplete evidence and reconciles
+    (finding #2). Accepts a Python int (not bool) or an integer-only string."""
+    if isinstance(v, bool):
+        return None                      # bool is an int subclass — reject explicitly
+    if isinstance(v, int):
+        return v
+    if isinstance(v, str):
+        s = v.strip()
+        digits = s[1:] if s[:1] in "+-" else s
+        if digits.isdigit():             # integer-only string, no '.', no NaN/inf
+            return int(s)
+    return None
+
+
+def completed_payment_evidence(checkout: dict) -> dict:
+    """Authoritative amount/currency/tip evidence for a COMPLETED checkout, read
+    from the underlying Square Payment (not local config — finding #6).
+
+    Returns a dict with, when the payment is readable:
+      captured_total_cents  — what Square captured (base + tip)
+      tip_cents             — the tip the guest added on the terminal
+      base_cents            — captured_total - tip (comparable to our pre-tip
+                              expected_total_cents — finding #17)
+      currency              — the processor's currency (e.g. 'CAD')
+      brand, last4          — card details
+    Missing/unreadable fields are None so settlement never compares against
+    fabricated evidence.
+    """
+    none = {"captured_total_cents": None, "tip_cents": None, "base_cents": None,
+            "currency": None, "brand": None, "last4": None}
+    payment_ids = checkout.get("payment_ids") or []
+    if not payment_ids:
+        return dict(none)
+    try:
+        pay = get_payment(payment_ids[0])
+    except SquareError:
+        return dict(none)  # unreadable — leave evidence None, do not guess
+    # The payment object is untrusted input (finding #4): parse defensively so a
+    # malformed payload maps to None evidence -> reconciliation, never a crash.
+    try:
+        total_money = pay.get("total_money") or {}
+        tip_money = pay.get("tip_money") or {}
+        total = _safe_int(total_money.get("amount"))
+        raw_tip = tip_money.get("amount")
+        tip = 0 if raw_tip is None else _safe_int(raw_tip)   # None if present but malformed
+        raw_cur = total_money.get("currency") or (pay.get("amount_money") or {}).get("currency")
+        currency = raw_cur.upper() if isinstance(raw_cur, str) and raw_cur.strip() else None
+        card = (pay.get("card_details") or {}).get("card") or {}
+        base = (total - tip) if (total is not None and tip is not None) else None
+        return {
+            "captured_total_cents": total, "tip_cents": tip, "base_cents": base,
+            "currency": currency, "brand": card.get("card_brand"), "last4": card.get("last_4"),
+        }
+    except Exception:  # noqa: BLE001 — any malformed payload -> reconcile
+        return dict(none)
+
+
 def tip_and_card(checkout: dict) -> tuple[int, str | None, str | None]:
     """From a COMPLETED checkout, return (tip_cents, card_brand, card_last4).
 
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
index 0000000..92a892e
--- /dev/null
+++ b/tests/test_payment_attempts.py
@@ -0,0 +1,268 @@
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
+from app.models.oltp import AuditEvent, PaymentAttempt, PaymentAttemptStatus as S, Staff
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
+def test_external_approval_requires_evidence():
+    """The state machine itself (not just the Square adapter) refuses external
+    PROCESSOR_APPROVED without payment id + amount + valid currency (#2)."""
+    def ext_attempt(key):
+        return pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
+                                 staff_id=ids["staff_id"], expected_total_cents=1000,
+                                 subtotal_cents=1000, idempotency_key=key)
+    db, ids = _db()
+    # payment id only -> reject
+    a = ext_attempt("e1"); pa.transition(db, a, S.PROCESSOR_PENDING)
+    for label, kw in [
+        ("payment id only", {"provider_payment_id": "p"}),
+        ("missing amount", {"provider_payment_id": "p", "processor_currency": "CAD"}),
+        ("missing currency", {"provider_payment_id": "p", "processor_amount_cents": 1000}),
+        ("missing payment id", {"processor_amount_cents": 1000, "processor_currency": "CAD"}),
+    ]:
+        raised = False
+        try:
+            pa.transition(db, a, S.PROCESSOR_APPROVED, **kw)
+        except pa.PaymentAttemptError:
+            raised = True
+        check(raised, f"external approval with {label} rejected (#2)")
+    # complete evidence -> approve
+    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="p",
+                  processor_amount_cents=1000, processor_currency="CAD")
+    check(a.status == S.PROCESSOR_APPROVED, "external approval with complete evidence succeeds (#2)")
+    # manual provider approves with no external evidence
+    m = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=500, idempotency_key="m1")
+    pa.transition(db, m, S.PROCESSOR_PENDING)
+    pa.transition(db, m, S.PROCESSOR_APPROVED)
+    check(m.status == S.PROCESSOR_APPROVED, "manual provider approval remains valid (#2)")
+
+
+def test_currency_defaults_to_venue():
+    db, ids = _db()
+    old = os.environ.get("VENUE_CURRENCY")
+    os.environ["VENUE_CURRENCY"] = "USD"
+    try:
+        a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
+                              staff_id=ids["staff_id"], expected_total_cents=1000,
+                              subtotal_cents=1000)  # currency omitted
+        check(a.currency == "USD", "omitted currency defaults to venue currency, not CAD (#3)")
+    finally:
+        os.environ.pop("VENUE_CURRENCY", None) if old is None else os.environ.__setitem__("VENUE_CURRENCY", old)
+
+
+def test_processor_evidence_is_write_once():
+    db, ids = _db()
+    a = _mk(db, ids)
+    pa.transition(db, a, S.PROCESSOR_PENDING)
+    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="p1",
+                  processor_amount_cents=1000, processor_currency="CAD")
+    # Same value on the next transition is fine (idempotent).
+    pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"],
+                  processor_amount_cents=1000)
+    check(a.processor_amount_cents == 1000 and a.processor_currency == "CAD",
+          "processor evidence persisted")
+    # A *different* processor amount must not overwrite the evidence. Use a
+    # separate attempt and a payment_id-free transition so the conflict can only
+    # come from the write-once evidence guard, not the unique payment_id.
+    b = _mk(db, ids, key="k2")
+    pa.transition(db, b, S.PROCESSOR_PENDING)
+    pa.transition(db, b, S.PROCESSOR_APPROVED, processor_amount_cents=1000, processor_currency="CAD")
+    raised = False
+    try:
+        pa.transition(db, b, S.REQUIRES_RECONCILIATION, processor_amount_cents=9999)
+    except pa.TransitionConflict:
+        raised = True
+    check(raised, "processor amount evidence cannot be overwritten with a different value (#8)")
+
+
+def test_reconciliation_authority_and_audit():
+    db, ids = _db()
+    a = _mk(db, ids)
+    pa.transition(db, a, S.REQUIRES_RECONCILIATION, last_error="lost")
+
+    # plain transition cannot leave reconciliation
+    raised = False
+    try:
+        pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"])
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "plain transition cannot leave REQUIRES_RECONCILIATION")
+
+    # a waiter (non-manager) cannot resolve
+    waiter = Staff(name="Wanda", role="waiter", pin_code="x")
+    db.add(waiter); db.commit()
+    raised = False
+    try:
+        pa.resolve_reconciliation(db, a, resolved_status=S.SETTLED, note="ok",
+                                  actor=waiter, payment_id=ids["payment_id"])
+    except pa.ReconciliationAuthorityError:
+        raised = True
+    check(raised, "a non-manager actor cannot resolve reconciliation (#13)")
+
+    # automatic resolution needs provider evidence, not a bare note
+    raised = False
+    try:
+        pa.resolve_reconciliation(db, a, resolved_status=S.SETTLED, note="just settle it",
+                                  automatic=True, payment_id=ids["payment_id"])
+    except pa.ReconciliationAuthorityError:
+        raised = True
+    check(raised, "automatic resolution needs provider evidence, not a note alone (#13)")
+
+    # an authorized owner resolves, with an audit event
+    owner = db.get(Staff, ids["staff_id"])  # seeded as owner
+    pa.resolve_reconciliation(db, a, resolved_status=S.SETTLED,
+                              note="verified in Square dashboard", actor=owner,
+                              provider_evidence="sq_txn_123", payment_id=ids["payment_id"])
+    check(a.status == S.SETTLED and a.reconciled_by == owner.name,
+          "authorized manager settles and records who/why")
+    n = db.query(AuditEvent).filter_by(action="reconcile_payment_attempt").count()
+    check(n == 1, "an audit event is written for the resolution (#13)")
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
+        test_external_approval_requires_evidence,
+        test_currency_defaults_to_venue,
+        test_processor_evidence_is_write_once,
+        test_reconciliation_authority_and_audit,
+    ):
+        print(f"- {fn.__name__}")
+        fn()
+    if _failures:
+        print(f"\n{len(_failures)} FAILED")
+        sys.exit(1)
+    print("\nall payment-attempt tests passed")
diff --git a/tests/test_payment_providers.py b/tests/test_payment_providers.py
new file mode 100644
index 0000000..d53d29e
--- /dev/null
+++ b/tests/test_payment_providers.py
@@ -0,0 +1,529 @@
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
+from app.services import payment_attempts as pa
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
+def test_square_poll_reads_processor_evidence():
+    # Payment captured 2300 total = 2000 pre-tip base + 300 tip, in CAD.
+    def fake(method, path, payload=None):
+        if path.endswith("/chk_1"):
+            return {"checkout": {"id": "chk_1", "status": "COMPLETED", "payment_ids": ["pay_9"]}}
+        if path.startswith("/v2/payments/"):
+            return {"payment": {"total_money": {"amount": 2300, "currency": "CAD"},
+                                "tip_money": {"amount": 300},
+                                "card_details": {"card": {"card_brand": "VISA", "last_4": "4242"}}}}
+        raise AssertionError(path)
+
+    orig = _fake(fake)
+    try:
+        res = pp.get_provider("square_terminal").poll("chk_1")
+        check(res.status == S.PROCESSOR_APPROVED, "COMPLETED+payment_id -> approved")
+        check(res.provider_payment_id == "pay_9", "captures payment id")
+        check(res.processor_amount_cents == 2000,
+              "processor amount is the PRE-TIP base = total - tip (#17)")
+        check(res.processor_currency == "CAD",
+              "processor currency comes from the Payment object, not config (#6)")
+        check(res.tip_cents == 300 and res.card_last4 == "4242", "reads tip + last4")
+    finally:
+        square._request = orig
+
+
+def test_approval_requires_complete_evidence():
+    """COMPLETED only settles with authoritative payment id + amount + currency;
+    anything missing -> reconciliation (finding #3)."""
+    def completed(payment_body):
+        def fake(method, path, payload=None):
+            if path.endswith("/chk_1"):
+                return {"checkout": {"id": "chk_1", "status": "COMPLETED", "payment_ids": ["pay_9"]}}
+            if path.startswith("/v2/payments/"):
+                if payment_body == "TRANSPORT":
+                    raise square.SquareTransportError("timeout reading payment")
+                if payment_body == "DEFINITIVE":
+                    raise square.SquareApiError("not found", status_code=404)
+                return {"payment": payment_body}
+            raise AssertionError(path)
+        return fake
+
+    good = {"total_money": {"amount": 2300, "currency": "CAD"}, "tip_money": {"amount": 300}}
+    no_amount = {"tip_money": {"amount": 300}}                       # no total_money
+    no_currency = {"total_money": {"amount": 2300}, "tip_money": {"amount": 300}}
+
+    cases = [
+        (good, S.PROCESSOR_APPROVED, "complete evidence -> APPROVED"),
+        ("TRANSPORT", S.REQUIRES_RECONCILIATION, "payment lookup transport failure -> reconcile"),
+        ("DEFINITIVE", S.REQUIRES_RECONCILIATION, "payment lookup definitive failure -> reconcile"),
+        (no_amount, S.REQUIRES_RECONCILIATION, "missing processor amount -> reconcile"),
+        (no_currency, S.REQUIRES_RECONCILIATION, "missing processor currency -> reconcile"),
+    ]
+    for body, expected, label in cases:
+        orig = _fake(completed(body))
+        try:
+            res = pp.get_provider("square_terminal").poll("chk_1")
+            check(res.status == expected, label + " (#3)")
+        finally:
+            square._request = orig
+
+
+def test_evidence_semantic_validation():
+    """Even with a payment id + currency present, incoherent evidence (negative
+    amount, tip > total, malformed currency) must not approve (#4)."""
+    def poll_with(payment):
+        def fake(method, path, payload=None):
+            if path.endswith("/chk_1"):
+                return {"checkout": {"id": "chk_1", "status": "COMPLETED", "payment_ids": ["pay_9"]}}
+            if path.startswith("/v2/payments/"):
+                return {"payment": payment}
+            raise AssertionError(path)
+        return fake
+
+    cases = [
+        ({"total_money": {"amount": 1300, "currency": "CAD"}, "tip_money": {"amount": 300}},
+         S.PROCESSOR_APPROVED, "valid tipped evidence -> approve"),
+        ({"total_money": {"amount": 1000, "currency": "CAD"}, "tip_money": {"amount": 0}},
+         S.PROCESSOR_APPROVED, "valid zero-tip evidence -> approve"),
+        ({"total_money": {"amount": -500, "currency": "CAD"}, "tip_money": {"amount": 0}},
+         S.REQUIRES_RECONCILIATION, "negative total -> reconcile"),
+        ({"total_money": {"amount": 1000, "currency": "CAD"}, "tip_money": {"amount": -100}},
+         S.REQUIRES_RECONCILIATION, "negative tip -> reconcile"),
+        ({"total_money": {"amount": 1000, "currency": "CAD"}, "tip_money": {"amount": 1500}},
+         S.REQUIRES_RECONCILIATION, "tip > total -> reconcile"),
+        ({"total_money": {"amount": 1000, "currency": "US"}, "tip_money": {"amount": 0}},
+         S.REQUIRES_RECONCILIATION, "malformed currency -> reconcile"),
+    ]
+    for payment, expected, label in cases:
+        orig = _fake(poll_with(payment))
+        try:
+            res = pp.get_provider("square_terminal").poll("chk_1")
+            check(res.status == expected, label + " (#4)")
+        finally:
+            square._request = orig
+
+
+def test_evidence_parse_hardening():
+    """Untrusted processor payloads must not crash polling; malformed numeric
+    values map to reconciliation and currency is normalized (#4)."""
+    def poll_with(payment):
+        def fake(method, path, payload=None):
+            if path.endswith("/chk_1"):
+                return {"checkout": {"id": "chk_1", "status": "COMPLETED", "payment_ids": ["pay_9"]}}
+            if path.startswith("/v2/payments/"):
+                return {"payment": payment}
+            raise AssertionError(path)
+        return fake
+
+    cases = [
+        ({"total_money": {"amount": "NaN", "currency": "CAD"}, "tip_money": {"amount": 0}},
+         S.REQUIRES_RECONCILIATION, "non-numeric total -> reconcile"),
+        ({"total_money": {"amount": 1000, "currency": "CAD"}, "tip_money": {"amount": "xx"}},
+         S.REQUIRES_RECONCILIATION, "non-numeric tip -> reconcile"),
+        ({"total_money": {"amount": [1, 2], "currency": "CAD"}, "tip_money": {"amount": 0}},
+         S.REQUIRES_RECONCILIATION, "unexpected numeric type -> reconcile"),
+        ("garbage-not-a-dict",
+         S.REQUIRES_RECONCILIATION, "malformed payment object -> reconcile"),
+        ({"total_money": {"amount": 1000, "currency": "cad"}, "tip_money": {"amount": 0}},
+         S.PROCESSOR_APPROVED, "lowercase currency normalized -> approve"),
+    ]
+    for payment, expected, label in cases:
+        orig = _fake(poll_with(payment))
+        try:
+            res = pp.get_provider("square_terminal").poll("chk_1")
+            check(res.status == expected, label + " (#4)")
+        finally:
+            square._request = orig
+
+
+def test_strict_money_parsing():
+    """Processor money is an integer number of minor units; floats/bools/decimal
+    strings must not be silently coerced (#2)."""
+    si = square._safe_int
+    check(si(1000) == 1000, "int 1000 accepted (#2)")
+    check(si("1000") == 1000, "integer string accepted (#2)")
+    check(si("-5") == -5, "signed integer string accepted (#2)")
+    check(si(1000.9) is None, "float rejected (#2)")
+    check(si(True) is None, "bool True rejected (#2)")
+    check(si(False) is None, "bool False rejected (#2)")
+    check(si("10.5") is None, "decimal string rejected (#2)")
+    check(si({"x": 1}) is None, "arbitrary object rejected (#2)")
+    check(si(float("nan")) is None, "NaN rejected (#2)")
+
+
+def test_charge_and_refund_forward_currency():
+    seen = {}
+
+    def fake(method, path, payload=None):
+        seen[path] = payload
+        if "checkouts" in path:
+            return {"checkout": {"id": "chk_1", "status": "PENDING"}}
+        return {"refund": {"id": "rf_1", "status": "COMPLETED"}}
+
+    orig = _fake(fake)
+    try:
+        sq = pp.get_provider("square_terminal")
+        sq.charge(amount_cents=1000, currency="USD", idempotency_key="k")
+        sq.refund(amount_cents=500, currency="USD", idempotency_key="k2", provider_payment_id="pay_9")
+        check(seen["/v2/terminals/checkouts"]["checkout"]["amount_money"]["currency"] == "USD",
+              "charge forwards per-operation currency to Square (#5)")
+        check(seen["/v2/refunds"]["amount_money"]["currency"] == "USD",
+              "refund forwards per-operation currency to Square (#5)")
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
+def test_charge_transport_ambiguity_reconciles():
+    def fake(method, path, payload=None):
+        raise square.SquareTransportError("timeout after send")
+    orig = _fake(fake)
+    try:
+        res = pp.get_provider("square_terminal").charge(
+            amount_cents=2000, currency="CAD", idempotency_key="k")
+        check(res.status == S.REQUIRES_RECONCILIATION,
+              "charge transport timeout -> reconciliation, never FAILED (#4)")
+    finally:
+        square._request = orig
+
+
+def test_charge_definitive_decline_fails():
+    def fake(method, path, payload=None):
+        raise square.SquareApiError("card declined", status_code=402)
+    orig = _fake(fake)
+    try:
+        res = pp.get_provider("square_terminal").charge(
+            amount_cents=2000, currency="CAD", idempotency_key="k")
+        check(res.status == S.FAILED, "definitive 4xx decline -> FAILED (#4)")
+    finally:
+        square._request = orig
+
+
+def test_square_4xx_classification():
+    """Only a definitive financial decline is FAILED; conflict/auth/config/
+    invalid/unexpected 4xx -> reconciliation, never safe-to-retry (#8)."""
+    def err(status, code=None, cat=None):
+        def fake(method, path, payload=None):
+            raise square.SquareApiError("boom", status_code=status, code=code, error_category=cat)
+        return fake
+
+    cases = [
+        ((402, None, "PAYMENT_METHOD_ERROR"), S.FAILED, "402 payment-method decline -> FAILED"),
+        ((400, "CARD_DECLINED", None), S.FAILED, "CARD_DECLINED -> FAILED"),
+        ((409, "IDEMPOTENCY_KEY_REUSED", None), S.REQUIRES_RECONCILIATION, "409 conflict -> reconcile (charge may exist)"),
+        ((401, None, "AUTHENTICATION_ERROR"), S.REQUIRES_RECONCILIATION, "401 auth/config -> reconcile, not safe-retry"),
+        ((404, "NOT_FOUND", None), S.REQUIRES_RECONCILIATION, "404 invalid target -> reconcile"),
+        ((400, "SOMETHING_ODD", None), S.REQUIRES_RECONCILIATION, "unexpected 4xx -> reconcile"),
+    ]
+    for (status, code, cat), expected, label in cases:
+        orig = _fake(err(status, code, cat))
+        try:
+            res = pp.get_provider("square_terminal").charge(
+                amount_cents=1000, currency="CAD", idempotency_key="k")
+            check(res.status == expected, label + " (#8)")
+        finally:
+            square._request = orig
+
+
+def test_unsupported_cancel_is_explicit():
+    # Manual does not advertise CANCEL and must not inherit a false success (#3).
+    raised = False
+    try:
+        pp.get_provider("manual").cancel(provider_checkout_id="x")
+    except NotImplementedError:
+        raised = True
+    check(raised, "an unsupported provider cancel raises, never returns ok=True (#3)")
+    check(pp.Capability.CANCEL in pp.get_provider("square_terminal").capabilities,
+          "Square advertises CANCEL and implements it (#3)")
+
+
+def test_cancel_capability_must_be_backed():
+    class NoCancelPay(pp.PaymentProvider):
+        key = "no_cancel_pay"; is_external = True
+        capabilities = frozenset({pp.Capability.CANCEL})  # advertises but never overrides cancel()
+        def charge(self, *, amount_cents, currency, idempotency_key, reference="", note="", tip_cents=0):
+            return pp.ChargeResult(status=S.PROCESSOR_APPROVED)
+        def refund(self, *, amount_cents, currency, idempotency_key, provider_payment_id=None):
+            return pp.RefundResult(status=R.COMPLETED)
+    raised = False
+    try:
+        pp.register(NoCancelPay())
+    except ValueError:
+        raised = True
+    check(raised, "advertising CANCEL without implementing cancel() is rejected (#3)")
+
+
+def test_unknown_capability_name_rejected():
+    class TeleportPay(pp.PaymentProvider):
+        key = "teleport_pay"; is_external = True
+        capabilities = frozenset({"teleport"})  # not in the vocabulary
+        def charge(self, *, amount_cents, currency, idempotency_key, reference="", note="", tip_cents=0):
+            return pp.ChargeResult(status=S.PROCESSOR_APPROVED)
+        def refund(self, *, amount_cents, currency, idempotency_key, provider_payment_id=None):
+            return pp.RefundResult(status=R.COMPLETED)
+    raised = False
+    try:
+        pp.register(TeleportPay())
+    except ValueError as exc:
+        raised = "unknown" in str(exc).lower()
+    check(raised, "a provider advertising an unknown capability name is rejected (#7)")
+
+
+def test_poll_transient_stays_pending_definitive_reconciles():
+    def transient(method, path, payload=None):
+        raise square.SquareTransportError("503 from Square")
+
+    def definitive(method, path, payload=None):
+        raise square.SquareApiError("not found", status_code=404)
+
+    orig = _fake(transient)
+    try:
+        r = pp.get_provider("square_terminal").poll("chk_1")
+        check(r.status == S.PROCESSOR_PENDING, "transient poll error stays PENDING (#9)")
+    finally:
+        square._request = orig
+    orig = _fake(definitive)
+    try:
+        r = pp.get_provider("square_terminal").poll("chk_1")
+        check(r.status == S.REQUIRES_RECONCILIATION,
+              "definitive poll error -> reconciliation, not PENDING forever (#9)")
+    finally:
+        square._request = orig
+
+
+def test_cancel_status_mapping():
+    cases = {
+        "CANCELED": (True, False),
+        "COMPLETED": (False, True),   # cancel failed; payment likely exists
+        "PENDING": (False, True),     # ambiguous
+        "WEIRD": (False, True),       # unknown
+    }
+    for st, (ok, recon) in cases.items():
+        def fake(method, path, payload=None, _s=st):
+            return {"checkout": {"id": "chk_1", "status": _s}}
+        orig = _fake(fake)
+        try:
+            res = pp.get_provider("square_terminal").cancel(provider_checkout_id="chk_1")
+            check(res.ok == ok and res.requires_reconciliation == recon,
+                  f"cancel status {st} -> ok={ok}, reconcile={recon} (#11)")
+        finally:
+            square._request = orig
+
+
+def test_operationalerror_classification():
+    # Only a lock/deadlock is a transition conflict; infra failures propagate (#12).
+    class FakeOrig:
+        def __init__(self, sqlstate): self.sqlstate = sqlstate
+    class FakeOpErr(Exception):
+        def __init__(self, sqlstate): self.orig = FakeOrig(sqlstate)
+    check(pa.is_lock_conflict(FakeOpErr("40P01")), "deadlock (40P01) classified as lock conflict")
+    check(pa.is_lock_conflict(FakeOpErr("55P03")), "lock_not_available (55P03) is a conflict")
+    check(not pa.is_lock_conflict(FakeOpErr("08006")), "connection failure (08006) is NOT a conflict")
+
+
+class AcmePay(pp.PaymentProvider):
+    key = "acme_pay"; label = "Acme"; is_external = True
+    capabilities = frozenset({pp.Capability.REFUND, pp.Capability.PARTIAL_REFUND})
+
+    def charge(self, *, amount_cents, currency, idempotency_key, reference="", note="", tip_cents=0):
+        return pp.ChargeResult(status=S.PROCESSOR_APPROVED, provider_payment_id="acme_1")
+
+    def refund(self, *, amount_cents, currency, idempotency_key, provider_payment_id=None):
+        return pp.RefundResult(status=R.COMPLETED, external=True, provider_refund_id="acme_rf")
+
+
+def test_new_provider_plugs_in():
+    pp.register(AcmePay())
+    got = pp.get_provider("acme_pay")
+    check(got.label == "Acme", "new provider resolves by key")
+    check(got.charge(amount_cents=100, currency="USD", idempotency_key="k").provider_payment_id == "acme_1",
+          "the new provider drives a charge")
+
+
+def test_unsupported_capability_rejected():
+    class LiarPay(pp.PaymentProvider):
+        key = "liar_pay"; is_external = True
+        capabilities = frozenset({pp.Capability.LOOKUP})  # never implements lookup()
+        def charge(self, *, amount_cents, currency, idempotency_key, reference="", note="", tip_cents=0):
+            return pp.ChargeResult(status=S.PROCESSOR_APPROVED)
+        def refund(self, *, amount_cents, currency, idempotency_key, provider_payment_id=None):
+            return pp.RefundResult(status=R.COMPLETED)
+    raised = False
+    try:
+        pp.register(LiarPay())
+    except ValueError:
+        raised = True
+    check(raised, "a provider advertising an unimplemented capability is rejected (#10)")
+
+
+def test_duplicate_registry_key_rejected():
+    raised = False
+    try:
+        pp.register(AcmePay())  # already registered above
+    except ValueError:
+        raised = True
+    check(raised, "duplicate provider registry key is rejected (#18)")
+    pp.register(AcmePay(), override=True)  # deliberate override is allowed
+    check(pp.get_provider("acme_pay") is not None, "override=True replaces deliberately")
+
+
+def test_builtin_capabilities_are_backed():
+    for prov in pp.available():
+        for cap, method in pp._CAPABILITY_METHOD.items():
+            if cap in prov.capabilities:
+                impl = getattr(type(prov), method, None)
+                check(impl is not None and impl is not getattr(pp.PaymentProvider, method),
+                      f"{prov.key} backs advertised {cap} with {method}()")
+
+
+if __name__ == "__main__":
+    for fn in (
+        test_registry_and_capabilities,
+        test_manual_is_local_with_amount,
+        test_square_forwards_idempotency_key,
+        test_square_poll_reads_processor_evidence,
+        test_approval_requires_complete_evidence,
+        test_evidence_semantic_validation,
+        test_evidence_parse_hardening,
+        test_strict_money_parsing,
+        test_charge_and_refund_forward_currency,
+        test_square_completed_without_payment_id_reconciles,
+        test_square_refund_state_mapping,
+        test_square_refund_without_payment_id_reconciles,
+        test_cancel_is_not_swallowed,
+        test_charge_transport_ambiguity_reconciles,
+        test_charge_definitive_decline_fails,
+        test_square_4xx_classification,
+        test_unsupported_cancel_is_explicit,
+        test_cancel_capability_must_be_backed,
+        test_unknown_capability_name_rejected,
+        test_poll_transient_stays_pending_definitive_reconciles,
+        test_cancel_status_mapping,
+        test_operationalerror_classification,
+        test_new_provider_plugs_in,
+        test_unsupported_capability_rejected,
+        test_duplicate_registry_key_rejected,
+        test_builtin_capabilities_are_backed,
+    ):
+        print(f"- {fn.__name__}")
+        fn()
+    if _failures:
+        print(f"\n{len(_failures)} FAILED")
+        sys.exit(1)
+    print("\nall payment-provider tests passed")
diff --git a/tests/test_pg_concurrency.py b/tests/test_pg_concurrency.py
new file mode 100644
index 0000000..56cae67
--- /dev/null
+++ b/tests/test_pg_concurrency.py
@@ -0,0 +1,255 @@
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
+from app.models.oltp import PaymentAttempt, PaymentAttemptStatus as S, RefundAttempt
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
+def test_concurrent_refund_same_key(engine, ids):
+    Sess = Session(engine)
+
+    def make(_i):
+        db = Sess()
+        try:
+            r = ra.create_refund_attempt(db, payment_id=ids["payment_id"],
+                                         staff_id=ids["staff_id"], provider="manual",
+                                         amount_cents=1500, idempotency_key="refund-key")
+            return r.id
+        finally:
+            db.close()
+
+    results = _run_concurrently(make, 8)
+    ids_ret = {r[1] for r in results if r[0] == "ok"}
+    errs = [r[1] for r in results if r[0] == "err"]
+    check(not errs, f"no unhandled errors under concurrent same-key refund ({errs[:1]})")
+    check(len(ids_ret) == 1, "all concurrent refund callers resolve to one attempt")
+    verify = Sess()
+    n = verify.query(RefundAttempt).filter_by(idempotency_key="refund-key").count()
+    verify.close()
+    check(n == 1, "exactly one RefundAttempt row persisted (#16)")
+
+
+def test_concurrent_refund_reconciliation(engine, ids):
+    from app.models.oltp import RefundAttemptStatus as R
+    Sess = Session(engine)
+    db0 = Sess()
+    r = ra.create_refund_attempt(db0, payment_id=ids["payment_id"], staff_id=ids["staff_id"],
+                                 provider="manual", amount_cents=500, idempotency_key="rr")
+    ra.transition_refund(db0, r, R.REQUIRES_RECONCILIATION)
+    rid = r.id
+    db0.close()
+
+    def resolve(_i):
+        db = Sess()
+        try:
+            ref = db.get(RefundAttempt, rid)
+            ra.resolve_refund_reconciliation(db, ref, resolved_status=R.COMPLETED,
+                                             note="auto", automatic=True, provider_evidence="ev")
+            return "ok"
+        finally:
+            db.close()
+
+    results = _run_concurrently(resolve, 6)
+    wins = [r for r in results if r[0] == "ok"]
+    refusals = [r for r in results if r[0] == "err" and isinstance(r[1], pa.PaymentAttemptError)]
+    check(len(wins) == 1, f"exactly one refund reconciliation wins ({len(wins)})")
+    check(len(refusals) == 5, "the losers refuse with an explicit typed error (#1)")
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
+        test_concurrent_refund_same_key,
+        test_concurrent_refund_reconciliation,
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
diff --git a/tests/test_pg_migration.py b/tests/test_pg_migration.py
new file mode 100644
index 0000000..ef13d0e
--- /dev/null
+++ b/tests/test_pg_migration.py
@@ -0,0 +1,330 @@
+"""PostgreSQL migration-UPGRADE proofs (findings #1, #2, #14, #15).
+
+Unlike the other suites (drop_all + create_all = fresh schema), this builds the
+*previous* Stage-2a `payment_attempt` shape, inserts representative rows, runs the
+real migration, and asserts the upgraded schema + preserved data. Skips (exit 0)
+without PG_TEST_DSN — upgrade behavior is Postgres-specific.
+
+Run: PG_TEST_DSN=... python tests/test_pg_migration.py
+"""
+import os
+import sys
+
+sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
+
+from tests._pay_fixture import pg_dsn
+
+from sqlalchemy import create_engine, text
+from app import migrate
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
+# The Stage-2a payment_attempt, before this round's hardening: provider is
+# VARCHAR(20) DEFAULT 'square', provider_refund_id still present, and the
+# provider-scoped UNIQUE constraints do NOT exist yet.
+LEGACY_DDL = """
+CREATE TABLE payment_attempt (
+  id SERIAL PRIMARY KEY,
+  order_id INTEGER NOT NULL,
+  seat_id INTEGER,
+  staff_id INTEGER NOT NULL,
+  provider VARCHAR(20) NOT NULL DEFAULT 'square',
+  provider_checkout_id VARCHAR(64),
+  provider_payment_id VARCHAR(64),
+  provider_refund_id VARCHAR(64),
+  idempotency_key VARCHAR(64) NOT NULL,
+  subtotal_cents INTEGER NOT NULL DEFAULT 0,
+  tax_cents INTEGER NOT NULL DEFAULT 0,
+  tip_cents INTEGER NOT NULL DEFAULT 0,
+  service_charge_cents INTEGER NOT NULL DEFAULT 0,
+  discount_cents INTEGER NOT NULL DEFAULT 0,
+  surcharge_cents INTEGER NOT NULL DEFAULT 0,
+  expected_total_cents INTEGER NOT NULL DEFAULT 0,
+  currency VARCHAR(3) NOT NULL DEFAULT 'CAD',
+  status VARCHAR(30) NOT NULL DEFAULT 'created',
+  last_error TEXT NOT NULL DEFAULT '',
+  payment_id INTEGER,
+  created_at TIMESTAMP NOT NULL DEFAULT now(),
+  updated_at TIMESTAMP NOT NULL DEFAULT now(),
+  CONSTRAINT uq_attempt_idempotency_key UNIQUE (idempotency_key),
+  CONSTRAINT uq_attempt_payment UNIQUE (payment_id)
+);
+"""
+
+
+def _build_legacy(engine, rows):
+    with engine.begin() as conn:
+        conn.execute(text("DROP TABLE IF EXISTS refund_attempt CASCADE"))
+        conn.execute(text("DROP TABLE IF EXISTS payment_attempt CASCADE"))
+        conn.execute(text(LEGACY_DDL))
+        for r in rows:
+            conn.execute(text(
+                "INSERT INTO payment_attempt (order_id, staff_id, provider, "
+                "provider_payment_id, provider_checkout_id, idempotency_key, "
+                "expected_total_cents, status) VALUES (:o,:s,:p,:pp,:pc,:k,:t,:st)"), r)
+
+
+def _constraints(engine):
+    with engine.connect() as conn:
+        return {row[0] for row in conn.execute(text(
+            "SELECT constraint_name FROM information_schema.table_constraints "
+            "WHERE table_name='payment_attempt' AND table_schema=current_schema()"))}
+
+
+def _provider_len(engine):
+    with engine.connect() as conn:
+        return conn.execute(text(
+            "SELECT character_maximum_length FROM information_schema.columns "
+            "WHERE table_name='payment_attempt' AND column_name='provider' "
+            "AND table_schema=current_schema()")).scalar_one()
+
+
+def _has_column(engine, col):
+    with engine.connect() as conn:
+        return conn.execute(text(
+            "SELECT 1 FROM information_schema.columns WHERE table_name='payment_attempt' "
+            "AND column_name=:c AND table_schema=current_schema()"), {"c": col}).first() is not None
+
+
+def _column_default(engine, table, col):
+    with engine.connect() as conn:
+        return conn.execute(text(
+            "SELECT column_default FROM information_schema.columns WHERE table_name=:t "
+            "AND column_name=:c AND table_schema=current_schema()"), {"t": table, "c": col}).scalar_one()
+
+
+# A legacy payment_instrument WITHOUT the provider column (predates it), so the
+# migration adds the column (DEFAULT 'manual') and must then backfill card_terminal.
+LEGACY_INSTRUMENT_DDL = """
+CREATE TABLE payment_instrument (
+  id SERIAL PRIMARY KEY,
+  code VARCHAR(30) UNIQUE NOT NULL,
+  name VARCHAR(60) NOT NULL,
+  instrument_type VARCHAR(20) NOT NULL,
+  is_third_party BOOLEAN DEFAULT FALSE,
+  delivery_only BOOLEAN DEFAULT FALSE
+);
+"""
+
+
+def _build_legacy_instruments(engine):
+    with engine.begin() as conn:
+        conn.execute(text("DROP TABLE IF EXISTS payment_instrument CASCADE"))
+        conn.execute(text(LEGACY_INSTRUMENT_DDL))
+        conn.execute(text("INSERT INTO payment_instrument (code, name, instrument_type) "
+                          "VALUES ('card_terminal','Card (terminal)','card'), "
+                          "('cash','Cash','cash')"))
+
+
+# payment_instrument that already HAS the provider column with an explicit value —
+# used to prove the historical backfill never overwrites a deliberate choice.
+INSTRUMENT_WITH_PROVIDER_DDL = """
+CREATE TABLE payment_instrument (
+  id SERIAL PRIMARY KEY,
+  code VARCHAR(30) UNIQUE NOT NULL,
+  name VARCHAR(60) NOT NULL,
+  instrument_type VARCHAR(20) NOT NULL,
+  is_third_party BOOLEAN DEFAULT FALSE,
+  delivery_only BOOLEAN DEFAULT FALSE,
+  provider VARCHAR(30) NOT NULL DEFAULT 'manual'
+);
+"""
+
+
+def _build_instrument_with_provider(engine, code, provider):
+    with engine.begin() as conn:
+        conn.execute(text("DROP TABLE IF EXISTS payment_instrument CASCADE"))
+        conn.execute(text(INSTRUMENT_WITH_PROVIDER_DDL))
+        conn.execute(text("INSERT INTO payment_instrument (code, name, instrument_type, provider) "
+                          "VALUES (:c,'Card','card',:p)"), {"c": code, "p": provider})
+
+
+def test_clean_upgrade(engine):
+    _build_legacy(engine, [
+        {"o": 1, "s": 1, "p": "square", "pp": "PAY_A", "pc": "CHK_A", "k": "k1", "t": 4500, "st": "settled"},
+        {"o": 2, "s": 1, "p": "square", "pp": "PAY_B", "pc": "CHK_B", "k": "k2", "t": 9500, "st": "created"},
+        {"o": 3, "s": 1, "p": "square", "pp": None, "pc": None, "k": "k3", "t": 100, "st": "created"},
+    ])
+    applied = migrate.run(engine, strict=True)
+    print("    migration applied:", [a for a in applied if "attempt" in a.lower() or "provider" in a.lower()][:8])
+
+    cons = _constraints(engine)
+    check("uq_attempt_provider_payment" in cons, "provider_payment UNIQUE constraint exists after upgrade")
+    check("uq_attempt_provider_checkout" in cons, "provider_checkout UNIQUE constraint exists after upgrade")
+    check(_provider_len(engine) == 30, "provider column widened to 30")
+    check(_has_column(engine, "intent_fingerprint"), "new column intent_fingerprint added")
+    check(_has_column(engine, "processor_currency"), "new column processor_currency added")
+    check(not _has_column(engine, "provider_refund_id"), "retired provider_refund_id dropped (all-null)")
+
+    with engine.connect() as conn:
+        n = conn.execute(text("SELECT COUNT(*) FROM payment_attempt")).scalar_one()
+        sq = conn.execute(text("SELECT COUNT(*) FROM payment_attempt WHERE provider='square_terminal'")).scalar_one()
+        keys = {r[0] for r in conn.execute(text("SELECT idempotency_key FROM payment_attempt"))}
+    check(n == 3, "all rows preserved through migration")
+    check(sq == 3, "provider backfilled 'square' -> 'square_terminal'")
+    check(keys == {"k1", "k2", "k3"}, "financial identifiers preserved")
+
+
+def test_provider_default_removed(engine):
+    _build_legacy(engine, [
+        {"o": 1, "s": 1, "p": "square", "pp": "PAY_A", "pc": "CHK_A", "k": "k1", "t": 100, "st": "created"},
+    ])
+    check(_column_default(engine, "payment_attempt", "provider") is not None,
+          "legacy default present before upgrade (sanity)")
+    migrate.run(engine, strict=True)
+    check(_column_default(engine, "payment_attempt", "provider") is None,
+          "legacy DEFAULT 'square' dropped after upgrade (#1)")
+
+
+def test_card_terminal_instrument_backfilled(engine):
+    _build_legacy(engine, [])            # payment_attempt present (migration touches it too)
+    _build_legacy_instruments(engine)
+    migrate.run(engine, strict=True)
+    with engine.connect() as conn:
+        rows = dict(conn.execute(text("SELECT code, provider FROM payment_instrument")).all())
+    check(rows.get("card_terminal") == "square_terminal",
+          "legacy card_terminal instrument backfilled to square_terminal (#2)")
+    check(rows.get("cash") == "manual", "ordinary cash instrument stays manual (#2)")
+
+
+def _instrument_provider(engine, code):
+    with engine.connect() as conn:
+        return conn.execute(text("SELECT provider FROM payment_instrument WHERE code=:c"),
+                            {"c": code}).scalar_one()
+
+
+def test_card_terminal_preserves_explicit_provider(engine):
+    # A deliberately-chosen provider must survive the historical backfill (#1).
+    _build_legacy(engine, [])
+    _build_instrument_with_provider(engine, "card_terminal", "stripe_terminal")
+    migrate.run(engine, strict=True)
+    check(_instrument_provider(engine, "card_terminal") == "stripe_terminal",
+          "card_terminal + alternate provider is left unchanged (#1)")
+    # An already-correct square_terminal is also untouched.
+    _build_legacy(engine, [])
+    _build_instrument_with_provider(engine, "card_terminal", "square_terminal")
+    migrate.run(engine, strict=True)
+    check(_instrument_provider(engine, "card_terminal") == "square_terminal",
+          "card_terminal + square_terminal unchanged (#1)")
+
+
+def test_hardening_is_idempotent(engine):
+    _build_legacy(engine, [
+        {"o": 1, "s": 1, "p": "square", "pp": "PAY_A", "pc": "CHK_A", "k": "k1", "t": 100, "st": "created"},
+    ])
+    _build_legacy_instruments(engine)
+    first = migrate.run(engine, strict=True)
+    second = migrate.run(engine, strict=True)   # re-run must change nothing semantically
+    payment_changes = [a for a in second if "payment_attempt" in a or "payment_instrument" in a
+                       or "constraint" in a.lower() or "default" in a or "backfill" in a]
+    check(payment_changes == [], f"second migration run is a no-op ({payment_changes})")
+    check(_instrument_provider(engine, "card_terminal") == "square_terminal",
+          "provider values stable across repeated runs")
+
+
+def test_hardening_atomic_rollback(engine):
+    # A duplicate provider_payment_id makes the constraint step fail AFTER the
+    # default-drop/backfill steps. Atomicity means those earlier steps roll back.
+    _build_legacy(engine, [
+        {"o": 1, "s": 1, "p": "square", "pp": "DUP", "pc": None, "k": "a1", "t": 100, "st": "created"},
+        {"o": 2, "s": 1, "p": "square", "pp": "DUP", "pc": None, "k": "a2", "t": 100, "st": "created"},
+    ])
+    check(_column_default(engine, "payment_attempt", "provider") is not None, "default present pre-run")
+    raised = False
+    try:
+        migrate.run(engine, strict=True)
+    except migrate.MigrationError:
+        raised = True
+    check(raised, "strict migration fails on the duplicate")
+    check(_column_default(engine, "payment_attempt", "provider") is not None,
+          "earlier steps rolled back: provider default still present (atomic — #2)")
+    with engine.connect() as conn:
+        providers = {r[0] for r in conn.execute(text("SELECT provider FROM payment_attempt"))}
+    check(providers == {"square"}, "provider backfill rolled back too (rows still 'square')")
+
+
+def test_nonnull_provider_refund_id_fails_strict(engine):
+    with engine.begin() as conn:
+        conn.execute(text("DROP TABLE IF EXISTS refund_attempt CASCADE"))
+        conn.execute(text("DROP TABLE IF EXISTS payment_attempt CASCADE"))
+        conn.execute(text(LEGACY_DDL))
+        conn.execute(text("INSERT INTO payment_attempt (order_id, staff_id, provider, "
+                          "provider_refund_id, idempotency_key, expected_total_cents) "
+                          "VALUES (1,1,'square','RF_OLD','k1',100)"))
+    raised = False
+    try:
+        migrate.run(engine, strict=True)
+    except migrate.MigrationError as exc:
+        raised = "provider_refund_id" in str(exc)
+    check(raised, "non-null legacy provider_refund_id fails closed under strict (#6)")
+
+
+def test_upgrade_blocks_on_duplicate_payment_id(engine):
+    _build_legacy(engine, [
+        {"o": 1, "s": 1, "p": "square", "pp": "DUP", "pc": None, "k": "d1", "t": 100, "st": "created"},
+        {"o": 2, "s": 1, "p": "square", "pp": "DUP", "pc": None, "k": "d2", "t": 100, "st": "created"},
+    ])
+    raised = False
+    try:
+        migrate.run(engine, strict=True)
+    except migrate.MigrationError as exc:
+        raised = "duplicate" in str(exc).lower()
+    check(raised, "upgrade fails closed on duplicate provider_payment_id (no silent rewrite)")
+
+
+def test_upgrade_blocks_on_duplicate_checkout_id(engine):
+    _build_legacy(engine, [
+        {"o": 1, "s": 1, "p": "square", "pp": None, "pc": "DUPC", "k": "c1", "t": 100, "st": "created"},
+        {"o": 2, "s": 1, "p": "square", "pp": None, "pc": "DUPC", "k": "c2", "t": 100, "st": "created"},
+    ])
+    raised = False
+    try:
+        migrate.run(engine, strict=True)
+    except migrate.MigrationError as exc:
+        raised = "duplicate" in str(exc).lower()
+    check(raised, "upgrade fails closed on duplicate provider_checkout_id (#15)")
+
+
+def test_duplicate_rejected_after_upgrade(engine):
+    """Once upgraded, the live constraint rejects a duplicate external id."""
+    _build_legacy(engine, [
+        {"o": 1, "s": 1, "p": "square", "pp": "P1", "pc": "C1", "k": "u1", "t": 100, "st": "created"},
+    ])
+    migrate.run(engine, strict=True)
+    raised = False
+    try:
+        with engine.begin() as conn:
+            conn.execute(text("INSERT INTO payment_attempt (order_id, staff_id, provider, "
+                              "provider_payment_id, idempotency_key, expected_total_cents) "
+                              "VALUES (9,1,'square_terminal','P1','u2',100)"))
+    except Exception:
+        raised = True
+    check(raised, "duplicate provider_payment_id rejected by the live constraint post-upgrade")
+
+
+if __name__ == "__main__":
+    if not pg_dsn():
+        print("SKIP: PG_TEST_DSN not set (migration-upgrade tests are Postgres-specific)")
+        sys.exit(0)
+    print(f"Postgres: {pg_dsn()}")
+    for fn in (test_clean_upgrade, test_provider_default_removed,
+               test_card_terminal_instrument_backfilled, test_card_terminal_preserves_explicit_provider,
+               test_hardening_is_idempotent, test_hardening_atomic_rollback,
+               test_nonnull_provider_refund_id_fails_strict,
+               test_upgrade_blocks_on_duplicate_payment_id,
+               test_upgrade_blocks_on_duplicate_checkout_id, test_duplicate_rejected_after_upgrade):
+        print(f"- {fn.__name__}")
+        eng = create_engine(pg_dsn(), future=True)
+        fn(eng)
+        eng.dispose()
+    if _failures:
+        print(f"\n{len(_failures)} FAILED")
+        sys.exit(1)
+    print("\nall migration-upgrade tests passed")
diff --git a/tests/test_refund_attempts.py b/tests/test_refund_attempts.py
new file mode 100644
index 0000000..69b2300
--- /dev/null
+++ b/tests/test_refund_attempts.py
@@ -0,0 +1,382 @@
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
+from app.models.oltp import (
+    AuditEvent, Payment, PaymentAttemptStatus as PS, PaymentInstrument,
+    RefundAttempt, RefundAttemptStatus as R, Staff,
+)
+from app.services import payment_attempts as pa
+from app.services import refund_attempts as ra
+
+
+def _settled_attempt(db, ids, provider="square_terminal"):
+    """A charge attempt walked to SETTLED against the seeded payment."""
+    a = pa.create_attempt(db, provider=provider, order_id=ids["order_id"],
+                          staff_id=ids["staff_id"], expected_total_cents=10000,
+                          subtotal_cents=10000)
+    pa.transition(db, a, PS.PROCESSOR_PENDING)
+    pa.transition(db, a, PS.PROCESSOR_APPROVED, provider_payment_id="pay_seed",
+                  processor_amount_cents=10000, processor_currency="CAD")
+    pa.transition(db, a, PS.SETTLED, payment_id=ids["payment_id"])
+    return a
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
+def _mkref(db, ids, amount, provider="manual", key=None, currency="CAD"):
+    # The seeded payment uses a cash/'manual' instrument, so a legacy refund
+    # defaults to provider='manual' to match it (#4).
+    return ra.create_refund_attempt(
+        db, payment_id=ids["payment_id"], staff_id=ids["staff_id"],
+        provider=provider, amount_cents=amount, currency=currency, idempotency_key=key,
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
+def test_same_key_different_amount_conflicts():
+    db, ids = _db()
+    _settled_attempt(db, ids)  # square_terminal
+    _mkref(db, ids, 1000, key="rk", provider="square_terminal")
+    raised = False
+    try:
+        _mkref(db, ids, 2500, key="rk", provider="square_terminal")  # same key, diff amount
+    except pa.IdempotencyConflict:
+        raised = True
+    check(raised, "same refund key + different amount raises IdempotencyConflict (#3)")
+
+
+def test_legacy_refund_provider_derivation():
+    db, ids = _db()  # seeded payment uses a 'manual' cash instrument, no attempt
+    # matching provider accepted
+    r = _mkref(db, ids, 500, provider="manual")
+    check(r.provider == "manual", "legacy manual payment refunded as manual is accepted (#4)")
+    # caller claiming square_terminal on a manual legacy payment is rejected
+    raised = False
+    try:
+        _mkref(db, ids, 500, provider="square_terminal", key="x")
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "legacy manual payment cannot be refunded as square_terminal (#4)")
+
+
+def test_legacy_square_payment_refunded_as_manual_rejected():
+    db, ids = _db()
+    inst = PaymentInstrument(code="card_terminal", name="Card (terminal)",
+                             instrument_type="card", provider="square_terminal")
+    db.add(inst); db.flush()
+    pay = Payment(order_id=ids["order_id"], instrument_id=inst.id,
+                  staff_id=ids["staff_id"], total_cents=5000)
+    db.add(pay); db.commit()
+    raised = False
+    try:
+        ra.create_refund_attempt(db, payment_id=pay.id, staff_id=ids["staff_id"],
+                                 provider="manual", amount_cents=500)
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "legacy square payment cannot be refunded as manual (#4)")
+
+
+def test_legacy_provider_must_be_derivable():
+    """A legacy payment whose provider cannot be derived (blank or unregistered
+    instrument provider) fails closed — caller input never becomes authoritative (#5)."""
+    db, ids = _db()
+    # blank instrument provider
+    blank = PaymentInstrument(code="blank_inst", name="Blank", instrument_type="card", provider="")
+    db.add(blank); db.flush()
+    pay1 = Payment(order_id=ids["order_id"], instrument_id=blank.id, staff_id=ids["staff_id"], total_cents=1000)
+    db.add(pay1); db.commit()
+    raised = False
+    try:
+        ra.create_refund_attempt(db, payment_id=pay1.id, staff_id=ids["staff_id"],
+                                 provider="manual", amount_cents=100)
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "legacy payment with blank instrument provider is rejected (#5)")
+
+    # unregistered instrument provider
+    bogus = PaymentInstrument(code="bogus_inst", name="Bogus", instrument_type="card", provider="ghost_pay")
+    db.add(bogus); db.flush()
+    pay2 = Payment(order_id=ids["order_id"], instrument_id=bogus.id, staff_id=ids["staff_id"], total_cents=1000)
+    db.add(pay2); db.commit()
+    raised = False
+    try:
+        ra.create_refund_attempt(db, payment_id=pay2.id, staff_id=ids["staff_id"],
+                                 provider="ghost_pay", amount_cents=100)
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "legacy payment with unregistered instrument provider is rejected (#5)")
+
+
+def test_refund_currency_must_match():
+    db, ids = _db()
+    _settled_attempt(db, ids)  # CAD square_terminal charge
+    ok = _mkref(db, ids, 500, provider="square_terminal", currency="CAD")
+    check(ok.currency == "CAD", "CAD charge -> CAD refund accepted (#5)")
+    raised = False
+    try:
+        _mkref(db, ids, 500, provider="square_terminal", currency="USD", key="u")
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "CAD charge -> USD refund rejected (#5)")
+    # legacy payment: refund currency must equal the venue currency (CAD)
+    db2, ids2 = _db()
+    raised = False
+    try:
+        _mkref(db2, ids2, 500, provider="manual", currency="USD")
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "legacy refund in a non-venue currency rejected (#5)")
+
+
+def test_provider_mismatch_rejected():
+    db, ids = _db()
+    _settled_attempt(db, ids, provider="square_terminal")
+    raised = False
+    try:
+        _mkref(db, ids, 500, provider="manual")  # settled attempt is square_terminal
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "refund provider must match the charge attempt's provider (#7)")
+
+
+def test_wrong_charge_attempt_rejected():
+    db, ids = _db()
+    _settled_attempt(db, ids)
+    # A charge attempt that does NOT back this payment (still pending, no payment_id).
+    other = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
+                              staff_id=ids["staff_id"], expected_total_cents=500)
+    raised = False
+    try:
+        ra.create_refund_attempt(db, payment_id=ids["payment_id"], staff_id=ids["staff_id"],
+                                 provider="square_terminal", amount_cents=500,
+                                 charge_attempt_id=other.id)
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "a charge attempt not backing this payment is rejected (#7)")
+
+
+def test_refund_currency_defaults_to_venue():
+    db, ids = _db()
+    old = os.environ.get("VENUE_CURRENCY")
+    os.environ["VENUE_CURRENCY"] = "USD"
+    try:
+        r = ra.create_refund_attempt(db, payment_id=ids["payment_id"], staff_id=ids["staff_id"],
+                                     provider="manual", amount_cents=500)  # currency omitted
+        check(r.currency == "USD", "omitted refund currency defaults to venue, not CAD (#3)")
+    finally:
+        os.environ.pop("VENUE_CURRENCY", None) if old is None else os.environ.__setitem__("VENUE_CURRENCY", old)
+
+
+def test_refund_reconciliation_authority():
+    db, ids = _db()
+    r = _mkref(db, ids, 500, provider="manual")
+    ra.transition_refund(db, r, R.REQUIRES_RECONCILIATION, last_error="lost")
+
+    # bare transition out of reconciliation is blocked
+    for target in (R.COMPLETED, R.FAILED):
+        raised = False
+        try:
+            ra.transition_refund(db, r, target)
+        except pa.PaymentAttemptError:
+            raised = True
+        check(raised, f"bare refund reconciliation -> {target} rejected (#1)")
+
+    # unauthorized actor cannot resolve
+    waiter = Staff(name="Wanda", role="waiter", pin_code="x")
+    db.add(waiter); db.commit()
+    raised = False
+    try:
+        ra.resolve_refund_reconciliation(db, r, resolved_status=R.COMPLETED, note="ok", actor=waiter)
+    except pa.ReconciliationAuthorityError:
+        raised = True
+    check(raised, "a non-manager cannot resolve refund reconciliation (#1)")
+
+    # automatic without evidence rejected
+    raised = False
+    try:
+        ra.resolve_refund_reconciliation(db, r, resolved_status=R.COMPLETED, note="x", automatic=True)
+    except pa.ReconciliationAuthorityError:
+        raised = True
+    check(raised, "automatic refund reconciliation needs provider evidence (#1)")
+
+    # authorized owner resolves + audit
+    owner = db.get(Staff, ids["staff_id"])  # seeded owner
+    ra.resolve_refund_reconciliation(db, r, resolved_status=R.COMPLETED, note="verified in dashboard",
+                                     actor=owner, provider_evidence="rf_123")
+    check(r.status == R.COMPLETED and r.reconciled_by == owner.name,
+          "authorized manager resolves refund + records who (#1)")
+    check(db.query(AuditEvent).filter_by(action="reconcile_refund_attempt").count() == 1,
+          "an audit event is written for the refund resolution (#1)")
+
+    # automatic WITH evidence accepted (fresh refund)
+    r2 = _mkref(db, ids, 200, provider="manual", key="r2")
+    ra.transition_refund(db, r2, R.REQUIRES_RECONCILIATION)
+    ra.resolve_refund_reconciliation(db, r2, resolved_status=R.FAILED, note="processor lookup",
+                                     automatic=True, provider_evidence="lookup:not_found")
+    check(r2.status == R.FAILED and r2.reconciled_by == "system:auto",
+          "automatic refund reconciliation with evidence is accepted (#1)")
+
+
+def test_external_refund_requires_refund_id():
+    db, ids = _db()
+    _settled_attempt(db, ids)  # square_terminal settled charge
+    r = _mkref(db, ids, 500, provider="square_terminal", key="er1")
+    raised = False
+    try:
+        ra.transition_refund(db, r, R.PROCESSOR_PENDING)  # no id
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "external refund -> PENDING without provider_refund_id rejected (#1)")
+    ra.transition_refund(db, r, R.PROCESSOR_PENDING, provider_refund_id="rf_1")
+    check(r.status == R.PROCESSOR_PENDING, "external refund -> PENDING with id accepted (#1)")
+    ra.transition_refund(db, r, R.COMPLETED)  # persisted id carries forward
+    check(r.status == R.COMPLETED, "external refund PENDING(persisted id) -> COMPLETED accepted (#1)")
+
+    r2 = _mkref(db, ids, 200, provider="square_terminal", key="er2")
+    raised = False
+    try:
+        ra.transition_refund(db, r2, R.COMPLETED)  # no id
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "external refund CREATED -> COMPLETED without id rejected (#1)")
+
+
+def test_manual_refund_completes_without_id():
+    db, ids = _db()  # seeded manual payment, no attempt
+    r = _mkref(db, ids, 100, provider="manual")
+    ra.transition_refund(db, r, R.COMPLETED)  # no provider_refund_id needed
+    check(r.status == R.COMPLETED, "manual refund completes without a processor id (#1)")
+
+
+def test_external_refund_reconciliation_requires_id():
+    db, ids = _db()
+    _settled_attempt(db, ids)
+    r = _mkref(db, ids, 500, provider="square_terminal", key="er")
+    ra.transition_refund(db, r, R.REQUIRES_RECONCILIATION)
+    raised = False
+    try:
+        ra.resolve_refund_reconciliation(db, r, resolved_status=R.COMPLETED, note="x",
+                                         automatic=True, provider_evidence="ev")  # no id
+    except pa.PaymentAttemptError:
+        raised = True
+    check(raised, "external reconciliation -> COMPLETED without refund id rejected (#1)")
+    ra.resolve_refund_reconciliation(db, r, resolved_status=R.COMPLETED, note="x",
+                                     automatic=True, provider_evidence="ev", provider_refund_id="rf_done")
+    check(r.status == R.COMPLETED, "external reconciliation -> COMPLETED with refund id accepted (#1)")
+
+
+def test_refund_provider_id_unique_and_write_once():
+    db, ids = _db()
+    _settled_attempt(db, ids)
+    a = _mkref(db, ids, 100, provider="square_terminal", key="u1")
+    b = _mkref(db, ids, 100, provider="square_terminal", key="u2")
+    ra.transition_refund(db, a, R.PROCESSOR_PENDING, provider_refund_id="RF")
+    raised = False
+    try:
+        ra.transition_refund(db, a, R.COMPLETED, provider_refund_id="RF2")  # change id
+    except pa.TransitionConflict:
+        raised = True
+    check(raised, "provider_refund_id is write-once (#1)")
+    raised = False
+    try:
+        ra.transition_refund(db, b, R.PROCESSOR_PENDING, provider_refund_id="RF")  # duplicate
+    except pa.TransitionConflict:
+        raised = True
+    check(raised, "duplicate (provider, provider_refund_id) rejected (#1)")
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
+        test_same_key_different_amount_conflicts,
+        test_legacy_refund_provider_derivation,
+        test_legacy_square_payment_refunded_as_manual_rejected,
+        test_legacy_provider_must_be_derivable,
+        test_refund_reconciliation_authority,
+        test_external_refund_requires_refund_id,
+        test_manual_refund_completes_without_id,
+        test_external_refund_reconciliation_requires_id,
+        test_refund_provider_id_unique_and_write_once,
+        test_refund_currency_must_match,
+        test_refund_currency_defaults_to_venue,
+        test_provider_mismatch_rejected,
+        test_wrong_charge_attempt_rejected,
+        test_refund_state_transitions,
+    ):
+        print(f"- {fn.__name__}")
+        fn()
+    if _failures:
+        print(f"\n{len(_failures)} FAILED")
+        sys.exit(1)
+    print("\nall refund-attempt tests passed")
```
