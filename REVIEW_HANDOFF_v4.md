# Review Handoff v4 — Stage 2c, Slice 2a (revised per v3 deep review)

Addresses every P0/P1/P2 item from `CLAUDE_STAGE2C_SLICE2A_V3_DEEP_REVIEW_FEEDBACK.md`.
Scope is unchanged: **only** the manual/cash `take_seat_payment` route. Square terminal
and refunds remain out of scope (slices 2b / 3 / 4).

> Note on the requested `git diff a0c0cf9…`: this working copy is **not a git repo**
> (`git rev-parse` fails; the audit bundle was distributed as a zip). I cannot produce a
> commit-range diff. Section 6 lists the exact per-file changes instead.

---

## 1. P0 — Local Payment validated against the durable attempt snapshot

**The exact `Payment.total` formula this app uses** (`payments.pay_seat`), for a manual charge:

```
items_cents      = Σ outstanding_cents of the covered (served) lines   [pre-tip, pre-discount base]
discount_cents   = min(requested_discount, items_cents)                [manager-approved]
tax_cents        = _taxes(items_cents - discount_cents)                [venue GST/PST config]
service_charge   = passed in by the route (rate × net)
card_surcharge   = card tenders only, on (items - discount + tax + service)
total_cents      = items - discount + tax + tip + service_charge + card_surcharge
```

The durable `PaymentAttempt` captures the intent components: `expected_total_cents`
(= `subtotal_cents` = the pre-tip/pre-discount `items` base), `discount_cents`,
`service_charge_cents`, `tip_cents`, and `line_selection`.

**New invariant — `settlement._local_snapshot_reason(attempt, payment)`** (runs for every
**local/non-external** provider; external providers keep reconciling against processor
evidence via `_mismatch_reason`). Before `PaymentAttempt → SETTLED`, the Payment the
factory booked **from freshly order-row-locked state** must reproduce the attempt exactly:

- `payment.items_cents == attempt.expected_total_cents`  (payable base)
- `payment.discount_cents == attempt.discount_cents`
- `payment.service_charge_cents == attempt.service_charge_cents`
- `payment.tip_cents == attempt.tip_cents`
- `payment.total_cents ==` the components re-summed by the formula above (pins tax + surcharge,
  which are deterministic functions of the captured components + venue config)
- `canonical(payment.allocations.order_item_id) == attempt.line_selection`  (paid-item set)

On **any** drift, settlement raises `SettlementDrift` **before** the SETTLED transition.
No Payment is committed; the caller rolls back; the attempt stays retryable (`CREATED`).
This is a deterministic **reject**, not a `REQUIRES_RECONCILIATION` park — for cash there is
no processor to reconcile against, and no money has moved, so a clean retry is safer than a
stuck row. `settle_charge` still never uses an exception to *drive a commit* (mismatch
reconciliation for external providers is still a returned `SettlementResult`); `SettlementDrift`
is the one case that means "abort — roll back," which is exactly the intended control flow.

Route mapping: `take_seat_payment` catches `SettlementDrift` → **HTTP 409** ("the bill changed
while you were paying; re-check and settle again"). `settle_manual_charge` catches it, rolls
back the uncommitted Payment + in-memory transitions, and re-raises.

**Failure-injection tests (`tests/test_charge.py`):**
- `test_wrong_total_factory_rejected` — factory books a Payment whose total (1234) disagrees
  with its components (base 1000) → `SettlementDrift`; **0 Payments, 0 allocations**, attempt left `CREATED`.
- `test_wrong_base_rejected` — attempt base (999) ≠ what pay_seat books from locked state
  (1000) → reject; no partial Payment/allocation survives.
- `test_wrong_tip_rejected` — a tip booked onto the Payment (500) the attempt did not capture
  (0) → reject.
- `test_toctou_item_paid_elsewhere_rejected` — the realistic drift: of two selected items, one
  is paid out-of-band first; the stale two-item attempt then settles nothing (booked base
  1000 ≠ attempt 2000) → **no second Payment on the already-paid item**.

---

## 2. P1 — Request idempotency token wired into the live route

**Lifecycle**

```
GET  /orders/{id}/pay   -> render issues one fresh token PER SEAT pay-form:
                           pay_tokens = { seat.id: secrets.token_urlsafe(16) }
                           (hidden <input name="pay_token"> inside each seat's form)
POST .../seats/{sid}/pay -> pay_token is used verbatim as PaymentAttempt.idempotency_key
                           create_attempt: UNIQUE(idempotency_key) + intent fingerprint
success                  -> PRG redirect (303) re-renders the screen with fresh tokens (rotation)
```

- **Per-seat, not per-page** — paying seat A then seat B on the same page load must not collide;
  each seat form carries its own token. Verified live: a two-payable-seat order renders **2
  distinct** `pay_token` values.
- **Server-generated, never derived from user-editable amount fields** (`secrets.token_urlsafe`).
- **Bound to the intent by the existing fingerprint**: reusing a token with a changed
  amount/selection fails `_assert_same_intent` → `IdempotencyConflict`.
- The route still accepts an empty `pay_token` (defaults to `idempotency_key=None`) so direct
  API callers / the existing e2e harness keep working; the browser form always sends one.

**Tests (`tests/test_charge.py` unless noted):**
- `test_same_token_same_intent_converges` — double-submit: same token → same attempt, same
  Payment, **no duplicate allocation**.
- `test_response_loss_retry_in_fresh_session_converges` — lost-response retry on a brand-new
  session still converges on the one settled Payment.
- `test_same_token_changed_amount_conflicts` — same token, changed amount → `IdempotencyConflict`
  (cannot silently settle a different amount).
- `test_drift_retry_reuses_same_durable_attempt` — after a drift, repeated same-token retries
  reuse the **one** durable attempt (no stray-attempt proliferation); 0 Payments booked.
- **Postgres, threaded** `test_concurrent_duplicate_token_one_payment` (`tests/test_pg_concurrency.py`)
  — 8 concurrent POSTs with the **same token**: exactly **one Payment**, **one attempt row**,
  **one allocation**; losers converge or refuse with a typed error, never an unhandled error.

---

## 3. P1 — Locked snapshot / selection revalidation (TOCTOU)

The route computes `selected` / `base` / tip / discount / service-charge **before** the attempt
exists (unlocked). `pay_seat` then takes the `SELECT … FOR UPDATE` order-row lock and recomputes
the payable + booked selection from committed state. The **local-snapshot invariant (§1)** is what
closes the TOCTOU: because the booked Payment is compared back to the attempt, any drift between
attempt-creation and the locked booking — an item paid, a price/discount/service change, a
selection move — fails the comparison and **rejects** rather than settling the new amount under
the old intent.

Design note (see §7): the comparison runs on the Payment `pay_seat` built from locked state,
i.e. *after* the in-memory Payment object exists but **before** it is committed or the attempt is
SETTLED. Since a drift rolls the uncommitted Payment back, this is equivalent to "no Payment is
created," and it also lets us diff the actually-booked allocations against `line_selection`.
Covered by `test_wrong_base_rejected` and `test_toctou_item_paid_elsewhere_rejected`.

---

## 4. P2 — E2E closeout baseline evidence

The two failing `test_e2e` end-of-day-close checks are **pre-existing and independent of Stage 2c**.
Reproduced baseline (fresh reseed each run, live server):

- **Wiring ON** (this slice): close section → `there is money in the window to close → $134.15`
  (PASS), then `counted == float + expected gives zero variance → -2816` (FAIL) and `Z-report froze
  the window's collected total` (FAIL).
- **Wiring REVERTED** to the old direct `pay_seat` call (route bypassing `settle_manual_charge`),
  fresh reseed, same run: the **identical** two failures (`-2816`, deterministic) persist.

Root cause is the test harness, not the payment code: `test_e2e` holds one long-lived
`db = SessionLocal()` and calls `closeout.compute_pending(db)` on that stale session
(test_e2e.py:1234), then POSTs the close, which recomputes on the server's **fresh** session; the
~$28.16 delta is payments the stale session hadn't expired. The seat-settlement (4.2.4) and
partial-close (4.2.5) sections — the code this slice actually rewires — **pass**. I recommend
fixing the harness (an `db.expire_all()` before the pending read) as a separate, non-payment
change before the consolidated Stage 2c approval; it is not fixed here to keep the slice scoped.

---

## 5. Test counts (this session)

| Suite | Tests | Assertions | Backends run |
|-------|------:|-----------:|--------------|
| `test_charge.py` | 11 | 31 | SQLite-FK + Postgres 16 |
| `test_settlement.py` | 9 | 18 | SQLite-FK + Postgres 16 |
| `test_pg_concurrency.py` | 10 | 21 | Postgres 16 |
| `test_pg_migration.py` | — | (all pass) | Postgres 16 |
| `test_payment_attempts.py`, `test_refund_attempts.py`, `test_payment_providers.py` | — | (all pass) | SQLite-FK + Postgres |

Regression sweep green on SQLite: `test_money`, `test_admin`, `test_config`, `test_schedule`,
`test_security`, `test_templates`, `test_reconciliation`. `test_e2e`: settlement/partial-close
sections pass; two closeout checks fail as documented in §4 (pre-existing).

---

## 6. Exact changes (no git repo — per-file)

- **`app/services/settlement.py`** — new `SettlementDrift(PaymentAttemptError)`; new
  `_local_snapshot_reason(attempt, payment)`; `settle_charge` raises `SettlementDrift` after the
  factory flush and before the SETTLED transition; module docstring updated.
- **`app/services/charge.py`** — `settle_manual_charge` wraps `settle_charge` in
  `try/except SettlementDrift: db.rollback(); raise` (durable attempt survives as `CREATED`).
- **`app/routers/pay.py`** — `import secrets`; `import SettlementDrift`; `payment_screen` render
  adds `pay_tokens = {seat.id: token_urlsafe(16)}`; `take_seat_payment` gains `pay_token` form
  field, passes it as `idempotency_key`, and maps `SettlementDrift → HTTP 409`.
- **`web/templates/pay.html`** — hidden `pay_token` input inside each seat pay-form.
- **`tests/_pay_fixture.py`** — `seed_charge(n_items=…)` + `ChargeScenario.item_ids`.
- **`tests/test_charge.py`** — expanded to 11 tests (drift injection, TOCTOU, idempotency token).
- **`tests/test_pg_concurrency.py`** — new threaded `test_concurrent_duplicate_token_one_payment`.
- **`tests/test_settlement.py`** — synthetic `_factory` now books a snapshot-consistent Payment.

---

## 7. Areas of least confidence

1. **Where the snapshot check runs.** It validates the Payment `pay_seat` already built (in
   memory, pre-commit) rather than literally "before creating the Payment." I argue these are
   equivalent because a drift rolls the uncommitted Payment back, and running post-build is what
   lets me diff the booked allocations against `line_selection`. If you want the check strictly
   before any Payment INSERT, it would mean passing the attempt into `pay_seat` (coupling the
   provider-agnostic core to attempts) or re-deriving the ledger a second time under the lock.
2. **`_local_snapshot_reason` is scoped to local providers.** External/terminal (slice 2b) still
   only checks processor evidence. The terminal path will need its **own** decision for the
   terminal-reported tip (which legitimately differs from the pre-tip attempt snapshot) — that is
   a 2b design item, deliberately not pre-committed here.
3. **Concurrent duplicate-token proof is threaded** (real contention, `threading.Barrier`), so it
   asserts an outcome invariant (one Payment / one attempt / one allocation) rather than a fixed
   interleaving. It passed on Postgres 16; like all threaded tests it is not a formal proof of
   every schedule.
4. **e2e** still shows two red closeout checks by design (§4); I did not touch the harness.

**Stopped here for review. I will not start Slice 2b (Square terminal) until you approve.**
