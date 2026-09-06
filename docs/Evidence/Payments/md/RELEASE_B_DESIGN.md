> **DESIGN — Release B (Payment/Security Stage 1, 2a, 2b), Rev 3.** The design
> artifact the deployed baseline `489f6d2` was built against; gates G1-G8 are
> recorded passed in `docs/03_CURRENT_WORK.md`.
>
> Promoted verbatim from the loose working file `DESIGN_RELEASE_B.md`
> (2026-08-27); body unchanged below this header, including its own status line
> and revision history. Deployment status of record: `docs/03_CURRENT_WORK.md`.

# DESIGN — Release B (Payment/Security Stage 1, 2a, 2b) — **Rev 3**

Status: DESIGN ONLY, resubmitted for independent review.
Nothing implemented, cherry-picked, committed, pushed or deployed.
Author: Claude. Date: 2026-08-27.

**Rev 3 supersedes Rev 2.** Rev 2's APP_ENV inversion was under-specified in a
way that would have broken the suite and produced self-contradictory log
messages. Five corrections, all from independent review, marked `REV3`.

Revision history, kept visible rather than edited away:

```text
Rev 1   initial design
Rev 2   corrected: missed card_terminal backfill; untraced import graph;
        inflated entrypoint count. Two of three found by review, not by me.
Rev 3   corrected: split-brain app_env(); tests/_env.py omitted from scope;
        over-deletion of the Stage 1 config proof; two overstated claims.
        All five found by review.
```

---

## 0. Scope — what this release delivers, and what it does not

**This release delivers foundations only.** Verified, not assumed: in the
approved tip `2c2e676`, no router and no service calls `payment_attempts`,
`refund_attempts` or `payment_providers`. The grep returns empty. The wiring
lives entirely in Stage 2c, which is excluded:

```text
modules that call the new services, at the EXCLUDED tip 58e0324:
  app/routers/pay.py        <- the wiring
  app/services/charge.py    <- 2c only
  app/services/settlement.py<- 2c only
```

So Release B ships **schema, configuration and the service layer**, dormant.

### What is therefore still open after this release

- **The Square charge durability window is NOT closed.** `PaymentAttempt` exists
  and is correct, but nothing writes one on a real charge. A processor success
  can still precede durable local persistence, exactly as today.
- **External refunds are NOT executed.** `RefundAttempt` exists; the refund path
  remains local-only.
- **Full processor reconciliation remains absent.**

These close in Stage 2c, which stays excluded and unauthorised. The risk backlog
must keep them open after this release ships — shipping Release B must not be
read as having fixed them.

### What it does deliver

Production configuration that fails closed, split liveness/readiness probes, a
durable-attempt schema with its state machine, a pluggable provider layer, and
23 review-fix commits of independent hardening on all of it. That is real value:
it is the foundation 2c is built on, and it lands with no behavioural change to
any route in use today.

---

## 1. Inventory — base, source, SHAs, divergence

```text
BASE (target of integration)
  main (local)               b0ab8e7   must remain an ancestor of Release B
  origin/main                7068bb4   unchanged
  production                 7068bb4   healthy since 2026-08-27 11:19:44

SOURCE
  fix/p0-security-and-payments   58e0324   branch tip — NEVER integrate
  origin/fix/p0-security-...     58e0324   same, already published

COMMON ANCESTOR
  merge-base(b0ab8e7, fix/p0)    07ee4f1   2026-08-08

DIVERGENCE
  27 commits exclusive to b0ab8e7      (Kitchen reconstruction + hardening + docs)
  33 commits exclusive to fix/p0       (26 approved + 7 excluded)
  86 files changed on the main line since 07ee4f1
```

### The cut boundary

```text
APPROVED   81d0cd6 .. 2c2e676     26 commits, contiguous
EXCLUDED   50f687a .. 58e0324      7 commits, contiguous SUFFIX
```

The excluded suffix, enumerated:

```text
50f687a  feat(2c slice 1): charge settlement service + paid-item selection
e3c28f7  fix(2c slice 1 review): transaction contract, CAS convergence
f55f048  fix(2c slice1 v0): fingerprint versioning, VARCHAR->TEXT, strict item ids
a0c0cf9  test(2c): cover reconciliation validation + charge-resolution branches
ca9ea30  fix(2c v2): per-row fingerprint backfill, unsupported-version fail-closed
4f78b97  Light mode: drop the background-photo veil        (unrelated UI, owner-excluded)
58e0324  WIP snapshot: Stage 2c slices 2a/2b + review fixes (branch tip, owner-excluded)
```

Because the exclusions are a contiguous suffix, **no interleaved cut points are
needed** — unlike the Kitchen release, which required seven cuts through ~40
interleaved Floor commits. `2c2e676` is a real, reviewed tip of the approved
work, not a synthetic boundary.

### Proof the boundary is real, not inferred from commit subjects

```text
approved range (07ee4f1..2c2e676)   21 files   +4091 / -23
excluded range (2c2e676..58e0324)   37 files  +29481 / -145
```

Files appearing **only** in the excluded range: `app/services/charge.py`,
`app/services/settlement.py`, `app/services/payments.py`, `web/static/app.css`,
`web/templates/pay.html`, `web/templates/terminal_wait.html`,
`tests/test_charge.py`, `tests/test_settlement.py`, `tests/test_terminal.py`,
`tests/test_e2e.py`, `scripts/migrate_to_neon.sh`, and 17 `REVIEW_HANDOFF*.md`
files (~29,000 lines of review transcript).

The approved slice touches **no template and no CSS at all**.

---

## 2. Map — commit -> files -> behaviour

### Stage 1 — production config fails closed (1 commit)

```text
81d0cd6  .env.example, REMEDIATION_PLAN.md, app/config.py, app/main.py,
         app/migrate.py, app/security.py, docker-compose.yml,
         docker-entrypoint.sh, tests/test_config.py
```

Introduces `app/config.py` (stdlib-only, imports no app code, so no cycle):
`app_env()`, `is_production()`, `venue_currency()`, `ConfigError`,
`validate_startup_config()`. Splits liveness from readiness: `/healthz` stops
touching the database; `/readyz` proves the DB is reachable and the `staff`
table exists, returning 503 otherwise with the error logged server-side only.
Makes `migrate.run()` strict in production and the container entrypoint fail
closed.

### Stage 2a — durable charge attempt (1 commit)

```text
0a98b12  app/models/oltp.py, app/services/payment_attempts.py,
         tests/test_payment_attempts.py
```

`PaymentAttempt` is designed to be written and committed **before** the external
processor is contacted. Dormant in this release — see §0.

### Stage 2b — pluggable provider layer (1 commit)

```text
67aebfd  app/migrate.py, app/models/oltp.py, app/routers/pay.py,
         app/services/payment_providers.py, app/services/square.py,
         tests/test_payment_providers.py
```

`PaymentInstrument.provider` selects the settling adapter. `"manual"` is a real
registered key meaning staff-recorded, no processor.

### Review-fix chain — 23 review-fix commits  `REV3`

> Rev 2 called these "23 rounds". They are **23 review-fix commits**, grouped
> into six labelled batches by their subjects. The count of independent review
> rounds is not established by the commit log and is not claimed here.

```text
1d88455 555dc62 69860db a6addbe          labelled 2a/2b A–E
85dbbe2 68b5f84 be115ee 3a4e552 0621b6a  labelled h2
f99e815 5572a16 58abfd1 9dfe1d7          labelled h3
646aabc 911b61f b8b0636 ceb2004          labelled h4
3b6f7bd cfac2bf 9c2e954 f44bb3f          labelled h5
3166c67 2c2e676                          labelled h6
```

Themes, which become the invariants of §5: concurrency via guarded UPDATE,
separate refund lifecycle, refund idempotency fingerprints, processor
uncertainty never assumed away, processor-confirmed amount/currency as
write-once evidence, enforceable capabilities, audited reconciliation authority,
defensive parsing of untrusted payloads, strict money parsing.

`RefundAttempt` enters at `1d88455`, not at 2a.

---

## 3. Dependencies and cross-domain contamination

### Contamination: none found

`app/models/oltp.py` receives exactly three hunks, verified by hunk header:

```text
@@ -23,6 +23,11    add PROVIDER_KEY_LEN = 30
@@ -584,6 +589,14  add PaymentInstrument.provider
@@ -907,6 +920,282 add PaymentAttemptStatus, RefundAttemptStatus,
                   PaymentAttempt, RefundAttempt
```

No import line is altered. Nothing touches `Reservation`, `Station`,
`PreparationTask`, `Zone` or `Table`. This is the check that failed on the
Kitchen design (`reservation_table`), so it was run explicitly rather than
inferred from a clean merge.

### Conflict surface: six files, three conflicting files forecast  `REV3`

```text
file                  payment side   main side   merge-tree forecast
.env.example          +24/-4         +24/-5      CONFLICT
app/migrate.py        +190/-5        +323/-4     CONFLICT
app/security.py       +14/-1         +91/-9      CONFLICT
app/main.py           +33/-2         +20/-0      auto-merges
app/models/oltp.py    +289/-0        +310/-0     auto-merges
docker-compose.yml    +19/-0         +13/-0      auto-merges
```

> Rev 2 wrote "three commits stop for resolution". That was not proved.
> `git merge-tree` forecasts a **three-file conflict on the squashed
> base-to-tip comparison**; it says nothing about how many of the 26 individual
> cherry-picks will halt. A sequential cherry-pick may stop more times (the same
> file contested by several commits) or fewer. **The number of halts is
> unknown until the operation runs**, and the plan must not budget on it.

The three that auto-merge are **not thereby proven correct** — a clean merge
shows syntactic reconcilability, not semantic correctness. They are review
targets in §6. The other 15 files land on virgin ground.

---

## 4. Schema / migration delta and data impact

> **Rev 1 was wrong here** and Rev 2 corrected it. Rev 1 classified
> `PROVIDER_BACKFILL` as a no-op and stopped looking, missing a second, separate
> backfill that **does** rewrite a live production row. Found by review.

```text
NEW TABLES (created empty by create_all — no data impact)
  payment_attempt      ~30 columns, 6 constraints/indexes
  refund_attempt       ~18 columns, 4 constraints/indexes

NEW COLUMN ON AN EXISTING TABLE
  payment_instrument.provider   VARCHAR(30) NOT NULL DEFAULT 'manual'

ROW REWRITE ON AN EXISTING TABLE   <- migrate.py:318-323
  UPDATE payment_instrument SET provider='square_terminal'
   WHERE code='card_terminal' AND provider='manual'

NO-OPS AGAINST PRODUCTION (table does not exist there yet)
  6 ADDED_COLUMNS on payment_attempt / refund_attempt
  WIDENED payment_attempt.provider VARCHAR(20)->VARCHAR(30)
  UNIQUE uq_attempt_provider_payment, uq_attempt_provider_checkout
  PROVIDER_BACKFILL payment_attempt 'square' -> 'square_terminal'
  DROP DEFAULT on payment_attempt.provider (Postgres only)

ALREADY PRESENT ON THE MAIN LINE — not a delta
  WIDENED staff.pin_code VARCHAR(128)
```

### The card_terminal rewrite, in full

Production almost certainly holds a `card_terminal` instrument — `pay.py`
creates one lazily the first time a terminal payment is taken. The migration
adds `provider` with default `'manual'`, then immediately rewrites that one row
to `'square_terminal'`.

The `AND provider='manual'` predicate is deliberate (`646aabc`, "narrow terminal
backfill"): it makes the statement idempotent and will never overwrite a
provider an operator set on purpose. That is good design and why the statement
is safe. It is still **a write to production data and is inventoried as one**.

Behavioural consequence today: none, because nothing reads
`PaymentInstrument.provider` on any live route (§0). The value is staged for 2c.
This is *latent*, which is not the same as harmless.

### Assessment

The `payment` and `refund` tables — real money records — are **not modified**.
`payment_instrument` is the seeded instrument catalogue, on the order of ten
rows. On PostgreSQL 11+, `ADD COLUMN ... NOT NULL DEFAULT <constant>` is
metadata-only, no table rewrite, no long lock. The UPDATE touches at most one
row.

Migration is additive-only: no DROP COLUMN, no type change on a populated
column, no destructive backfill. `run()` gains `strict=False`, so the main
line's `migrate.run(engine)` call site stays valid.

### Required before/after checks on the dump restore

```sql
-- BEFORE
SELECT count(*) FROM payment_instrument;
SELECT id, code, instrument_type FROM payment_instrument ORDER BY id;
SELECT to_regclass('payment_attempt'), to_regclass('refund_attempt');  -- expect NULL, NULL

-- AFTER
SELECT count(*) FROM payment_instrument;                    -- must be unchanged
SELECT provider, count(*) FROM payment_instrument GROUP BY provider ORDER BY 1;
SELECT id, code, provider FROM payment_instrument WHERE code='card_terminal';
SELECT count(*) FROM payment_attempt;                       -- must be 0
SELECT count(*) FROM refund_attempt;                        -- must be 0
SELECT count(*) FROM payment;  SELECT count(*) FROM refund; -- must both be unchanged
```

Acceptance: `payment_instrument` count unchanged; every row `'manual'` except
`card_terminal` = `'square_terminal'`; `payment` and `refund` counts identical
to the pre-migration values; both new tables present and empty. Then **re-run
the migration on the same database** and assert it reports no further change —
idempotency demonstrated, not argued.

---

## 5. Invariants

### Payment

- A charge attempt is durably committed **before** the processor is contacted —
  as a property of the service layer. Dormant until 2c wires it (§0).
- `provider='manual'` preserves existing behaviour for every pre-existing
  instrument, except `card_terminal`, which the migration sets to
  `'square_terminal'` (§4). No live route reads the column in this release.
- Processor-confirmed amount and currency are **write-once evidence** (`3a4e552`).
- No charge is approved on incomplete processor evidence (`5572a16`); evidence
  coherence is validated before approval (`b8b0636`).
- Currency defaults to `venue_currency()`, not hardcoded CAD (`911b61f`).
- `payment` and `refund` rows are untouched.

### Security

- Production config fails closed: missing `SECRET_KEY` or a *partial* Square
  config aborts startup. All-or-nothing on the three server-side Square vars.
- The deployed import-time `SECRET_KEY` guard from `b0ab8e7` survives this
  release **unchanged** (§6 Cut 1). This is the load-bearing invariant of the
  whole integration.
- The two guards are **independent and both required**: `app.security` protects
  the signing key at import; `validate_startup_config()` protects deployment
  configuration at startup. Neither subsumes the other, and they are proved
  separately (§6 Cut 6, §7).
- `/healthz` is liveness only; `/readyz` proves the DB and returns 503 until it
  can, with error detail logged and never returned.
- Untrusted processor payloads parsed defensively (`f44bb3f`); money parsing
  rejects float, bool and Decimal (`2c2e676`).
- Unknown provider capabilities are rejected, not ignored (`9dfe1d7`).

### Idempotency

- `uq_attempt_idempotency_key`, `uq_refund_idempotency_key` — a replayed request
  cannot create a second attempt; the collision surfaces as `IntegrityError`
  and is handled, not swallowed.
- `uq_attempt_provider_payment` / `uq_attempt_provider_checkout` —
  provider-scoped, so two providers may legitimately reuse an id.
- `uq_attempt_payment` — at most one attempt per `Payment`.
- Refund idempotency uses an intent fingerprint (`68b5f84`); an external refund
  needs a durable `provider_refund_id` before it may pend or complete (`3166c67`).
- The `card_terminal` backfill is idempotent by its `provider='manual'` guard.

### Concurrency — and one thing it is NOT

Safety comes from **guarded UPDATE with a rowcount check (CAS)** plus unique
constraints, *not* `SELECT ... FOR UPDATE`. `with_for_update` appears nowhere in
`payment_attempts.py` or `refund_attempts.py`.

This matters because `PaymentAttempt` declares four `lazy="joined"`
relationships (`order`, `seat`, `staff`, `payment`), two nullable. Locking that
entity would hit the *identical* PostgreSQL defect that produced the 500s in
both fire routes — `FOR UPDATE cannot be applied to the nullable side of an
outer join`. It does not, so this is a **latent trap for future work, not a
present defect**, recorded so whoever adds row locking later does not walk into
it.

---

## 6. Controlled cut plan onto b0ab8e7

```text
git switch -c release/payment-security b0ab8e7
git cherry-pick 81d0cd6^..2c2e676        # 26 commits, no squash, no merge commit
```

`b0ab8e7` remains an ancestor by construction. The cherry-pick will halt an
**unknown number of times** (§3); each halt is resolved against the rules below,
which are stated per file, not per commit.

### Cut 1 — `app/security.py` :: preserve b0ab8e7 ENTIRELY  <- BLOCKING (F1)

Stage 1 carries an **older, weaker `_secret()`**:

```text
                          Stage 1 (81d0cd6)        deployed b0ab8e7
strip() the key           yes, and USES it         detects and REJECTS padding
fails closed              only if is_production()  unconditionally
minimum length            none                     >= 32 bytes UTF-8
rejects the public key    no                       yes
validated at              call time                MODULE IMPORT
```

Taking it would restore a silent `_DEV_SECRET` fallback wherever `APP_ENV` is
unset — the exact failure that signed production cookies with a source-code key
until 11:19:44 on 2026-08-27. It would also reintroduce `.strip()`-then-use,
which independent review already rejected on the grounds that a key is opaque
material and must never be normalised.

**Resolution: `app/security.py` is preserved from `b0ab8e7` in full.** Nothing
is taken from theirs — not the `_secret()` body, not the `from app.config import
ConfigError, is_production` line. Verifiable acceptance criterion:

```text
git diff b0ab8e7 <integration-tip> -- app/security.py   ->   must be EMPTY
```

### Cut 2 — `app/migrate.py` :: union of Kitchen and Payment

Main side owns the Kitchen migration (`_ensure_prep_task_index`,
`_backfill_prep_tasks`, `_pg_boolean_ddl`); payment side adds
`_migrate_payment_hardening`, `_constraint_exists`, `_duplicates`,
`PROVIDER_BACKFILL` and the `strict` parameter. The structures are additive
tuples and independent functions, so a union is correct — but the `run()`
signature and the **ordering of steps** are resolved by hand and read back in
full. Neither side's migrations may be dropped or reordered relative to their
own siblings.

### Cut 3 — `.env.example` :: union, minus one regression  <- BLOCKING (F4)

Take the payment side's `APP_ENV` and Square blocks. **Preserve `b0ab8e7`'s
`SECRET_KEY` and `ALLOW_INSECURE_DEV_SECRET` blocks verbatim.** Stage 1's
version states the key *"Signs the login cookie and salts PIN hashes"*, which is
false — PINs use PBKDF2 with a per-PIN random salt, independent of `SECRET_KEY`
— and was deliberately removed during the hardening slice.

The `APP_ENV` block's text must be rewritten to match the policy decided in §8:
absence means production, not development.

### Cut 4 — the three auto-merging files :: verify, do not trust

- `app/main.py` — `validate_startup_config()` runs from `main.py` while the
  deployed `_secret()` guard runs at `app.security` import. **Establish the
  ordering** and confirm which message an operator sees first on a misconfigured
  boot. The two guards overlap on `SECRET_KEY` and the weaker one must never be
  the one that decides.
- `app/models/oltp.py` — confirm the three payment hunks and nothing else.
- `docker-compose.yml` — payment adds 19 lines; main added `env_file: .env`.
  Confirm both survive.

### Cut 5 — `docker-entrypoint.sh` :: keep fail-closed (owner-decided)

Stage 1 removes the `|| echo ... (continuing)` fallback so a bootstrap failure
aborts the container under `set -e`. **Owner decision: keep it.**

Consequence to record rather than discover: on Render a config error now kills
the container at the entrypoint, before Gunicorn starts, so the familiar
`Worker failed to boot` / exit-3 signature is replaced by a different one. The
runbook must say so, because the next incident will otherwise be read against
the wrong signature.

Also the file carrying the CRLF debt, which stays out of scope. Editing its
content neither fixes nor worsens that; the overlap is noted, not acted on.

### Cut 6 — test environment and `test_config.py`  <- BLOCKING (F3)  `REV3`

> Rev 1 claimed all six new suites break on the import-time guard, from a grep
> that never traced the import graph. Rev 2 corrected that to one suite. **Rev 2
> then made the opposite error**: having scoped the problem to `app.security`,
> it never asked what the *new* APP_ENV policy does to the *existing* suites.
> Found by review.

#### 6a — `tests/_env.py` is in scope, and it is the whole fix for the regression

With absence meaning production (§8), `app.main` calls
`validate_startup_config()` at import. That function requires a real
`SECRET_KEY` and **does not honour `ALLOW_INSECURE_DEV_SECRET`** — the opt-out
is `app.security`'s, and config validation deliberately knows nothing about it.
Every entrypoint that imports `app.main` would die at import.

Measured, not estimated:

```text
entrypoints that reach app.main                    9
  pg_fire_routes_proof, test_admin, test_e2e, test_kds_task_reads,
  test_kds_task_ready, test_prep_tasks, test_schedule, test_security,
  test_stations
of those, already importing tests/_env.py          9   (all of them)
entrypoints importing tests/_env.py today         12
```

All nine are already inside the twelve, so **one line in one file fixes all
nine** and no test gains a new import:

```python
# tests/_env.py
os.environ.setdefault("APP_ENV", "test")            # NEW
os.environ.setdefault("ALLOW_INSECURE_DEV_SECRET", "1")
```

`setdefault`, matching the file's existing contract: a caller who exports a real
`APP_ENV` keeps it, so the same suites can still be pointed at a
production-shaped environment. `"test"` is a member of `_DEV_ENVS`.

The module docstring must be updated with it. Today it explains itself purely in
terms of `app.security`; after this change it also governs configuration policy,
and a stale docstring here is exactly the kind of documentation drift this
project has already had to correct twice.

`tests/test_secret_key.py` continues **not** to import `_env` — it owns the
variables under test, and it does not import `app.main` (verified: zero
references), so the new policy does not reach it.

#### 6b — `test_config.py` :: replace the mixed test, do not merely delete it

`test_config.py` is the only new suite that reaches `app.security`, and its
problem is not the import — it encodes the **superseded APP_ENV-based policy**:

```text
test_dev_allows_fallback_secret        asserts _secret() == _DEV_SECRET
                                       -> the deployed guard REFUSES this
test_production_without_secret_...     asserts the raise happens only in production
                                       -> mixes TWO policies in one test
```

The first is actively dangerous: a maintainer seeing it fail could "fix" it by
weakening `_secret()`, turning a red test into a reopened vulnerability. It is
deleted outright — its subject is covered better by the 17-case subprocess
matrix in `tests/test_secret_key.py`.

> The second must **not** simply be deleted, which is what Rev 2 proposed.
> `test_secret_key.py` proves `app.security`. It proves **nothing** about
> `validate_startup_config()`, which is new in Stage 1 and is a different guard
> with a different trigger. Deleting the test wholesale would ship Stage 1's
> central behaviour unproven. Found by review.

It is **replaced by a config-only test**, with the `security._secret()`
assertion removed:

```text
DELETE   test_dev_allows_fallback_secret
                 obsolete policy, actively harmful, superseded by test_secret_key.py

REPLACE  test_production_without_secret_fails_closed
   with  test_validate_startup_config_requires_secret_in_production
                 APP_ENV=production, SECRET_KEY absent
                 -> validate_startup_config() raises ConfigError
                 asserts ONLY the config guard; no security._secret() assertion

KEEP     test_partial_square_fails_closed and the Square all-or-nothing cases
                 Stage 1's other real subject

ADD      import _env at the top
ADD      a case asserting absence of APP_ENV is treated as production
                 the §8 policy, tested at its own boundary
ADD      a regression sentinel: the deployed guard still refuses the public dev
                 key with Stage 1 beside it
```

Every case sets `APP_ENV` explicitly rather than relying on the default —
`_env.py` sets `"test"`, and a test of production behaviour must say so itself.

---

## 7. Test matrix and required proofs

```text
suite                        engine       _env      gate
tests/_env.py                —            —         AMENDED: +APP_ENV=test  [REV3]
tests/test_config.py         env only     +new      Stage 1 config, redesigned §6b
tests/test_payment_attempts  SQLite       no        attempt state machine
tests/test_payment_providers SQLite       no        provider contract, capabilities,
                                                    evidence, money parsing
tests/test_refund_attempts   SQLite       no        refund lifecycle + idempotency
tests/_pay_fixture.py        SQLite       no        helper, not an entrypoint
tests/test_pg_migration.py   PostgreSQL   no        upgrade migration [PG_TEST_DSN]
tests/test_pg_concurrency.py PostgreSQL   no        concurrency proofs [PG_TEST_DSN]
+ all 12 existing entrypoints on b0ab8e7 — full regression. Nine of them import
  app.main and are the ones the _env amendment protects; tests/test_secret_key.py
  is outside _env by design and is the F1 regression sentinel.
```

### False-green trap — gated mechanically  <- BLOCKING (F6)

Both PostgreSQL suites **SKIP and exit 0** when `PG_TEST_DSN` is unset:

```text
"SKIP: PG_TEST_DSN not set (migration-upgrade tests are Postgres-specific)"
"SKIP: PG_TEST_DSN not set (Postgres concurrency tests not run)"
```

The variable is `PG_TEST_DSN`, **not** `DATABASE_URL` — setting the wrong one
still shows green. Gate 5 of the Kitchen release failed in exactly this class.
Acceptance is mechanical: capture stdout and **assert the string `SKIP` does not
appear**. An exit code of 0 is not evidence.

### Proofs required beyond the suites

1. **Migration against a production-dump restore**, with the before/after
   queries of §4 recorded verbatim, including the `card_terminal` row, and a
   second run proving idempotency.
2. **Concurrency proof instrumented at the statement level**, barrier inside
   `before_cursor_execute` on the contended statement. Two earlier attempts in
   this project put the barrier before the transaction began and proved nothing;
   both were caught in review. Stated here to prevent a third.
3. **Boot proof**: the integrated branch starts under Compose with a valid
   `SECRET_KEY` and refuses to start without one — the deployed guard still
   behaving as deployed with Stage 1 beside it.
4. **`git diff b0ab8e7 <tip> -- app/security.py` is empty** (Cut 1).
5. **Policy proof**: with `APP_ENV` unset, `app_env()` returns `"production"`
   and `is_production()` is `True` — the two agree (§8).

---

## 8. Risks, policy decisions, rollback

### Blocking findings

```text
F1  Stage 1 _secret() would REGRESS the deployed guard      §6 Cut 1
F2  APP_ENV policy — decided below; one code change + one
    test-env amendment                                      §8, §6 Cut 6a
F3  test_config.py encodes the superseded policy            §6 Cut 6b
F4  .env.example would reintroduce the false PIN claim      §6 Cut 3
F5  entrypoint fail-closed changes the deploy signature     §6 Cut 5 [decided: keep]
F6  PG suites SKIP exit 0 — false green                     §7
```

### F2 — APP_ENV policy: DECIDED, centralised in `app_env()`  `REV3`

`app_env()` reads `APP_ENV` and defaults to `"development"`; `is_production()`
is false for `{development, dev, local, test, testing, ci}`. Absence therefore
means "not production" — fail-open by default, the same shape of assumption that
caused the 2026-08-27 incident.

**Decision: absent or blank `APP_ENV` means PRODUCTION. Development and test
behaviour require an explicit, recognised value.**

> Rev 2 proposed changing only `is_production()`. That would have split the
> policy across two functions: with `APP_ENV` unset, `app_env()` would keep
> answering `"development"` while `is_production()` answered `True`. The
> `ConfigError` message interpolates `app_env()` — so the process would have
> aborted with a message reading `SECRET_KEY is required in production
> (APP_ENV=development)`. A self-contradicting error message during an outage is
> worse than no message. Found by review.

The policy lives in **one function**:

```python
def app_env() -> str:
    """The deployment environment. Absent or blank means production: a
    deployment that never declares itself is treated as the strict case, never
    the permissive one."""
    value = os.environ.get("APP_ENV", "").strip().lower()
    return value or "production"


def is_production() -> bool:
    return app_env() not in _DEV_ENVS
```

`is_production()` is left as it was. Every caller, every log line and every
error message now agrees, because there is only one source of the answer.

Two consequences that must be planned for, not discovered:

**(a) This is a modification to cherry-picked code.** It must NOT be smuggled in
during conflict resolution. It lands as **its own commit on top of the 26**,
with its own message and its own review, together with the `tests/_env.py`
amendment (§6 Cut 6a) that it makes necessary — the change and its blast radius
in one reviewable unit. The integration stays a faithful cherry-pick plus one
explicit, attributable change.

**(b) It makes the next deploy stricter, and that is a new outage risk.** With
absence meaning production, `validate_startup_config()` runs on Render whether
or not `APP_ENV` is set, and it **raises `ConfigError` on a partial Square
configuration**. If production currently has some but not all of
`SQUARE_ACCESS_TOKEN`, `SQUARE_LOCATION_ID`, `SQUARE_DEVICE_ID`, the container
refuses to boot — a second 51-minute outage with a different variable on the
label.

The Square configuration must be read off the Render dashboard and recorded
**before** deploy: all three present, or all three absent. This cannot be
determined from the repository; reading it is the owner's action.

Because absence is now safe-by-default, verifying `APP_ENV` itself is no longer
an integration blocker. It moves to the deploy gate alongside the Square check.

### Other risks

- **Two overlapping SECRET_KEY guards** after integration. Not a defect — they
  are independent and both required (§5) — but the interaction and the
  operator-visible message must be established (Cut 4).
- **`/readyz` runs `SELECT 1 FROM staff LIMIT 1`** per probe. Harmless at this
  scale; recorded as a decision, not a surprise.
- **`app/config.py` is new and unconditionally imported**; an error inside it
  takes the process down. Under the new policy it is also load-bearing for every
  test run, which the `_env` amendment addresses.
- **Latent FOR UPDATE trap** on `PaymentAttempt`'s joined relationships (§5).
- **Dormant foundations may be misread as delivered mitigations** (§0). The risk
  backlog must keep the Square durability window and local-only refunds open.

### Rollback

Cheaper than Release A. Before any push, abandoning the work is deleting a local
branch; nothing published changes. After a deploy, rollback is a Render redeploy
of the previous image: the migration is additive-only, so the prior code runs
unchanged against the migrated schema — two empty tables and one column it never
reads. The `card_terminal` row keeps `provider='square_terminal'`, which the
prior code also never reads. **No down migration is required, and none should be
written.**

---

## 9. Gates — eight, sequential, none implicit

No gate authorises the next. Each is opened by the owner explicitly, and passing
one is not evidence for another.

```text
G1  DESIGN REVIEW          this document reviewed independently and approved
                           <- we are here

G2  LOCAL INTEGRATION      branch off b0ab8e7; cherry-pick 81d0cd6^..2c2e676;
                           resolve Cuts 1-6 as specified; APP_ENV policy and the
                           tests/_env.py amendment as ONE separate commit on top.
                           Local only.
                           EXIT: git diff b0ab8e7 <tip> -- app/security.py empty

G3  SQLITE SUITES          all SQLite suites + all 12 existing entrypoints green,
                           including tests/test_secret_key.py
                           EXIT: full output captured, no failures

G4  POSTGRESQL SUITES      both PG suites run with PG_TEST_DSN set
                           EXIT: output captured AND the string SKIP absent

G5  DUMP RESTORE           migration run against a restore of the production dump
                           EXIT: §4 before/after queries recorded, counts match,
                           card_terminal verified, second run reports no change

G6  MAIN ADVANCE           owner authorises fast-forwarding main
                           (b0ab8e7 remains an ancestor)

G7  PUSH / DEPLOY          owner authorises the push and the Render auto-deploy
                           it triggers. PRECONDITIONS, owner-read from the
                           dashboard and recorded: Square vars all-present or
                           all-absent; APP_ENV value noted
                           NOTE: G7 is where the F2(b) outage risk lives

G8  POST-DEPLOY SMOKE      recorded, as for Release A
```

G5 does not authorise G6. G6 does not authorise G7. G7's preconditions are read
before it opens, not during.

---

## 10. What this document is not

It is not an authorisation to implement. No branch was created, no commit
cherry-picked, `main` not advanced, nothing pushed or deployed; Render, the
database and production untouched. Floor, Reservations, Schedule, Kitchen B3/B4,
`COOKIE_SECURE`, `DATABASE_URL` and the CRLF debt were not modified.

Findings were computed with read-only commands (`git log`, `git diff`,
`git show`, `git grep`, `git merge-tree --write-tree`). Claims that could **not**
be verified from the repository are the Render environment values in F2(b),
marked as owner-read rather than assumed.

Errors from earlier revisions are corrected in place and left visible. Across
three revisions the recurring pattern in my own work is the same: reasoning
about one layer and asserting a conclusion about another — a clean merge taken
as correct scope, a grep taken as an import graph, a guard fixed without asking
what depended on it. Each was caught by review rather than by me, which is the
argument for the gate structure in §9 rather than for trusting the next draft.
