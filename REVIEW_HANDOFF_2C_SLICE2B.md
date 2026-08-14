# Review Handoff — Stage 2c, Slice 2b: Square terminal charge wired through the durable attempt

Implements **Slice 2b only** (the card-terminal route), against the 10 guardrails and the
acceptance list in `CLAUDE_STAGE2C_SLICE2A_V5_SLICE2B_GO.md`. The manual/cash path (Slice 2a,
approved) and the durable-attempt/settlement spine are unchanged in contract; this slice reuses
them and adds an **external settle-vs-reconcile policy**. Refund wiring (slices 3/4) remains out
of scope.

> Still not a git repo — no `git diff`. Section 9 lists exact per-file changes.

---

## 1. Route flow

Three routes, now backed by the durable `PaymentAttempt`:

```
POST /orders/{o}/seats/{s}/pay-terminal        start_terminal_payment
  -> require non-empty pay_token (422 otherwise)
  -> charge.start_terminal_attempt(...)         [commit attempt, THEN Square]
  -> redirect to the wait page at the checkout id

GET  /orders/{o}/seats/{s}/terminal/{cid}       terminal_wait   (polling page)
GET  /orders/{o}/seats/{s}/terminal/{cid}/status terminal_status
  -> find the attempt by provider_checkout_id
  -> provider.poll(cid) -> ChargeResult
  -> charge.advance_terminal_attempt(...)        [record evidence, settle-or-reconcile]
  -> JSON {done | pending | canceled | reconciling}

POST .../terminal/{cid}/cancel                   terminal_cancel
  -> provider.cancel(cid); attempt -> CANCELLED (confirmed) or RECONCILIATION (ambiguous)
```

All Square I/O goes through the existing `SquareTerminalProvider` (charge/poll/cancel), which
already maps every processor outcome to a `PaymentAttemptStatus` and reads authoritative evidence
via `square.completed_payment_evidence`. The routes never call `square.*` directly anymore.

## 2. Idempotency lifecycle (#1/#2)

- The terminal form renders a **per-seat server token** (`pay_tokens_terminal[seat.id]`),
  separate from the manual form's token (a terminal charge is a different provider/intent).
- `start_terminal_attempt` uses that token as the attempt's `idempotency_key`; the attempt is
  **created and committed before any Square call** (guardrail #1 — verified in
  `test_start_commits_attempt_before_square_and_is_idempotent`, which reads the row from a fresh
  session inside the mocked `create_checkout`).
- The attempt's own key is forwarded to Square (`create_checkout(idempotency_key=...)`), so a
  retried submission reaches Square with the **same** key and cannot double-charge.
- A **double-submit reuses the one attempt**; if a checkout already exists the service returns its
  current state and does **not** open a second checkout (verified: `create_checkout` called once,
  one attempt row).
- A reused token with a **different** intent raises `IdempotencyConflict` → HTTP 409.

## 3. Transaction / lock boundaries (#3/#8/#10)

- **No order-row lock is held during the terminal/customer interaction** (#3): `start` only reads
  the ledger to price the charge, commits the attempt, and calls Square. The lock is taken only
  later, inside `pay_seat`, for the brief local settlement.
- On approval, the processor evidence is committed as `PROCESSOR_APPROVED` **before** local
  settlement (#8), so a Square-success/local-failure leaves a durable APPROVED attempt with the
  provider payment id — `payment_attempts.requires_reconciliation()` surfaces it to the recovery
  worker (verified in `test_approved_evidence_is_recoverable_before_settle`).
- The local settlement (`pay_seat`) writes **Payment + allocations + seat state + attempt→SETTLED
  atomically** in one commit (#10), via the same no-commit core as the manual path.

## 4. Terminal-tip semantics (#5)

The tip is **not** in the attempt snapshot (`tip_cents = 0` at creation — the guest tips on the
machine). `advance_terminal_attempt` books the Payment with `tip_cents = result.tip_cents` (the
processor-confirmed tip from `completed_payment_evidence`). The external local-consistency check
deliberately does **not** compare tip against the snapshot; it compares the **pre-tip** amount.
Verified: `Payment.tip_cents == 250` for a terminal-reported 250 tip.

## 5. Processor evidence handling (#4)

`_mismatch_reason` (external branch, unchanged from slice 1) gates settlement on authoritative
evidence:
- `processor_currency == attempt.currency` else reconcile (no Payment);
- `processor_amount_cents (pre-tip base) == attempt.expected_total_cents` else reconcile.
- The `PROCESSOR_APPROVED` transition itself refuses to record without a provider payment id +
  non-negative amount + valid currency (`_require_approval_evidence`).

Verified: `test_amount_mismatch_reconciles_no_payment`, `test_currency_mismatch_reconciles_no_payment`.

## 6. Order-drift behavior (#6/#7)

After Square approval (money moved), the local booking must reproduce the attempt's **pre-tip
amount and paid-item selection** (`settlement._external_booking_reason`, run under the reacquired
order lock). If the order drifted (an item was paid/voided in between):
- `pay_seat` books nothing outstanding → raises `PaymentError` → caught → **reconcile**;
- or books a different amount → pre-tip mismatch → **reconcile**.

Either way the attempt is parked `REQUIRES_RECONCILIATION` (evidence preserved) and **no second
Payment** is booked — never a manual-style reject/retry, because external money moved. Verified in
`test_order_drift_after_capture_reconciles` (the seat's item is paid on cash first, then the
terminal attempt reconciles and books no second Payment).

## 7. Failure recovery & ambiguity (#8/#9)

- `SquareTerminalProvider.poll` maps every ambiguous processor state to
  `REQUIRES_RECONCILIATION`, preserving any payment id: transport error → keep polling; definitive
  lookup error → reconcile; COMPLETED without a payment id → reconcile; COMPLETED with
  incoherent evidence → reconcile. `advance_terminal_attempt` parks the attempt accordingly and
  the wait page shows a "being verified — do not charge again" state (verified in
  `test_reconcile_result_parks_and_keeps_evidence`).
- Re-polling after settlement is idempotent — it returns `done` on the same Payment, no second
  charge (`test_resettle_is_idempotent`).
- Cancel: a confirmed CANCELED → attempt CANCELLED; an ambiguous cancel (Square may have
  completed) → RECONCILIATION.

## 8. Tests & results

| Suite | Tests | Assertions | Backends |
|-------|------:|-----------:|----------|
| `test_terminal.py` (new) | 10 | 30 | SQLite-FK + Postgres 16 |
| `test_charge.py` (manual regression) | 14 | 42 | SQLite-FK + Postgres 16 |
| `test_settlement.py` | 9 | 18 | SQLite-FK + Postgres 16 |
| `test_pg_concurrency.py` | 10 | 21 | Postgres 16 |

**Terminal acceptance coverage:** attempt-before-Square (#1), duplicate-submit → one attempt/one
checkout (#2), no order lock during interaction (#3, by construction + no lock call in `start`),
amount-match settles, amount/currency mismatch → reconcile, terminal tip → Payment tip (#5),
ambiguous/incoherent evidence → reconcile no auto-settle (#9), order-drift-after-capture →
reconcile (#7), evidence committed before settle / recoverable (#8), one Payment under concurrent
settlement (Postgres, #10), canceled → CANCELLED.

**Postgres concurrency (real row locking / threads):** all green, including the new
`test_concurrent_terminal_settle_one_payment` (two concurrent settlements of one APPROVED terminal
attempt → exactly one Payment) and the existing manual duplicate-token 8-way proof.

**Regression:** manual/cash suite green (SQLite + Postgres); `test_pg_migration`,
`test_payment_attempts`, `test_refund_attempts`, `test_payment_providers`, `test_reconciliation`,
`test_money`, `test_security`, `test_templates`, `test_admin`, `test_config`, `test_schedule`
green. `test_e2e`: settlement/partial-close pass; the two closeout checks still fail as documented
(pre-existing stale-session harness, unrelated — the terminal path is Square-gated and not
exercised by e2e).

## 9. Exact changes (no git repo — per-file)

- **`app/services/settlement.py`** — external settle-vs-reconcile: `_is_external`,
  `_external_booking_reason` (pre-tip + selection, tip excluded), `_park_reconcile` (rollback the
  mismatched Payment, converge-on-winner / already-parked / else conflict), and `settle_charge`
  now branches external (reconcile on local drift, catch `PaymentError`) vs local (SettlementDrift).
- **`app/services/charge.py`** — `start_terminal_attempt` (commit attempt before Square, forward
  the key, idempotent double-submit) and `advance_terminal_attempt` (commit evidence before
  settle, book with terminal tip, reconcile on drift/mismatch).
- **`app/routers/pay.py`** — `start_terminal_payment` / `terminal_status` / `terminal_cancel`
  rewired to the attempt + provider; require non-empty terminal `pay_token`; `_terminal_attempt`
  lookup by checkout id; render issues `pay_tokens_terminal`.
- **`web/templates/pay.html`** — hidden `pay_token` in the terminal form.
- **`web/templates/terminal_wait.html`** — a `reconciling` state ("being verified — do not charge
  again").
- **`tests/test_terminal.py`** (new) — 10 tests. **`tests/test_pg_concurrency.py`** — added
  `test_concurrent_terminal_settle_one_payment`; `_pay_factory` made snapshot-consistent.

## 10. Areas of least confidence

1. **No live Square exercised.** Tests craft `ChargeResult`s and mock `create_checkout`; the
   provider↔Square HTTP mapping (`square.py`) is covered by `test_payment_providers` but not
   against a real sandbox terminal. The trust gap is the sandbox, not the branch logic.
2. **`advance_terminal_attempt` commits the APPROVED evidence with `commit=True` even when called
   with `commit=False`.** This is deliberate (guardrail #8 wants evidence durable before settle),
   but it means the evidence transition is not part of the caller's outer transaction. Flagging in
   case you want the two-phase boundary drawn elsewhere.
3. **Selection for a terminal charge is the whole served-outstanding seat** (the terminal form has
   no per-item checkboxes). Per-item terminal selection would be a small follow-up.
4. **e2e** still shows the two pre-existing closeout reds (harness, untouched).

**Stopped for review. Refund settlement/route (slices 3/4) not started.**
