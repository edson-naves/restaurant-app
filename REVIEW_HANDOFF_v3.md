# Review Handoff v3 — Stage 2c, Slice 2a: live manual/cash charge wired through the durable attempt lifecycle

**Scope of this slice (deliberately narrow).** This wires **only the manual/cash
charge route** (`take_seat_payment`) through the durable `PaymentAttempt` lifecycle +
settlement service that v0–v2 built and you approved. The Square **terminal** path,
refund settlement, and the refund route are **out of scope** and untouched here — they
are the next slices (2b, 3, 4). Please review this slice in isolation and gate 2b on it.

---

## 1. What changed

| File | Change |
|------|--------|
| `app/services/charge.py` (new) | `settle_manual_charge()` — orchestrates a manual charge: create durable attempt → instant-approve → settle into exactly one Payment via the no-commit core. |
| `app/routers/pay.py` | `take_seat_payment` now calls `settle_manual_charge` (pay.py:388) instead of calling `pay_seat` directly. `pay_seat` is wrapped in a zero-arg `_factory()` closure (pay.py:371) so the settlement service owns the Payment creation. |
| `tests/_pay_fixture.py` | `seed_charge()` + `ChargeScenario` — a served-item order scenario for charge tests. |
| `tests/test_charge.py` (new) | 4 tests: atomic settle, idempotent retry, atomic rollback + durable attempt, concurrent no-double (PG). |

The route’s pre-charge logic is unchanged: it still computes the served-item
`selected` set, `base` (sum of outstanding on selected), tip/discount/service-charge,
and the discount approver **before** any attempt is created.

---

## 2. How each Slice-2 guardrail is met

- **#1 — `pay_seat` is the no-commit core.** Unchanged and re-confirmed: `pay_seat`
  (payments.py:334) locks the order row via `_lock_order` (payments.py:318,
  `SELECT Order.id … FOR UPDATE`), re-validates the payable from current state, mutates
  Payment + allocations + seat with `db.flush()` only — **never commit/rollback**. The
  route no longer commits inside the factory; the single outer commit lives in
  `settle_manual_charge`.
- **#3 — attempt committed before settlement.** `create_attempt` commits the durable
  intent first (charge.py:51). The Payment/allocations/seat mutations happen afterward
  and commit in one outer transaction. A crash between the two leaves a durable
  `CREATED` attempt with no Payment — proven by `test_settlement_is_atomic_and_attempt_durable`.
- **#6 — one atomic local transaction.** Attempt→SETTLED + Payment + allocations + seat
  state all commit together (charge.py:64–66). Injected rollback before the outer commit
  reverts *all* of it while the separately-committed attempt survives as `CREATED`.
- **#14 — manual carries no external processor id.** The manual attempt is instant-approved
  with **no** `provider_payment_id`/amount/currency evidence; `transition`’s external-evidence
  gate only fires for external providers. Settlement therefore runs no amount/currency
  mismatch check for manual (there is nothing to reconcile against). Proven by the manual
  path settling with `attempt.provider == "manual"` and no external id.
- **CAS-loss ≠ success.** Inherited unchanged from settlement.py (v1/v2): a CAS loser only
  converges if the winner is provably `SETTLED` with a `payment_id` and a real Payment row;
  otherwise it re-raises. Covered by the existing PG concurrency suite (still green here).

---

## 3. Test evidence

All suites run on **SQLite-with-FK** and, where noted, on a **real disposable Postgres 16**
(`postgres:16-alpine`, `PG_TEST_DSN` set). Commands and tails below are from this session.

**New — `tests/test_charge.py` (SQLite + PG):**
- `manual charge settles` / `attempt SETTLED (manual, no external id)` / `attempt links its one Payment`
- `exactly one Payment` / `one allocation for the paid item` / `seat marked paid`
- `retry returns the same attempt + Payment` / `retry creates no duplicate Payment`
- `attempt is durable as CREATED after the rollback (#3)` / `no Payment persisted (atomic rollback #6)` / `no allocation persisted` / `seat not marked paid`
- **PG only:** `second concurrent charge is refused (no double Payment)` / `exactly one Payment across both`

**Regression — all green on PG:** `test_settlement`, `test_payment_attempts`,
`test_refund_attempts`, `test_pg_migration`, `test_pg_concurrency`. Green on SQLite:
`test_payment_providers`, `test_reconciliation`, `test_money`, `test_admin`, `test_config`,
`test_schedule`, `test_security`, `test_templates`.

**End-to-end (live HTTP server, real app) — `test_e2e.py`:** the seat-level settlement
(4.2.4) and partial-close (4.2.5) sections — i.e. the exact route this slice rewires —
**pass** with the wiring on (e.g. *“4.2.5: only the ticked item was charged → \$34.50”*,
*“tip is proportional → \$5.18”*, *“seat shows Paid (partial)”*, receipt issued).

---

## 4. Honest caveats (please weigh these)

1. **Two pre-existing `test_e2e` end-of-day-close checks FAIL, and it is *not* this
   change.** On a freshly-seeded DB the close section reports
   `counted == float + expected gives zero variance → -2816` and `Z-report froze the
   window's collected total` FAIL. **I isolated it:** I reverted the route to the old
   direct `pay_seat` call, reseeded, and re-ran — the **identical** two failures persist
   (`-2816`, deterministic). Root cause is in the test harness, not the payment code: the
   test holds one long-lived `db = SessionLocal()` session and calls
   `closeout.compute_pending(db)` (test_e2e.py:1234) on that stale session, then POSTs the
   close, which recomputes on the server’s **fresh** session; the ~\$28.16 delta is
   payments the stale session hadn’t expired. It is out of scope for slice 2a; flagging it
   so it isn’t mistaken for a regression. Happy to fix the test in a separate change if you want.

2. **No request-level idempotency key is wired from the form yet.** `settle_manual_charge`
   fully supports `idempotency_key` (proven by `test_manual_charge_is_idempotent`), but the
   HTML form POST has no natural client token, so the route currently passes
   `idempotency_key=None` — each POST is a fresh attempt. This gives crash-safety and a
   durable audit trail, but **not** double-submit protection. Wiring a client-supplied token
   (hidden form field / PRG token) is a deliberate follow-up decision, not smuggled in here.

3. **Manual amount is not cross-checked.** By design (#14): a manual/cash charge has no
   processor to reconcile against, so the attempt’s `expected_total_cents` is the item
   subtotal and the Payment total (incl. tip/service) is authoritative. This matches the
   approved settlement contract; noting it so the asymmetry vs. external providers is explicit.

---

## 5. Explicitly NOT done in this slice

- Square **terminal** charge path (`start_terminal_payment` / `terminal_status`) — still on
  the pre-2c direct `pay_seat` path. **Slice 2b.**
- Refund settlement service wiring and the refund route. **Slices 3 / 4.**
- Any change to `compute_pending` / `record_close` or the e2e harness.

**I have STOPPED here for your review and will not proceed to slice 2b until you approve.**
