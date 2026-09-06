# Restaurant App — Current Work

## Purpose

Authoritative execution checkpoint.

Read this file before modifying production code.

Last consolidated: 2026-09-06, after Floor UI + Reservations and Floor spatial
were both integrated and deployed, and the evidence-preservation commit
`8e5ecf6` was independently reviewed. The block below this line, through
"Post-A hardening", is superseded history — kept for provenance, not current
state. Current state is recorded from "### Floor UI + Reservations" onward.

## Repository / Branch Reality

Current line:

```text
main (local)     2 commits ahead of origin/main, not yet pushed:
                   8e5ecf6  test-evidence commit, independently APPROVED
                   + a docs-only checkpoint commit on top of it
origin/main      ac0be60  pushed 2026-09-06; production runs this
production       ac0be60  /healthz HEALTHY; exact deployed SHA not
                          independently confirmable (no Render dashboard/API
                          access from this session)
```

`b0ab8e7` / `7068bb4` / `release/payment-security` / `release/post-a-hardening`
below are the Release A / Release B history that predates Floor UI +
Reservations and Floor spatial — real, but no longer current. Do not treat
anything in this section as present-tense.

```text
main (local)              b0ab8e7  deployed code plus incident correction; local only
origin/main               7068bb4  pushed 2026-08-27; production runs this
release/payment-security HEAD     Release B candidate; code tip f608119; local only
release/post-a-hardening  7068bb4  deployed integration branch, kept for history
release/kitchen-sync      9bd8743  Release A only; superseded, kept for history

045dfd5 was the base at reconstruction time; 9bd8743 was Release A alone and was
origin/main from 08-26 to 08-27. Both are history, not current state.
```

`feat/floor-map` remains the historical Floor/Kitchen development line. It is NOT
merged and NOT approved as a whole: only its Kitchen slice is, and this release
carries that slice alone.

Kitchen source checkpoint on `feat/floor-map`:

```text
13c0fa2  feat(kitchen): close stations B1 B2 and add audit handoff
9bd5b73  fix(kitchen): complete Stage A/B2 hunks left in shared files + docs
fd11ecd  fix(kitchen): include Stage A floor per-station readiness strip
f4dd079  fix(docs): B2.2 status consistency  ← independently re-audited: APPROVED WITH NON-BLOCKING NOTES
```

`a79dd95` ("commit required app/services/reservations dependency") is **excluded**:
its subject is mislabelled — `active_holds()` is called only from `floor_plan()`,
never from Kitchen code, so it is Reservations work, not a Kitchen dependency.

The Kitchen slice is **reconstructed onto `045dfd5`**, not cherry-picked, because
those commits sit on top of ~40 unapproved Floor UI commits. Seven enumerated cut
points separate them. `8a0e349` is a superseded historical HEAD on the old line,
not the current tip of anything.

A later corrective slice (`7225c6e`) fixed a PostgreSQL-only defect that the SQLite
suites cannot detect by construction: both fire routes locked the Order entity,
whose eager relationships become outer joins, and Postgres refuses `FOR UPDATE` on
the nullable side of one. The routes now lock the scalar id and revalidate after
the lock.

Release A is **deployed**, and has since been superseded by the post-A hardening:
`origin/main` is `7068bb4` and production runs it. The reconstruction described in
this section is history, recorded for provenance.

Floor and Reservations work is **entirely absent from this branch**. It remains on
`feat/floor-map` (committed there, plus uncommitted work in that worktree), unapproved
and unmerged. `app/services/reservations.py` does not exist here at all; the Kitchen
router's import of it was removed as part of the reconstruction.

Important:

- payment/security remediation is integrated on the local Release B candidate;
- inspect current `git status`, current branch, and relevant files before any new implementation or integration work.

## Kitchen Status

### Stage A — APPROVED / CLOSED

Includes Station, explicit routing, `OrderItem.station_id` snapshot-at-fire, Unassigned, KDS filtering, inactive-station live-work reachability, Expo/Floor station presentation, importer protections, and filter-before-limit.

Locked invariants include `READY != SERVED`, explicit routing, coursing preservation, unchanged payment gate, and historical routing immutability.

### Stage B1 — APPROVED / CLOSED

Includes PreparationTask foundation, modifier routing fields, multi-station task creation at fire, snapshots, Unassigned, rollup, `fire_seq`, fire-batch uniqueness, conflict-safe/idempotent creation, backfill/readiness, and `fired_items_without_tasks(db)`.

Compatibility remains:

- `OrderItem.station_id`;
- `OrderItem.kitchen_status`;
- tasks do not own SERVED.

### Stage B2 — APPROVED / CLOSED

Design artifact:

```text
KITCHEN_STATIONS_STAGE_B2_v3_DESIGN.md — APPROVED WITH NON-BLOCKING NOTES
```

Implementation status:

```text
B2.1 (task-based KDS reads)     — APPROVED
B2.2 (per-task READY + bridge)  — APPROVED / CLOSED
```

B2.1 and B2.2 were implemented, independently audited, and fixed per findings. The
committed Kitchen checkpoint (`f4dd079`) was then **independently re-audited: APPROVED
WITH NON-BLOCKING NOTES**, so Kitchen Stage B2 / B2.2 are APPROVED / CLOSED.

Non-blocking note: the re-audit reused the four previously-executed Kitchen tests
(`test_stations`, `test_prep_tasks`, `test_kds_task_reads`, `test_kds_task_ready`,
re-run green from a clean tree) and the existing PostgreSQL concurrency evidence — no
new test execution was required.

Current B2 implementation includes:

- task-based KDS station reads from snapshotted `PreparationTask.station_id`;
- one `OrderItem` sale line with task/station subrows;
- task-based station-work badge counts;
- hard no-hybrid activation gate;
- per-task READY;
- parent Order lock + post-lock task/sibling refresh;
- `OrderItem.kitchen_status` rollup;
- legacy kitchen-status compatibility bridge;
- all-or-nothing validation for task-backed legacy writes;
- explicit destructive `Un-ready (all stations)` behavior;
- `READY != SERVED`;
- payment-gate semantics unchanged.

Activation gate:

```text
B2_2_ACTIVE
AND kitchen_b2_active
AND fired_items_without_tasks(db) == 0
```

B2.2 PostgreSQL concurrency evidence:

```text
KITCHEN_STATIONS_STAGE_B2_2_POSTGRES_CONCURRENCY_EVIDENCE.md
PASS
```

The proof used real PostgreSQL 16 with two independent sessions. Session B blocked on the parent Order `FOR UPDATE`, then observed Session A's committed READY state after the lock and completed the correct final rollup.

### Kitchen work NOT authorized now

- B3/B4;
- Expo/Floor task-read migration beyond the approved compatibility behavior;
- task-owned SERVED/reporting;
- remake/re-fire model;
- `OrderItem.station_id` cleanup/removal;
- richer task states;
- WebSocket/SSE.

## Payment / Security — Release B candidate passed G1-G5

Integration branch:

```text
release/payment-security  (code tip f608119; this checkpoint commit on top)
```

Status:

```text
Stage 1  — IN CANDIDATE; G1-G5 PASSED
Stage 2a — IN CANDIDATE; G1-G5 PASSED
Stage 2b — IN CANDIDATE; G1-G5 PASSED
Stage 2c — EXCLUDED / WIP
```

The candidate code is 29 linear commits over `b0ab8e7`: 26 approved source commits,
the APP_ENV and `.env.example` integration amendments, and the Windows timezone
fix `f608119`. `app/security.py` remains byte-for-byte identical to `b0ab8e7`.

G3 passed all required SQLite entrypoints after the timezone correction. G4
independently passed both PostgreSQL suites with zero SKIP. G5 restored the
retained PostgreSQL 18.6 production dump locally: financial row counts stayed
unchanged, `card_terminal` became `square_terminal`, both new attempt tables were
empty, and the second strict migration run returned `[]`.

Stage 1/2a/2b are still dormant foundations: no live router invokes the new
attempt/provider/refund services. The Square durability window and external
refund wiring remain open until a separately approved later stage.

Stage 2c is not part of the approved baseline.

## Current Payment Reality on Floor/Kitchen Line

Current line uses Payment + PaymentAllocation seat ledger, direct Square Terminal flow, and local Refund rows.

Current risks include:

- processor success can precede durable local Payment persistence;
- local card Refund does not execute Square refund;
- full processor reconciliation is absent;
- production critical config can fail open.

Do not fix these incidentally during unrelated work.

## Future Payment/Security Integration — NOT AUTHORIZED

A controlled integration slice is required to reconcile approved Payment/Security Stage 1/2a/2b with the current Floor/Kitchen line.

Do not:

- merge `fix/p0-security-and-payments` wholesale;
- integrate Stage 2c;
- integrate while working-tree state is unresolved;
- combine payment integration with unrelated feature work.

## Current Risk / Evidence Backlog

### High
- `SECRET_KEY` fallback — CLOSED 2026-08-27 11:19. See the incident below: the
  earlier claim that this was mitigated on 08-26 was false, and production signed
  session cookies with the public source-code key until the deploy of `7068bb4`
  forced the misconfiguration into the open;
- Square charge durability window;
- card refunds are local-only.

### Medium / unresolved
- incomplete processor identity/reconciliation on current line;
- table-open concurrency;
- reservation overlap control not confirmed;
- narrow financial/config audit trail;
- `test_admin.py` failing in the inspected working tree while backend authz cases passed;
- production `DATABASE_URL`/cookie-security defaults.

### Kitchen evidence notes
- SQLite functional/regression tests passed for B2 implementation packages;
- real PostgreSQL B2.2 concurrency proof passed;
- the B2.2 proof was a focused contract proof, not a production-system certification;
- startup warning literal remained inspection-based rather than a dedicated runtime test;
- no production execution was performed.

### Deferred Kitchen backlog
- normalized Station-name DB uniqueness;
- duplicate routing-import IDs;
- CSV formula-injection hardening;
- Expo/Floor task migration;
- modifier-routing UI;
- `OrderItem.station_id` removal;
- task SERVED/reporting;
- remake/re-fire;
- richer task states;
- WebSocket/SSE.

### Other evidence gaps
- no production execution/data mutation;
- no live provider execution in fact audit;
- no load benchmark;
- no real-device/browser validation;
- no restore-drill evidence.

## Multi-Agent Development Model

Claude and Codex are both eligible developers. Every active implementation slice must
identify exactly one implementer and an independent reviewer. Default cross-review:
Claude implements → Codex reviews; Codex implements → Claude reviews. The implementer
may not independently approve the same slice. (Full rules: `docs/05_AI_HANDOFF.md`;
policy: ADR-028.)

### Domain preference (not feature authorization)

```text
Claude — Kitchen / KDS / Expo; core Order lifecycle; Payment/Security when separately authorized
Codex  — Schedule / Reservations; other self-contained modules when explicitly authorized
```

### Governance state

```text
Governance agent: NOT YET ASSIGNED
Human authority: repository owner
Technical enforcement: GitHub/CI governance gates NOT YET CONFIGURED
```

Do not claim autonomous governance enforcement is active until those mechanisms are
actually configured.

### Parallel-work rule

Before parallel implementation begins, each slice must record: module/slice,
implementer, reviewer, branch/worktree, authorized scope, out-of-scope boundaries,
evidence requirements. Use separate branches/worktrees whenever practical.

### Shared core

No implicit implementation authorization exists for shared-core / cross-domain changes
(Order/OrderItem lifecycle, Payments/Refunds, auth/permissions, shared migration
architecture, DayClose/timezone, reporting contracts, Kitchen cross-domain behavior,
provider integrations). These require explicit design or scope expansion first.

### Schedule / Reservations

Codex is the preferred implementer for a future Schedule / Reservations slice. This
preference does **not** authorize implementation yet. Before implementation, the
authorized slice must define its branch/worktree and confirm whether the work touches
shared table, Order, BusinessDay, permissions, migration, payment, or timezone behavior.

### Current checkpoint (unchanged)

The Kitchen closure recorded above (Stage A / B1 / B2 and B2.2 = APPROVED / CLOSED;
B3/B4 = DEFERRED / NOT AUTHORIZED) and the Payment/Security statuses remain unchanged.

### Floor UI + Reservations (table-hold model) — CLOSED / INTEGRATED / DEPLOYED

```text
Decision: picker/mapa (floor-map); cards stayed out of scope
Implementer: Codex
Reviewer: Claude
Branch/worktree: feat/floor-reservations-cut, from 489f6d2
Commit: ddd5add6d564ca6ea90bbd29422dc22a7e620aaf
```

Delivered: `reservation_table` (N:N), Zone rectangle fields, `RestaurantTable.shape`,
the reconciled `app/services/reservations.py` (availability engine + hold-settings),
lock + `calculate_availability` wired into `add_reservation`/`seat_reservation_here`,
holds surfaced on the live Floor plan, four reservation settings defaults.

Independent review found one real concurrency bug (stale identity-map read in
`seat_reservation_here` after the `FOR UPDATE` lock) — fixed, re-verified against
real Postgres, re-reviewed, approved. Evidence: `test_reservations_map.py` 16/16;
`pg_reservation_overlap_race_proof.py` and `pg_seat_here_race_proof.py` 2/2 each
against a disposable Postgres; `test_pg_migration.py` 26/26; fresh bootstrap proven
equal to upgrade from `489f6d2` for the 5 new columns; full standalone regression
green except the pre-existing, unrelated `test_e2e.py` (needs a live server).

Fast-forwarded `origin/main` from `489f6d2` to `ddd5add` and pushed. `/healthz`
HEALTHY post-deploy. SHA served by Render not independently confirmed (no
dashboard/API access from this session) — inferred from the fail-closed boot
guard plus `origin/main` state, not observed directly.

Known residual risks, not fixed here, not blocking:

```text
seat-here + multi-table reservation   if only part of the held tables survive
                                       revalidation, the code seats the full
                                       party across whatever remains, without
                                       checking aggregate capacity
walk-in override on a reserved table  orphans the original reservation
                                       (stays WAITING, points at an occupied
                                       table); no product decision made yet
open_order_on_table                   still has no row lock or DB constraint;
                                       pre-existing, confirmed exploitable via
                                       a real-Postgres proof, out of scope here
```

Out of scope, confirmed unaffected: Stage 2c, B3/B4, reopening B2,
Strategy/PostHog/.github/menu_stations_export.csv/framework-experiments variants.

### DEV inventory vs `ddd5add` (read-only, recorded)

A full inventory of `feat/floor-map`, `feat/reservation-availability`,
`feat/reservation-hold-settings`, `feat/reservation-ui-1`, and the uncommitted
diff in the `restaurant_app` worktree, against `ddd5add`, found:

- Kitchen (Stage A/B1/B2/B2.2): fully integrated, nothing outstanding.
- Reservations (availability, hold-settings): fully integrated; diff against the
  source branches is naming/docstring only, logic identical.
- Floor spatial (zones-as-rectangles, drag, auto-relayout, shapes, waiter colour,
  per-zone waiter tool): **not integrated** — committed only on `feat/floor-map`,
  on a base older than `045dfd5`.
- `restaurant_app`'s uncommitted diff (7 files, 634 lines): a superseded, earlier
  draft of the same reservation picker/holds (`add_reservation`/`seat_reservation_here`
  without the lock `ddd5add` has) — archived, not ported — plus a `floor.html`/
  `reservations.html`/`app.css`/`admin_settings.html` top layer with real,
  not-yet-integrated content: reserved/due-soon status on the live Floor (depends
  on the Floor-spatial backend below), a two-column reservation-picker layout, and
  independent hold-window settings form fields. `admin_settings.html`'s form
  fields were flagged as portable on their own; the rest requires the Floor
  spatial slice first.
- `release/kitchen-sync`: obsolete, superseded, kept for history only.
- No Schedule branch/commit exists — only planning language in this file.

### Floor spatial (zones, drag, relayout, shapes, colour) — CLOSED / INTEGRATED / DEPLOYED

```text
Product decision: soft-retire stays the only table-removal path; the
                   hard-delete endpoint on feat/floor-map was NOT ported
Implementer: Codex
Reviewer: Claude
Branch/worktree: feat/floor-spatial-cut, from ddd5add
Commit: ac0be6079b692eddc34a4ac5e83de4270f160fec
```

Delivered, additively onto `ddd5add`'s existing grid-based admin pages — the
free-placement/unified-page redesign on `feat/floor-map` was explicitly NOT
ported: zones as editable rectangles (`Zone.pos_x/pos_y/width/height`, already
added in the Reservations slice; edited via `edit_zone`), table drag +
shape via `save_layout` (backward-compatible 3-or-4-field payload),
`set_zone_tables` (declarative zone sizing), `set_staff_color`/`Staff.color`/
`Staff.swatch`, `floor_plan()` card data (`disp_status`, `waiter_color`,
`zone_groups`, `reservation`, `attended_at`), reserved/due-soon status on the
live Floor, and the reservation-picker grouped into the same zone panels as
the Floor page. New columns `staff.color`, `order.attended_at`.

Independent review found one real blocker before approval: `set_zone_tables`
could soft-retire a table that was `occupied` with a live order, orphaning
that order from the visible Floor. Fixed — retirement now only ever touches a
table that is both `status == FREE` and has no live order (`_live_order_for_table`,
the same guard `edit_table`/bulk-retire already use); if the reduction would
require touching an occupied table, the whole call is rejected and nothing
changes; capacity likewise only ever changes on a free table. Re-verified,
re-reviewed, approved.

Evidence at approval time: `test_floor_spatial.py` 36/36 (incl. the
occupied-table rejection case); `test_reservations_map.py` 16/16 (re-verified
against the Floor-spatial changes, not just the Reservations slice alone);
`pg_reservation_overlap_race_proof.py` and `pg_seat_here_race_proof.py` 2/2
each against a disposable Postgres; fresh bootstrap proven equal to upgrade
for the 2 new columns, on both SQLite (this fix also corrected a real,
previously-latent bug: the SQLite branch of `migrate.run()` did `PRAGMA
table_info(order)` / `ALTER TABLE order ADD COLUMN` unquoted — `order` is a
SQL reserved word; fixed to `"order"` in both statements) and Postgres; full
standalone regression green except the pre-existing `test_e2e.py`.

Fast-forwarded `origin/main` from `ddd5add` to `ac0be60` and pushed.
`/healthz` HEALTHY post-deploy; SHA served not independently confirmed, same
caveat as the Reservations deploy.

Out of scope, confirmed unaffected: hard-delete of a table, reservation cards,
Stage 2c, B3/B4, Payment, the multi-table partial-availability risk, and the
`open_order_on_table` lock.

### Evidence-preservation commit — `8e5ecf6` — APPROVED, not yet pushed

```text
Author: enave
Branch: feat/floor-spatial-cut, one commit ahead of ac0be60
Commit: 8e5ecf6b8ba0aa44c5485f8ba0512f66cf2c0539
```

Adds, as tracked files for the first time, the required evidence for both
Floor slices — previously excluded from every integration commit on request,
which meant none of it survived in `origin/main`'s history and would be lost
if the working worktrees were ever discarded:
`tests/test_reservations_map.py`, `tests/test_floor_spatial.py`,
`tests/pg_reservation_overlap_race_proof.py`,
`tests/pg_seat_here_race_proof.py`, `tests/pg_table_open_race_proof.py`.
Test-only — zero lines of application code. Independently reviewed: confirmed
the two stale assertions in `test_reservations_map.py` (checking CSS class
names the Floor-spatial picker redesign had already renamed) were genuinely
corrected, not just committed as-is; all 5 files re-run clean against the
current code. **Verdict: APPROVED.**

Local `main` has been fast-forwarded to this commit. It has not been pushed
to `origin/main` — that is a separate, still-pending authorization.

## Release A — integration history (now DEPLOYED)

Kitchen Stage A / B1 / B2 / B2.2 was reconstructed onto `045dfd5` per the
owner-authorized Rev 3 integration design, then fast-forwarded into local `main`.
Both commits are preserved — no squash:

```text
7225c6e  fix(kitchen): take the Order fire lock on the scalar id and decide post-lock
97bed73  feat(kitchen): Release A — Stage A/B1/B2/B2.2 reconstructed onto 045dfd5
045dfd5  (base — origin/main)
```

Each was independently reviewed by Codex: `APPROVED WITH NON-BLOCKING NOTES`.
The fire fix was additionally reproduced by the reviewer in its own PostgreSQL
database.

```text
STATE AT THE TIME OF INTEGRATION — superseded, kept for provenance:
  local main   = 7225c6e     ahead of origin/main by 2 commits
  origin/main  = 045dfd5     the base; nothing pushed yet at that point
  production   = not yet updated

CURRENT STATE: origin/main = 7068bb4, deployed 2026-08-27, production healthy.
```

Schema delta introduced by the release, and nothing else:

```text
4 columns   station_id on menu_item, order_item, modifier, modifier_option
            (INTEGER REFERENCES station(id), nullable)
3 tables    station, preparation_task, preparation_task_modifier
1 index     uq_prep_task_batch (order_item_id, fire_seq, COALESCE(station_id,-1))
0           Floor or Reservations tables/columns
```

`_pg_boolean_ddl` from the `045dfd5` base is preserved unchanged.

### Verification carried by this checkpoint

```text
SQLite       11 suites PASS
PostgreSQL   boolean-default translation, fresh bootstrap, index by catalog
             definition, pre-release upgrade, migration idempotence,
             fresh==upgrade convergence, B2.2 two-session concurrency,
             backfill with real fired data incl. a contended race
Fire routes  both routes proven on PostgreSQL, with real lock contention
UI smoke     T3.5 complete, 40/40 PASS against a PostgreSQL database upgraded
             from a pre-release schema
```

Pre-existing and unrelated: `test_security`, `test_admin` and `test_schedule`
fail for want of a `waiter` fixture — identically on the untouched `045dfd5`
base. Not a regression of this release.

### Release A is DEPLOYED

Release A shipped as `9bd8743`, pushed 2026-08-26, live at 20:11:07 PDT. It was
superseded on 08-27 by `7068bb4`, which is what production runs now — see the
post-A hardening section. The evidence below is Release A's own, and stands.

Post-deploy smoke, approved: both fire paths returned 303 rather than the 500 they
gave on PostgreSQL before `7225c6e`; re-sends were rejected with 400; CSV routing
applied with the unrouted item left Unassigned; the KDS station filter
discriminated (Grill 1 ticket, Bar 0, Unassigned 8); Expo aggregated; the floor
card showed its per-station strip; READY and SERVED stayed distinct transitions;
twelve pages and sixteen open orders returned no 5xx.

Test data was left in production deliberately: stations `SMOKE Grill` / `SMOKE
Bar`, two routed menu items, and orders 17 and 18 (18 partially fired, which is
the state the coursing check needs). Removing it would be a separate authorised
operation.

### Gates closed before the push

```text
deployed SHA   read from the Render dashboard, and corroborated by behaviour:
               /expo and /admin/stations answered 404 on 045dfd5
backup         pg_dump of production restored into a disposable PostgreSQL 18.6;
               exact COUNT(*) identical across all 50 tables; the release
               migration then ran on that restore over real production data,
               backfilling 33 PreparationTasks with no data loss
credentials    Neon role and Owner PIN both rotated, each revocation proven by
               a failed authentication attempt. The SECRET_KEY part of that
               rotation FAILED SILENTLY and was believed successful for a day —
               see the incident section below
```

## Post-A hardening — DEPLOYED as `7068bb4`

Both slices reached production on 2026-08-27. `release/post-a-hardening` was a
linear cherry-pick onto `9bd8743`, no squash and no merge commit; `main` was
fast-forwarded to it, pushed, and the Render auto-deploy carried it live.

```text
main = origin/main = release/post-a-hardening = production = 7068bb4
```

The deploy did not go cleanly: the SECRET_KEY guard refused to boot because no
correctly-named variable existed, and the service was down for 51 minutes. See the
incident section.

```text
integrated and deployed          provenance (original commits)
45a7942  docs(deploy)            8cd7b2f  on docs/deploy-reality
7e1b812  fix(security)           feff3b8  on fix/secret-key-fail-closed
54f7a67  docs(governance)        793f45f  on docs/deploy-reality
```

The SHAs differ because cherry-pick rewrites them; the originals are kept on their
branches so provenance stays checkable.

Verified on the integration branch itself, not only on the source slices: the 17-case
SECRET_KEY matrix, the 12 SQLite suites, and both PostgreSQL proofs. The
accumulated diff against `9bd8743` is 20 files, and equals the two slices with no
overlap — 3 documentation files and 17 security files.

```text
8cd7b2f  docs/deploy-reality
         docs(deploy): record Neon production reality and secret-key incident
         RENDER.md now says what it is — a from-scratch guide, not a description
         of the live system — names Neon as the production database, points at
         DATABASE_URL on the Environment tab as the only authority, and documents
         the Internal vs External split that sent one backup to an empty database.
         PROJECT_ARCHITECTURE_FACTS_FOR_DOCS.md promotes two INFERRED facts to
         CONFIRMED and records that the SECRET_KEY fail-open was live in
         production, not theoretical.

feff3b8  fix/secret-key-fail-closed
         fix(security): fail closed when the session secret is unsafe
         SECRET_KEY is required; the public development key is reachable only
         behind ALLOW_INSECURE_DEV_SECRET=1, and only that literal. A key that is
         present but empty, whitespace-padded, equal to the public value or under
         32 UTF-8 bytes is rejected in every environment. The key is returned
         verbatim, never trimmed. Validation lives in _secret() and is invoked at
         module import, so a misconfigured deployment dies at startup instead of
         on the first login. Twelve test entrypoints reach app.security and each
         imports tests/_env.py; docker-compose.yml gains env_file so the
         documented Compose flow still boots.
         Verified: 17-case matrix in subprocesses, 12 SQLite suites, both
         PostgreSQL proofs, and a real Compose boot in three shapes.
```

**Superseded 2026-08-27 — kept as evidence of how the check failed.** The text
below stood here as the deploy pre-condition:

> The production `SECRET_KEY` was measured at 62 bytes, above the 32-byte
> minimum, so the hardening slice will not refuse the deployment when it
> eventually ships.

The measurement was accurate and the conclusion was wrong. Those 62 bytes belonged
to **`SECRET_KEU`** — an inert variable the application never read, because the code
reads `SECRET_KEY`. Measuring it validated nothing about the variable that matters,
and could not support the conclusion drawn from it: the deploy *was* refused, and
the service stayed down for 51 minutes.

The pre-condition was proven only on **2026-08-27 at 11:19**, when `7068bb4`
completed its boot with a correctly-named `SECRET_KEY`. A finished boot is the
proof, because `_secret()` runs on the import path; a byte count read off a
dashboard is not, unless the name is verified first.

Re-check before any future deploy that changes the variable — and check the NAME
before the length.

**Superseded 2026-08-27.** This paragraph claimed the operational exposure was
mitigated by a correctly-named variable. It was not: no correctly-named variable
existed. `7068bb4` is deployed, the guard is live, and the exposure closed only at
11:19 on 08-27. See the incident section.

## Incident 2026-08-27 — SECRET_KEY was never read; two rotations failed silently

Production signed session cookies with `_DEV_SECRET`, the key printed in this
repository, from before 2026-08-26 until **2026-08-27 11:19**. Anyone holding a
checkout could forge a session cookie and arrive as the owner without a PIN.

Three variables existed on the Render service. None had the name the code reads:

```text
Security_Key   the original      never read
SECRET_KEU     the 08-26 "fix"   never read — a typo, Y written as U
SECRET_KEY     2026-08-27 11:19  the first one actually read
```

Environment variable names are case- and character-sensitive. `os.environ.get`
returned `None` every time, and the pre-`7068bb4` code answered that with a silent
fallback to the public key. Nothing failed, nothing logged.

### Why the 08-26 rotation was believed to have worked

After creating what was thought to be `SECRET_KEY`, the deployment was validated by
`/healthz`, a login, and the staff list rendering on `/login`. **None of those
touch the signing key in a way that distinguishes a real secret from the public
one.** The login succeeded because the PIN hash verifies independently of
`SECRET_KEY`; the staff list is an ordinary `SELECT`. The checks passed, and would
have passed identically with no key at all.

That is the whole failure: the verification could not observe what it claimed to
verify. The checkpoint and commits `54f7a67` and `feff3b8` state the exposure was
mitigated on 08-26. That statement was false when written.

### The outage, and what it bought

```text
10:26   7068bb4 pushed; Render auto-deploy starts
10:28:09  last healthy response from the old instance
10:28:40  502 — the new instance boots, _secret() raises, the worker dies
          Gunicorn exits 3, "Worker failed to boot", and retries in a loop
11:19:44  SECRET_KEY corrected on Render; the instance boots and serves
```

51 minutes unavailable. The deploy did not introduce a defect — it made an existing
one impossible to ignore. `7e1b812` calls `_secret()` at module import, so a
misconfiguration takes the process down instead of quietly weakening every session.

### What a healthy boot now proves

Before `7068bb4`, `/healthz` answering 200 said nothing about configuration: the app
booted identically on the public key. Now the guard runs on the import path, so a
worker that finishes booting has necessarily passed all five rules — present, not
empty, not whitespace-padded, not the public value, at least 32 UTF-8 bytes. The
evidence is the code path, not an inference from behaviour.

### Not authorized

`Security_Key` and `SECRET_KEU` both remain on the Render service. Both are inert:
nothing reads either name. **Removing them is not authorized** and is recorded here
so that a future reader does not mistake their presence for configuration in use.

## Known debt, not fixed

```text
CRLF / .gitattributes
  core.autocrlf=true converts docker-entrypoint.sh on Windows checkout, and the
  container's sh rejects `set -e\r` with "Illegal option -". The documented
  `cp .env.example .env && docker compose up` flow is broken on such a checkout.
  PRE-EXISTING — reproduced on untouched 9bd8743, not introduced by either slice.
  Fix is `.gitattributes` with `*.sh text eol=lf`, as its own slice.
```

Further debt — the `_DEV_SECRET` fail-open now closed by `feff3b8`, `COOKIE_SECURE`
and the `DATABASE_URL` SQLite fallback still open, the inert `Security_Key`
variable, the empty Render Postgres instance with an exposed credential — is
recorded in the Release A checkpoint package.

## Documentation reconciliation — IN PROGRESS, PENDING INDEPENDENT REVIEW

```text
Source: audit of 5 orphaned docs-only branches (docs/portability-gate @ 2a617b5
        and its ancestors docs/exp-000-portability, docs/exp-000-closeout; plus
        the separately-superseded docs/exp-000-evidence and docs/deploy-reality)
Branch/worktree: docs/portability-reconciliation, from a984172
Implementer: Claude
Reviewer: pending assignment
```

`2a617b5` and its two ancestors were never merged into `main` — they fell off
during the Reservations fast-forward (`ddd5add` was cut from `489f6d2`
directly). `AGENTS.md`, `docs/00_PROJECT_CONTEXT.md`, `docs/01_ARCHITECTURE.md`,
`docs/02_DECISIONS.md`, `docs/05_AI_HANDOFF.md`, both payment MOCs, and
`PAYMENT_BRANCH_RECONCILIATION.md` had not been touched on `main` since
`489f6d2` either, so `2a617b5`'s corrections to them (mainly: "approved but
unmerged" → "deployed but dormant" now that Release B has shipped; ADR-029;
the `AGENTS.md` Recovery/Portability-Gate sections; MOCs no longer duplicating
a stage-status snapshot `03_CURRENT_WORK.md` already owns) applied as direct,
conflict-free substitutions. Three new evidence files
(`docs/Evidence/Payments/md/*.md`, `docs/framework-experiments/EXP-000_...md`)
were added verbatim — pure additions, nothing to reconcile.

This file is deliberately NOT replaced by `2a617b5`'s version of it — that
version predates Floor UI + Reservations, Floor spatial, and everything
recorded above; this file already supersedes it. Only this note was added.

`docs/exp-000-portability`, `docs/exp-000-closeout`, `docs/exp-000-evidence`,
and `docs/deploy-reality` contributed nothing beyond what `2a617b5` already
carries or what `main` already has via a different cherry-picked path — see
the audit for detail. Not integrated as separate branches; safe to delete once
this reconciliation is approved, not before.

Awaiting independent review before commit.

## Next Authorized Action

```text
NONE implemented and pending. Two administrative decisions are open:
  1. push local main (2 commits ahead: the evidence-preservation commit,
     independently APPROVED, plus a docs-only checkpoint on top) to
     origin/main;
  2. decide the fate of the 5 pre-existing docs-only commits (topmost
     2a617b5) that fell off main's line during the Reservations
     fast-forward — preserved via reflog, not reintegrated.
```

G1-G8 are complete. Release B (Payment/Security Stage 1/2a/2b), Floor UI +
Reservations, and Floor spatial are all DEPLOYED — `origin/main` and
production are `ac0be60`; local `main` is 2 commits ahead, not yet pushed;
production `/healthz` is HEALTHY.

Residual risks carried forward, not fixed, not blocking, pending their own
authorization: multi-table partial availability in `seat_reservation_here`;
walk-in override orphaning a reservation; `open_order_on_table` has no row
lock (proven exploitable on real Postgres); `set_zone_tables` has no row lock
against a concurrent seat (narrow window, admin action, lower priority than
the other three).

Not authorized: NEW pushes beyond what a future explicit authorization covers,
NEW deployments, production mutation, Render configuration changes, removal of
the inert `Security_Key` / `SECRET_KEU` variables, cleanup of the smoke data,
fixing any residual risk above without its own authorization, or reservation
cards.

Do not start Stage 2c, B3/B4, reopen B2, or touch `open_order_on_table`'s or
`set_zone_tables`'s locking without a new authorized slice.
