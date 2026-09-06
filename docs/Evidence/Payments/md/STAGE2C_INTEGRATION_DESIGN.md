> **PROPOSED — NOT APPROVED FOR IMPLEMENTATION.**
> **NOT INDEPENDENTLY REVIEWED / NOT AUTHORIZED / NOT DEPLOYED.**
>
> Stage 2c audit and integration design. Target base: local `main` @ `f9bc865`.
> Source inspected: `fix/p0-security-and-payments` @ `58e0324`.
>
> Promoted from the loose working file `DESIGN_STAGE2C_INTEGRATION.md`
> (2026-09-02). The loose source contained self-asserted approval wording; those
> assertions are not accepted as evidence of independent approval. Stage 2c
> remains WIP and NOT AUTHORIZED — see `docs/03_CURRENT_WORK.md`. Branch
> existence is never approval.

# Stage 2c — audit and integration design

Date: 2026-09-02  
Target base: local `main` at `f9bc865`  
Source inspected: `fix/p0-security-and-payments` at `58e0324`  
Status: **PROPOSED — NOT APPROVED FOR IMPLEMENTATION; NOT INDEPENDENTLY
REVIEWED; NOT AUTHORIZED; NOT DEPLOYED. Do not integrate the source tip.**

This document is outside every Git worktree. The audit was read-only. No source,
branch, commit, remote, Render setting, or production data was changed.

## 1. Boundary

Production and `origin/main` run Release B Stage 1/2a/2b at `489f6d2`.
Local `main` adds only the deployment checkpoint at `f9bc865`.

Release B reached `main` by cherry-pick, so Git topology alone does not delimit
Stage 2c. Reproducible audit facts are:

```text
git merge-base main 58e0324       -> 07ee4f1
git log --oneline main..58e0324   -> 33 commits
git cherry -v main 58e0324        -> 23 equivalent (-), 10 unmatched (+)
```

Three of the ten `+` commits are older Release B commits whose content was
deliberately changed during integration (`81d0cd6`, `67aebfd`, `85dbbe2`). The
following seven commits are therefore a **manually curated logical remainder**,
not the literal output of `git log main..58e0324`:

```text
50f687a  settlement foundation and paid-item selection
e3c28f7  transaction/CAS/selection review fixes
f55f048  fingerprint versioning and TEXT migration
a0c0cf9  reconciliation tests
ca9ea30  per-row fingerprint classification and strict item ids
4f78b97  unrelated light-mode CSS — EXCLUDE
58e0324  WIP snapshot: manual and Square charge wiring plus review artifacts
```

`58e0324` is not a releasable commit. It contains the useful charge wiring, but
also 16 large review handoffs, `NEON_MIGRATION.md`, a migration shell script, and
other historical material. The scope measurements are:

```text
git diff --shortstat main 58e0324
  122 files, 29,677 insertions, 46,154 deletions
git diff --shortstat main...58e0324
  49 files, 33,554 insertions, 150 deletions
git diff --shortstat main...58e0324 -- app tests
  24 files, 6,537 insertions, 139 deletions
```

The two-dot comparison measures the two tree states; the three-dot comparison
measures source work since their old merge base. Neither is an integration cut.
Neither the branch tip nor the whole range may be merged/cherry-picked.

Refund route wiring was never implemented. The existing `/payments/{id}/refund`
route still uses the old local `Refund` service and does not call the durable
`RefundAttempt`/provider layer. Therefore this source can close only the manual
and Square **charge** path; it cannot honestly close all payment/refund work.

## 2. Blocking findings

### P0 — the “locked current snapshot” can still use stale ORM state

`payments.locked_seat_payable()` calls `_lock_order()`, which executes a scalar
`SELECT order.id ... FOR UPDATE`, then immediately calls `build_ledgers(db,
order)` on the already-loaded `Order` object. It never expires or refreshes the
order, seats, items, payments, allocations, or shares after acquiring the lock.

The route calls `_load()` and `_require_served()` before the helper, so those
relationships can already be populated. If another transaction settles the seat
while this request waits for the row lock, the waiter can acquire the lock and
still price the cached pre-lock ledger. That defeats the stated phase-1 invariant
and can open a Square checkout for items already paid.

The claimed regression test does not exercise this race: it pays and calls
`start_terminal_attempt()` in the same session after commit. It does not preload
in session A, mutate in session B, block A, then verify A observes B's commit.

Required correction: after the order lock is acquired, force authoritative
database state (`expire`/`refresh` with an explicitly proven loading strategy, or
requery the payable with fresh SQL in the locked transaction). Add a real
two-session PostgreSQL proof in which the waiting session preloads stale state.

### P1 — manual idempotency identity omits the payment instrument

`settle_manual_charge()` receives `instrument_id` and books the Payment under
that instrument, but `PaymentAttempt` and `intent_fingerprint()` identify only
`provider="manual"`. Cash, keyed Visa, e-transfer, and platform tenders can all
share that provider. A reused token with the same staff, selection, currency, and
money components but a different instrument is not an `IdempotencyConflict`.

This is especially unsafe after a first attempt remains `CREATED` because of
drift: the retry can settle a different tender under the old intent identity.
Even after success, a changed-instrument retry silently returns the earlier
Payment rather than rejecting the materially different request.

Required correction: persist `instrument_id` on the attempt and include it in a
new fingerprint version, or use an equally durable canonical tender identity.
Migration must classify existing versions without guessing. Add same-token /
different-instrument tests on SQLite and PostgreSQL.

### P1 — terminal settlement attributes the Payment to the poller

The attempt durably records the initiating `staff_id`, but `terminal_status()`
passes the currently authenticated poller's `staff.id` to
`advance_terminal_attempt()`, which passes it to `pay_seat()`. A different staff
member who can reach the scoped checkout URL can become the recorded payment
staff even though they did not create the intent.

Required correction: settle with `attempt.staff_id`. Treat the current caller as
the actor driving recovery only if a separate audited actor field is desired.
Add a test where one staff member starts and another polls.

### P1 — no real Square terminal acceptance evidence

The terminal suite mocks `create_checkout` and supplies synthetic
`ChargeResult`s. Provider HTTP mapping is unit-tested, but the complete flow has
not run against a Square sandbox terminal. This is a release-evidence gap, not a
reason to test with real production money.

Before deployment, either execute an owner-approved Square sandbox terminal
smoke or explicitly accept and record this residual risk. Production charging is
never an acceptable substitute for a sandbox gate.

### P2 — a GET request currently drives financial settlement

`terminal_status()` is a GET endpoint, but polling it calls
`advance_terminal_attempt()` and can persist processor evidence, create a local
Payment, or park reconciliation. CAS limits duplicate settlement, but browser,
proxy, crawler, and prefetch behavior should not be allowed to drive financial
state merely by reading a URL.

Required design decision for Cut 4: prefer a POST advancement endpoint (with the
waiting page explicitly posting each poll). If a read-only GET remains, it must
only report persisted state and never contact Square or mutate the database.

## 3. What is sound and reusable

- Attempt creation precedes processor I/O and forwards the durable idempotency
  key.
- Processor payment id, amount, currency, and terminal tip are write-once.
- External ambiguity parks for reconciliation instead of assuming failure.
- Settlement checks processor evidence and every local pre-tip component.
- Settlement CAS converges only on a proven winner.
- Checkout lookup is scoped by provider, order, and seat.
- Cancel cannot overwrite a winning approval transition.
- Pending terminal attempts have a bounded escalation deadline.
- Fingerprint-version migration fails closed on unclassifiable rows.

These properties should be ported, not rewritten casually.

## 4. Integration construction

Create a new isolated worktree/branch from `f9bc865`. Do not work in the `main`
worktree and do not use `58e0324` as a merge target.

### Cut 1 — settlement foundation

Port the logical content of `50f687a` through `ca9ea30` in reviewable order,
preserving the already-deployed Release B versions on `main`. Expected files:

```text
app/migrate.py
app/models/oltp.py
app/services/payment_attempts.py
app/services/settlement.py
tests/test_payment_attempts.py
tests/test_refund_attempts.py
tests/test_pg_concurrency.py
tests/test_pg_migration.py
tests/test_settlement.py
```

Resolve conflicts semantically; the five source commits predate the APP_ENV,
`.env.example`, timezone, and governance commits now on `main`.

Explicit non-regression zone: `git cherry` reports these older source commits as
unmatched because Release B intentionally changed them while integrating:

```text
81d0cd6  Stage 1 fail-closed startup/config
67aebfd  Stage 2b provider layer
85dbbe2  PostgreSQL payment-schema upgrade migration
```

For their overlapping files (`app/config.py`, `app/main.py`, `app/migrate.py`,
provider/payment services and associated tests), the deployed `main` version is
the baseline. Never restore a whole file from the source branch. In particular,
preserve absent/blank `APP_ENV` = production, the narrowed `card_terminal`
backfill, the atomic strict migration, provider evidence checks, and the deployed
timezone behavior.

### Cut 2 — corrected manual charge wiring

Port only the manual-charge pieces from `58e0324`, then fix instrument identity
before enabling the route. Include the new durable instrument field/fingerprint
version and migrations in this cut or a preceding schema cut. This is fingerprint
**v3**: new rows use v3, while existing v1/v2 rows remain classified as v1/v2;
the migration must never invent an instrument for a historical row. The route
must reject a reused token whose tender changed.

### Cut 3 — corrected locked snapshot

Port `locked_seat_payable()` only with an authoritative post-lock reload/query.
The exit criterion is the two-session PostgreSQL stale-preload proof, not merely
a `FOR UPDATE NOWAIT` lock-release test.

### Cut 4 — corrected Square terminal wiring

Port the charge orchestration, route/template changes, durable tip evidence,
scoped lookup, cancel race handling, and timeout escalation. Settle under
`attempt.staff_id`, not the poller's identity. Replace the mutating GET poll with
a POST advancement endpoint; any retained GET must be read-only.

### Exclusions

Exclude the CSS commit, all `REVIEW_HANDOFF*` files, `NEON_MIGRATION.md`,
`scripts/migrate_to_neon.sh`, and unrelated generated/history artifacts. Exclude
refund-route changes because none exist to review. Floor, Reservations, Schedule,
Kitchen B3/B4, `COOKIE_SECURE`, `DATABASE_URL`, and CRLF debt remain separate.

## 5. Gates

```text
C1  DESIGN REVIEW       NOT PASSED — independent review and approval required
C2  CURATED PORT        Cuts 1-4 composed linearly on f9bc865; exclusions proven
C3  STATIC/SQLITE       payment, settlement, terminal, config, timezone and all
                        existing SQLite entrypoints green
C4  POSTGRESQL          migration + concurrency + stale-preload + instrument and
                        cross-staff proofs; PG_TEST_DSN set; zero SKIP
C5  FRESH PROD RESTORE  new post-Release-B production dump restored locally;
                        existing empty/live attempt rows inventoried; strict
                        migration and second-run idempotency recorded
C6  SQUARE ACCEPTANCE   sandbox terminal smoke, or explicit owner risk acceptance
C7  MAIN ADVANCE        separately authorized fast-forward only
C8  PUSH/DEPLOY/SMOKE   separately authorized; configuration preflight repeated;
                        no real charge unless specifically authorized
```

No gate authorizes the next.

The fresh dump in C5 matters: the retained 2026-08-26 dump predates Release B and
cannot test migration of the now-deployed `payment_attempt`/`refund_attempt`
schema. It remains useful history, but is insufficient evidence for Stage 2c.

## 6. Exit condition and naming

Recommended scope name: **Stage 2c — durable charge wiring**. Do not call it
“payment/refund complete.” Completion means manual and Square charge routes use
the attempt/settlement spine and all five blocking findings above are closed or,
for Square sandbox only, explicitly accepted.

Durable external refund execution and refund reconciliation remain a later,
separately designed slice. The production risk register must continue to say
that card refunds are local-only until that slice deploys.
