# Remediation Plan — Restaurant App

Derived from the consolidated external audit (39 findings) and **verified against
the current code** before scheduling. Executed on branch
`fix/p0-security-and-payments`. Each stage is committed separately and stops for
review. Financial correctness, payment recovery, concurrency safety, and
production security are prioritized over features.

**Correction carried in:** PINs are already PBKDF2-HMAC-SHA256 (200k rounds,
salted) with legacy migration — this is a strength to preserve, NOT a bug. Any
doc that says "plaintext PINs" is wrong.

## Staging (cost-aware order)

| Stage | Findings | Risk of change | Cost |
|-------|----------|----------------|------|
| **1 — Deploy/config fail-closed** | #16, #17, #18, #19, #20 | Low (no money paths) | small |
| **2 — Payment durability core** | #1, #2, #3, #4, #5, #6 | High (money) | large |
| **3 — Financial correctness** | #7, #8, #9, #10, #11, #22, #27 | Medium | medium |
| **4 — Concurrency & business day** | #12, #13, #14, #15, #36 | Medium | medium |
| **5 — Security hardening** | #29, #30, #31, #32 | Low/Med | small-med |
| **6 — Validation & state machines** | #23, #24, #25, #26, #28, #34, #35 | Low/Med | medium |
| **7 — Reliability/CI/tests** | #21, #33, #37, #38, #39 | Low | medium |
| **P4 — Delivery adapters** | #34(plan) | — | deferred |

Every fix ships with a regression test proving the prior failure mode, per the
audit's acceptance criteria. Delivery-platform integrations are frozen until
Stage 2–4 pass under failure injection.

---

## Stage 1 — Deploy/config fail-closed (in progress)

- **#17** `SECRET_KEY`: stop returning the public `_DEV_SECRET` in production.
  Add `APP_ENV` (default `development`). In production, a missing `SECRET_KEY`
  raises at startup — fail closed.
- **#16** `docker-compose.yml`: explicitly pass `SECRET_KEY` (required, `:?`),
  `APP_ENV`, `COOKIE_SECURE`, `TZ`, and all `SQUARE_*` into the app container.
- **#18** `docker-entrypoint.sh`: bootstrap failure is fatal — do not `|| echo …`
  and continue.
- **#19** `migrate.py`: in production run `strict=True`; a real ALTER failure
  raises instead of being appended as `SKIPPED …` and swallowed.
- **#20** split liveness/readiness: keep `/healthz` (liveness); add `/readyz`
  that verifies DB connectivity + a core table exists.
- New `app/config.py` centralizes `app_env()`, `is_production()`, and
  `validate_startup_config()` (called from `main.py`).

## Stage 2 — Payment durability core (next, needs your go-ahead)
Durable immutable `PaymentAttempt` (order/seat/amounts/currency/idempotency +
provider checkout/payment/refund IDs + status state machine), created and
committed **before** contacting Square; lock+snapshot payable state before the
attempt; idempotent settlement; recovery/reconciliation for processor-success +
local-failure; real Square **Refunds API** for refund/void; refund locking to
prevent over-refund. This is the real-money blocker set.

## Stages 3–7
As tabled above; detail expanded when each stage begins.
