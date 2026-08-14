# Review Handoff v5 — Stage 2c, Slice 2a (revised per v4 deep review)

Addresses every P0/P1/P2 item from `CLAUDE_STAGE2C_SLICE2A_V4_DEEP_REVIEW_FEEDBACK.md`.
Scope unchanged: **only** the manual/cash `take_seat_payment` route. Square terminal and
refunds remain out of scope (slices 2b / 3 / 4).

> Still not a git repo — no `git diff` available. Section 7 lists exact per-file changes.

---

## 1. Provider-neutral meaning of every PaymentAttempt monetary field (P0 #1)

One definition, identical for manual and Square. The attempt stores the **component
snapshot**, and `expected_total_cents` is the **pre-tip** total:

| Field | Meaning |
|-------|---------|
| `subtotal_cents` | raw selected-item subtotal (pre-discount, pre-tip) |
| `discount_cents` | discount snapshot (clamped to subtotal) |
| `tax_cents` | tax snapshot (venue GST/PST at creation) |
| `service_charge_cents` | service-charge snapshot |
| `surcharge_cents` | card-surcharge snapshot |
| `tip_cents` | tip snapshot |
| **`expected_total_cents`** | **`subtotal − discount + tax + service_charge + surcharge`** (PRE-TIP; what a processor authorizes) |

Derived identities (asserted at settlement):

```
Payment pre-tip total == attempt.expected_total_cents
Payment final total   == attempt.expected_total_cents + attempt.tip_cents
Square (unchanged):  processor_amount_cents == attempt.expected_total_cents
```

**Worked manual example** (base 1000, 10% service, 5% GST, 0% PST, 200 tip, no discount):

```
subtotal        = 1000
discount        =    0
tax             =   50   (5% of 1000)
service_charge  =  100
surcharge       =    0   (cash)
expected_total  = 1000 - 0 + 50 + 100 + 0 = 1150   (PRE-TIP)
tip             =  200
Payment.total   = 1150 + 200 = 1350
```

This is exactly `test_expected_total_is_pre_tip_with_all_components` — it asserts
`expected_total_cents == 1150` (not the 1000 subtotal) and `Payment.total == expected + tip`.

**Single source of truth.** `payments.compute_breakdown()` (a pure `ChargeBreakdown`) now
computes the whole breakdown; `pay_seat` books from it, and `settle_manual_charge` snapshots
the attempt from it. Intent and booked Payment are therefore computed by identical arithmetic —
any divergence is a real drift, never a formula skew. The regression where manual set
`expected_total_cents = subtotal` is gone.

The idempotency fingerprint already hashes every component (`intent_fingerprint` includes
subtotal/tax/tip/service/discount/surcharge/expected_total + selection), so changing **any**
component changes the fingerprint → `IdempotencyConflict` on token reuse (acceptance #4).

---

## 2. Direct attempt-vs-Payment tax & surcharge checks (P0 #2)

`settlement._local_snapshot_reason` (runs for every **local** provider; external keeps its
processor-evidence check) now compares **all six components directly against the committed
attempt snapshot** — the snapshot, not live venue config, is authoritative:

```
payment.items_cents          == attempt.subtotal_cents
payment.discount_cents       == attempt.discount_cents
payment.tax_cents            == attempt.tax_cents          # NEW
payment.service_charge_cents == attempt.service_charge_cents
payment.card_surcharge_cents == attempt.surcharge_cents    # NEW
payment.tip_cents            == attempt.tip_cents
```

then the arithmetic-consistency second invariant:

```
(items − discount + tax + service + surcharge) == attempt.expected_total_cents   # pre-tip
payment.total_cents == attempt.expected_total_cents + attempt.tip_cents           # final
canonical(payment.allocations) == attempt.line_selection                          # selection
```

Any mismatch raises `SettlementDrift` **before** the SETTLED transition; the caller rolls the
uncommitted Payment back and the attempt stays retryable. No money moves; nothing is parked.

**Failure-injection tests (`tests/test_charge.py`):**
- `test_tax_config_drift_rejected` — snapshot at 5% GST, then venue tax → 0% before the locked
  settle: booked tax differs → `SettlementDrift`; 0 Payments/allocations; attempt not settled.
- `test_surcharge_drift_rejected` — snapshot a card charge at 10% surcharge, then settle with the
  surcharge rate dropped to 0 → booked surcharge differs → `SettlementDrift`; no Payment.
- Plus the retained `test_wrong_total_factory_rejected`, `test_wrong_base_rejected`,
  `test_wrong_tip_rejected`, `test_toctou_item_paid_elsewhere_rejected`.

---

## 3. Non-empty token required on the live route (P1 #3)

`take_seat_payment` now **rejects a missing/blank/oversized token before any work**:

```python
pay_token = (pay_token or "").strip()
if not pay_token or len(pay_token) > 64:      # idempotency_key is VARCHAR(64)
    raise HTTPException(422, "A valid payment idempotency token is required.")
```

The blank-token → `idempotency_key=None` bypass is gone. The pay screen always renders a fresh
per-seat token (§ token lifecycle in v4 handoff, unchanged). The **e2e harness now supplies a
token** on every seat-pay POST (a `_tok()` helper; 8 call sites updated).

Token behaviour tests (unchanged from v4, still green): same-token convergence, fresh-session
response-loss retry, same-token-changed-amount → `IdempotencyConflict`, drift-retry reuses the
one durable attempt, and the Postgres threaded 8-way duplicate-token → one Payment.

---

## 4. Manual tender / drift operational contract (P2 #4)

Documented in `charge.py` and here: **cash/manual tender is not considered accepted until the
local settlement commits.** On drift the attempt rolls back to a retryable state and no Payment
exists, which is correct precisely because no money is captured at the durable-attempt stage for
a manual charge (there is no external authorization holding funds). The operator simply re-reads
the bill and settles again. This is the reason a manual drift is a deterministic **reject**
(retry) rather than a `REQUIRES_RECONCILIATION` park (which is reserved for external providers,
where a processor may already hold funds and a human/worker must reconcile).

---

## 5. E2E closeout baseline (P2 #5)

Unchanged from v4 and still **pre-existing / independent of this slice**. Fresh-reseed run with
the token-required route: the seat-settlement (4.2.4) and partial-close (4.2.5) sections **pass**
end-to-end; the close section shows `money in the window → $64.21` (PASS) then the same two
deterministic failures `zero variance → -2816` and `froze the collected total` (FAIL). Reverting
the route to the old direct `pay_seat` call reproduced the identical pair in v3. Root cause is the
harness's stale long-lived `SessionLocal` read in `compute_pending` (test_e2e.py). Recommend
fixing the harness (`db.expire_all()` before the pending read) as a separate non-payment change
before consolidated Stage 2c approval; not done here to keep the slice scoped.

---

## 6. Test counts (this session)

| Suite | Tests | Assertions | Backends |
|-------|------:|-----------:|----------|
| `test_charge.py` | 14 | 42 | SQLite-FK + Postgres 16 |
| `test_settlement.py` | 9 | 18 | SQLite-FK + Postgres 16 |
| `test_pg_concurrency.py` | 10 | 21 | Postgres 16 |
| `test_pg_migration.py` | — | all pass | Postgres 16 |

Green on SQLite: `test_payment_attempts`, `test_refund_attempts`, `test_payment_providers`,
`test_reconciliation`, `test_money`, `test_security`, `test_templates`. `test_e2e`:
settlement/partial-close pass; the two closeout checks fail as in §5 (pre-existing).

---

## 7. Exact changes (no git repo — per-file)

- **`app/services/payments.py`** — new `ChargeBreakdown` dataclass + `compute_breakdown()`
  (single money-formula source; `expected_total_cents` = pre-tip, `total_cents` = +tip);
  `pay_seat` refactored to book from it (behaviour-preserving).
- **`app/services/charge.py`** — `settle_manual_charge` now takes `instrument_id` +
  `card_surcharge_rate`, computes the full breakdown, and snapshots **all** components (tax +
  surcharge included) with `expected_total_cents` = pre-tip; docstring documents the semantics
  and the tender/drift contract.
- **`app/services/settlement.py`** — `_local_snapshot_reason` compares all six components
  directly (tax + surcharge added) + the pre-tip/`+tip` identities + selection.
- **`app/routers/pay.py`** — require non-empty ≤64-char `pay_token` (422 otherwise); pass
  `instrument_id` + `card_surcharge_rate` to `settle_manual_charge`.
- **`tests/test_charge.py`** — 14 tests: added pre-tip-semantics, tax-drift, surcharge-drift;
  `_charge` passes `instrument_id`; `_snapshot_attempt`/`_expect_drift` helpers.
- **`tests/test_pg_concurrency.py`** — duplicate-token test passes `instrument_id`.
- **`tests/test_e2e.py`** — `_tok()` helper; `pay_token` added to all 8 seat-pay POSTs.
- **`tests/_pay_fixture.py`, `tests/test_settlement.py`** — unchanged from v4 (n_items scenario;
  snapshot-consistent synthetic factory).

---

## 8. Areas of least confidence

1. **`compute_breakdown` is the sole formula authority.** I refactored `pay_seat` to book from
   it rather than duplicate the math; the arithmetic is identical, but `pay_whole_order` and the
   card-terminal path still compute inline (out of scope). If you want one formula everywhere,
   that is a small follow-up.
2. **Snapshot check runs post-build, pre-commit** (unchanged rationale from v4): equivalent to
   "no Payment created" because a drift rolls the uncommitted Payment back, and it lets us diff
   booked allocations against `line_selection`.
3. **Terminal (2b) tip semantics.** `_local_snapshot_reason` is local-only; the terminal path
   will need its own decision for the terminal-reported tip (legitimately differs from the pre-tip
   snapshot). Deliberately not pre-committed here.
4. **e2e** still shows the two pre-existing closeout reds (§5); harness untouched.

**Stopped for review. I will not start Slice 2b (Square terminal) until you approve.**
