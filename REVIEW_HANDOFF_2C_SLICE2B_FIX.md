# Review Handoff — Stage 2c, Slice 2b FIX (per Slice-2b deep review)

Addresses every P0/P1/P2 item in `CLAUDE_STAGE2C_SLICE2B_DEEP_REVIEW_FEEDBACK.md`. Scope
unchanged: the Square-terminal route. Refund wiring (slices 3/4) not started.

> Repo moved to `Projetos/Restaurant/restaurant_app` (still not a git repo). Section 9 lists
> exact per-file changes.

---

## 1. P0 #1 — Short phase-1 order lock around the initial snapshot

`start_terminal_attempt` now runs the three phases the review specified:

```
PHASE 1  (locked snapshot)
  payments.locked_seat_payable(order, seat)   # SELECT order ... FOR UPDATE
  recompute selected items + base from locked state
  compute_breakdown  ->  create_attempt        # commits (releases the lock)
  db.commit()                                  # guarantees release on the idempotent path too
PHASE 2  (no lock held)
  provider.charge(idempotency_key = attempt.key)   # Square I/O
PHASE 3  (settlement, existing)
  pay_seat reacquires order FOR UPDATE, revalidates vs attempt, settles/reconciles
```

The authoritative payable is recomputed **under the lock** (a new `payments.locked_seat_payable`
helper), not from an unlocked route read. The route now passes the raw item filter + the
service-charge *rate*, not a pre-locked amount.

**Tests:**
- `test_phase1_lock_released_before_square` (**Postgres**) — a `FOR UPDATE NOWAIT` probe fired
  from *inside* the mocked `create_checkout` finds the order row **free**, proving the phase-1
  lock is released before any Square I/O.
- `test_phase1_snapshot_reflects_committed_state` — if the seat is paid just before start, the
  locked snapshot finds nothing to charge (prices from current committed state, not a stale read).

## 2. P0 #2 — Terminal tip is now durable, write-once processor evidence

New column **`payment_attempt.processor_tip_cents`** (nullable, idempotent ADD COLUMN migration).
The tip is persisted **write-once** with the `PROCESSOR_APPROVED` transition (same guarded CAS as
`provider_payment_id` / `processor_amount_cents`), and settlement books the Payment tip from the
**persisted** value — never from a transient/re-inferred one.

Crash-safety contract:
```
Square approves (tip 250)  ->  APPROVED evidence incl. tip commits  ->  [crash]
restart: recovery re-polls; settles from attempt.processor_tip_cents (250), never zero
```
Conflicting evidence fails closed: a reloaded APPROVED attempt whose fresh poll tip disagrees with
the recorded tip **reconciles** (does not overwrite); a reloaded APPROVED attempt with **no**
durable tip (legacy) reconciles rather than inferring zero.

**Tests:** `test_tip_is_durable_and_recovers_after_crash` (evidence committed pre-settle + on the
recovery queue + fresh-session recovery settles with 250, one Payment), `test_conflicting_tip_evidence_fails_closed`.

## 3. P0 #3 — External revalidation compares every pre-tip component

`settlement._external_booking_reason` now compares **subtotal, discount, tax, service charge,
surcharge** individually against the committed attempt (in addition to the pre-tip total and the
paid-item selection) — matching the provider-neutral Slice-2a contract. The **tip is the only
value allowed from processor evidence** after intent creation, so it alone is not compared here.
`_mismatch_reason` still gates on `processor_amount_cents == expected_total_cents` and
`processor_currency == attempt.currency` before booking.

**Test:** `test_component_drift_same_total_reconciles` — a booking whose components drift
(subtotal 1000→900, tax 50→150) while the pre-tip total stays 1050 now **reconciles** and writes
no Payment.

## 4. P1 #4 — Terminal lookup scoped to order + seat + provider

`_terminal_attempt(db, checkout_id, order_id, seat_id)` filters on `provider == "square_terminal"`
**and** `order_id` **and** `seat_id`; `terminal_status` and `terminal_cancel` both use it. A
checkout id can no longer resolve another order's or seat's attempt.

**Test:** `test_scoped_lookup_rejects_other_seat` (wrong seat / wrong order → None).

## 5. P1 #5 — Poll-vs-cancel race

`terminal_cancel` now `db.refresh`es to current truth and, if a concurrent poll already moved the
attempt past PENDING, the guarded transition loses the CAS and is caught — the cancel **never
overwrites the approval evidence**.

**Test:** `test_stale_cancel_cannot_overwrite_approval` — B holds a stale PENDING view while A
approves+settles; B's cancel is refused with a typed error and the SETTLED + provider-payment-id
evidence is intact.

## 6. P2 #6 — Bounded transport-ambiguity escalation

`advance_terminal_attempt` owns a deadline: a poll still PENDING (including transient transport
errors the provider maps to PENDING) past `TERMINAL_PENDING_DEADLINE_S` (360s, > Square's ~5-min
checkout deadline) transitions the attempt to **REQUIRES_RECONCILIATION** instead of polling
forever.

**Test:** `test_transport_pending_past_deadline_escalates`.

## 7. P2 #7 — e2e closeout harness: investigated, diagnosis corrected, STILL OPEN (non-blocking)

I could **not** fix this, and — importantly — I **disproved the stale-session diagnosis** we
were both carrying. Instrumented run (fresh reseed, single clean server):

```
test-side compute_pending :  total=6421   expected_cash=0     refund=0
server-side close (HTTP)   :  total=28220  expected_cash=2816  refund=500
```

Reading the window on a **brand-new `SessionLocal()`** in the test process reproduces the low
numbers, so it is **not** identity-map staleness and **not** an open-read-transaction snapshot
(I tried `expire_all()`, then `rollback()`, then a fresh session — none changed the result). The
test process reads the same `db/restaurant.db` the server writes but sees only a subset of the
committed payments — consistent with a cross-process SQLite/WAL visibility gap (or a DB-path
difference) between the separate test and server processes, not a payment-code bug. The real fix
is a harness redesign (have the e2e assert closeout **through the HTTP API** rather than a direct
in-process DB session). I reverted my non-working edits; the e2e is back at its baseline.

This remains **non-blocking for Slice 3** per the review, but I no longer claim it as a
stale-session artifact — flagging the corrected finding so it isn't mis-fixed.

## 8. Test counts & results

| Suite | Tests | Assertions | Backends |
|-------|------:|-----------:|----------|
| `test_terminal.py` | 17 | 50 | SQLite-FK + Postgres 16 |
| `test_charge.py` (manual regression) | 14 | 42 | SQLite-FK + Postgres 16 |
| `test_settlement.py` | 9 | 18 | SQLite-FK + Postgres 16 |
| `test_pg_concurrency.py` | 10 | 21 | Postgres 16 |

All green on both backends. `test_pg_migration`, `test_payment_attempts`, `test_refund_attempts`,
`test_payment_providers`, `test_reconciliation`, `test_money`, `test_security`, `test_templates`,
`test_admin`, `test_config`, `test_schedule` — green. `test_e2e`: settlement (4.2.4) and
partial-close (4.2.5) sections pass on a clean run; the **2 closeout checks** fail as in §7
(pre-existing, non-blocking).

## 9. Exact changes (no git repo — per-file)

- **`app/models/oltp.py`** — `PaymentAttempt.processor_tip_cents` (write-once tip evidence).
- **`app/migrate.py`** — idempotent `ADD COLUMN payment_attempt.processor_tip_cents INTEGER`.
- **`app/services/payment_attempts.py`** — `transition(processor_tip_cents=…)` write-once field.
- **`app/services/payments.py`** — `locked_seat_payable()` (phase-1 locked snapshot helper).
- **`app/services/charge.py`** — phase-1 lock in `start_terminal_attempt` (+ `db.commit()` release,
  raw `item_ids` + `service_charge_rate`); persist tip at APPROVED + settle from durable tip +
  conflicting-tip/no-tip → `_park_terminal`; `TERMINAL_PENDING_DEADLINE_S` transport escalation.
- **`app/services/settlement.py`** — `_external_booking_reason` full per-component comparison.
- **`app/routers/pay.py`** — `_terminal_attempt` scoped to order+seat+provider; `start_terminal_payment`
  passes raw filter + rate and maps `PaymentError`→400; `terminal_cancel` refresh + CAS-loss catch.
- **`web/templates/…`** — unchanged from Slice 2b.
- **`tests/test_terminal.py`** — 17 tests (7 new: tip-durable-recovery, conflicting-tip,
  component-drift, scoped-lookup, transport-deadline, stale-cancel, phase-1 snapshot + PG
  lock-release probe); `_scenario` now disposes engines (was leaking PG connections).
- **`tests/test_pg_concurrency.py`, `tests/test_e2e.py`** — concurrency factory unchanged; e2e
  reverted to baseline (§7).

## 10. Areas of least confidence

1. **No live Square.** Terminal tests craft `ChargeResult`s / mock `create_checkout`; the
   provider↔Square HTTP mapping is unit-tested, not run against a sandbox terminal.
2. **e2e closeout (§7)** — still red, diagnosis corrected to cross-process DB visibility, harness
   redesign deferred.
3. **Transport deadline is age-based** (attempt `created_at`), a single owner in
   `advance_terminal_attempt`; a recovery worker is the Stage-2d consumer of the parked attempts.

**Stopped for review. Refund settlement/route (slices 3/4) not started.**
