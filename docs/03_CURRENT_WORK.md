# Restaurant App — Current Work

## Purpose

Authoritative execution checkpoint.

Read this file before modifying production code.

Last consolidated: 2026-09-06, after Floor UI + Reservations, Floor spatial,
test-evidence preservation, and documentation portability reconciliation were
integrated and pushed. `Repository / Branch Reality` below is the current baseline
policy. Later sections explicitly labeled as history or superseded are kept only
for provenance; current slice status begins at "### Floor UI + Reservations".

## Repository / Branch Reality

Current line:

```text
canonical base   latest fetched origin/main, unless an explicitly authorized
                 slice records a different exact checkpoint
main / origin    synchronized at the last verification; resolve and freeze the
                 exact SHA at task start with `git fetch origin main` followed
                 by `git rev-parse origin/main`
production       Render visually confirmed `c18b9e03c264129f019341deddcad31e68b1cc05`
                 Live via Auto-Deploy on 2026-09-07 02:55:19 PDT (green/
                 concluded indicator). Neon branch `production`, database
                 `neondb`. Post-deploy DB-reality check: map_x_exists=true,
                 map_y_exists=true, invalid_positions=0, orphaned_zones=0.
                 This supersedes the prior `f6bd4ef` confirmation below —
                 slice 1 (model/migration/backfill) is now live; slice 2
                 (admin visual builder, this worktree) is NOT part of this
                 deploy — see "Implementation slice 2" status.
```

Do not copy the last observed SHA into future prompts as a permanent baseline.
Each new slice must resolve `origin/main`, record that exact SHA as its base, and
then pass that literal SHA—not the moving `origin/main` ref—to `git worktree add`.
Work only in the resulting isolated branch/worktree from that frozen checkpoint.

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

### Evidence-preservation commit — `8e5ecf6` — APPROVED / INTEGRATED / PUSHED

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

This commit was fast-forwarded into local `main` and pushed to `origin/main`;
it remains in the ancestry of the current `c39856a` checkpoint.

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

## Documentation reconciliation — CLOSED / INTEGRATED / DEPLOYED

```text
Source: audit of 5 orphaned docs-only branches (docs/portability-gate @ 2a617b5
        and its ancestors docs/exp-000-portability, docs/exp-000-closeout; plus
        the separately-superseded docs/exp-000-evidence and docs/deploy-reality)
Branch/worktree: docs/portability-reconciliation, from a984172
Implementer: Claude
Reviewer: independent reviewer session
Commit: c39856a88199092ed7ed9420a8b60289b29fe74f
Verdict: APPROVED — VERDICT UNCHANGED
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
the audit for detail. They were not integrated as separate branches. Their
useful content is now preserved on `main`, so the five historical branches are
superseded and eligible for later removal with explicit authorization.

The first independent review returned `FIX REQUIRED` for two stale current-state
claims inherited from the old common base. The implementer corrected only those
claims; re-review returned `APPROVED`. A final read-only check confirmed that the
three trailing-whitespace findings in `STAGE2C_INTEGRATION_DESIGN.md` are exactly
two intentional Markdown line-break spaces per line, byte-identical to `2a617b5`;
the verdict remained approved. The commit was fast-forwarded into `main`, pushed,
and `/healthz` returned HTTP 200 `ok`. The exact SHA served by Render was not
directly observable without dashboard/API access.

## `feat/floor-map` branch inventory — HISTORICAL, READ-ONLY AUDIT

```text
Branch: feat/floor-map @ 5d8eee4 (worktree: restaurant_app)
Never merged into main. Predates Release A/B and the current Floor/Kitchen line.
Audited read-only, feature-by-feature against main @ f6bd4ef
(merge-base 1756830a), to settle whether anything on it is worth recovering
before it's considered for deletion. No code changed by this audit.
```

**Exclusive, recoverable value — three items, none in `main`:**

1. The free-placement admin Floor/Zone builder in `admin_tables.html` / its
   `admin.py` routes — drag-to-place tables and zones (per-mille positioning,
   not the grid `main` uses), zone-rectangle resize, table shape cycling
   (round/square/long), and one unified floors+zones+tables+map page. `main`'s
   Floor spatial (`ac0be60`) was built additively onto the old grid-based
   admin pages and explicitly did not port this redesign.
2. On the *operational* Floor page itself (`floor.html`), a Map/List toggle
   (`viewToggle`, persisted in `localStorage['fp-view']`) and a manager-only
   "✏️ Arrange" mode that drags tables and drags/resizes zones directly on the
   live floor, auto-saving as it goes (`arrangeToggle`, `floor.html:78-80` and
   `:592-745` on this branch). `grep` for `arrange`/`viewToggle` in `main`'s
   `floor.html` returns nothing — this mode does not exist there at all.
3. The visual staff-color picker in `admin_staff.html` (a `type="color"` input
   per staff row, auto-saving on change). `main` has the backend route
   (`set_staff_color`, `admin.py:1236`) but **no template calls it** —
   confirmed by grepping every `web/templates/*.html` in `main` for
   `color`/`swatch` tied to staff. The capability is backend-only and
   effectively dead without a UI; it is not functionally equivalent to this
   branch's picker.

**Already ported to `main`, content-identical (CRLF aside, verified with
`diff -b -B`):** Kitchen Stations + Expo (`admin_stations.html`, `expo.html`,
`kitchen.html`). The reservations-on-floor UI (`zone_groups`, "due-soon" pill,
seat-here action) existed on this branch first and was reconstructed onto
`main` with minor copy changes.

**Predates the branch split, identical on both lines:** Happy Hour
(`services/happyhour.py`), Day menus (`services/daymenu.py`), Staff
scheduling/Positions (`schedule.py`, `Position`) — untouched by either line
since the common ancestor.

**Not on this branch at all — `main` is strictly ahead:** the whole
Payment/Security Release B (`app/config.py`, `payment_attempts.py`,
`payment_providers.py`, `refund_attempts.py`, fail-closed `security.py`), and
the Reservations availability engine (`calculate_availability`, overlap/expiry
checks — this branch has only `active_holds()`).

Conclusion: `feat/floor-map` is safe to treat as superseded except for the
three items above. If they're ever wanted, port only those — the admin
builder (`admin_tables.html` + its `admin.py` routes), the operational
Map/List + Arrange mode (`floor.html`), and the staff-color picker
(`admin_staff.html`) — onto the current base. Never bring the branch tip
across wholesale: everything else on it (Reservations, Stations, Kitchen,
Happy Hour, Day menus, Positions) is already equal or behind what `main`
has, and it entirely lacks Payment/Security Release B and the Reservations
availability engine.

## Floor Plan Builder — design slice — DESIGN CLOSED / APPROVED, IMPLEMENTATION NOT AUTHORIZED

```text
Branch/worktree: design/floor-plan-builder, from 99e2490 (literal SHA, fetched
                  and captured per the immutable-baseline procedure)
Implementer: Claude
Reviewer: four independent reviews. First three each returned DESIGN
           REVISION REQUIRED, each corrected by the author. Fourth returned
           APPROVED WITH NON-BLOCKING NOTES; both notes incorporated.
Design artifact: docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md
Design status: CLOSED / APPROVED.
Implementation status: NOT AUTHORIZED. Design approval is not
                        implementation authorization.
```

**Design closed.** This was the active design flow through three
correction cycles; it is now approved and closed. No implementation slice
is open under this checkpoint yet — see "Next Authorized Action" below for
the proposed next step, which still requires its own explicit
authorization.

First independent review found three real gaps in §4 (Migration and data):
the backfill's PostgreSQL call site was missing (columns would have shipped
NULL forever in production while working fine in SQLite/dev), a resilience
property was attributed to the wrong precedent function, and the Y-axis
formula referenced an undefined term. The author corrected all three plus
the two NULL-fallback/eligibility points the same review raised as open
-question recommendations.

Second independent review, on the corrected document, again returned
`DESIGN REVISION REQUIRED`, on four points: the backfill's fail-closed
guarantee was still a manual/CI step outside the transaction rather than
code the migration itself enforces, and its algorithm/verification could
disagree on which rows needed a position; shape's persistence still pointed
at a write path (the old grid endpoint's DOM-scraping JS) that has nothing
left to read once the grid leaves the page; a zone move/resize and its
tables' repositioning were two independent, non-atomic requests; plus
smaller documentation inconsistencies (this entry and the design artifact's
own "Confirmation nothing was implemented" text being stale, and an
imprecise claim about newly-added tables). The author has corrected all four
in the design artifact (§4.2's formal `ELIGIBLE` definition and
in-transaction check; §6 restructured into §6.1/§6.2/§6.3) — see the design
artifact's "Revision note (second pass)" for the full account.

Third independent review, on the second-pass document, again returned
`DESIGN REVISION REQUIRED`, on two points: the zone-move endpoint's payload
validation checked that every table it named belonged to the zone, but
never checked that it named *every* table the zone actually has — an
incomplete payload (one table silently omitted) passed and would have left
that table visually detached from its own zone; and `ELIGIBLE` was defined
in prose ("active, non-NULL zone_id") without being formally identical to
the `INNER JOIN` the rest of the migration section described, leaving room
for an orphaned `zone_id` (pointing at a since-nonexistent `Zone`) to be
quietly excluded by the join with no integrity signal, rather than being
treated as the data-integrity fault it is. The author has corrected both in
the design artifact — §6.2 now requires an exact-set match
(`received_ids == current_active_table_ids_for_zone`) before any write;
§4.2 restates `ELIGIBLE(table, zone)` as the literal `INNER JOIN` relation,
used verbatim throughout §4, plus a new step-0 check that raises on an
orphaned reference. See the design artifact's "Revision note (third pass)"
for the full account.

Fourth independent review, on the third-pass document, returned `APPROVED
WITH NON-BLOCKING NOTES` — two notes, both incorporated before closing:
(1) the PostgreSQL half of the orphaned-zone-reference test must not
depend on disabling or bypassing constraint enforcement on that database,
since a disposable instance may not have the privileges that requires —
corrected to build the orphan via deliberately-legacy, FK-less scratch
tables of its own instead; (2) §4.5's "a partial run rolls back entirely"
read as true on both dialects the same way, which it isn't — corrected to
name the backfill's own transaction specifically and restate the
already-correct per-dialect distinction (SQLite: same transaction as the
column ALTERs; PostgreSQL: columns already committed, only the backfill
transaction's writes are undone) rather than leaving it generic. See the
design artifact's "Revision note (fourth pass)" for the full account.

**This design is now CLOSED / APPROVED.** Implementation remains NOT
AUTHORIZED — approving this document authorizes nothing beyond itself.

Technical design for porting the free-placement admin Floor/Zone builder
(item 1 of the three preserved `feat/floor-map` items above) onto the current
base. Decides the coordinate-collision strategy (`RestaurantTable` gets new
`map_x_per_mille`/`map_y_per_mille` columns; `pos_x`/`pos_y` are never
reinterpreted and keep serving the grid view and `floor.html` unchanged),
migration/backfill approach, write contracts, compatibility, rollout order,
and test coverage. No application code, migration, model, route, or template
was written or changed by this slice — the design document is the only
artifact.

This authorization covers **design only**. It does not authorize
implementation, migration execution, merge, push, or deployment. A separate,
explicit authorization is required before any implementation slice begins,
per the standard `AGENTS.md` workflow (`DESIGN → explicit authorization →
IMPLEMENT`).

Explicitly still out of scope, unaffected by this design: the operational
Floor page Map/List + Arrange mode, and the staff visual color picker — both
remain separate future slices per the audit above.

### Implementation slice 1 — model, migration, backfill — APPROVED / READY TO COMMIT

```text
Branch/worktree: feat/floor-map-coordinates, from 12131d2 (literal SHA,
                  fetched and captured per the immutable-baseline procedure)
Implementer: Claude
Reviewer: independent review — final re-review verdict: APPROVED
Files: app/models/oltp.py, app/migrate.py, docs/03_CURRENT_WORK.md,
       docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md,
       tests/test_floor_map_coordinates.py
Status: APPROVED / READY TO COMMIT. Integration, push, and deploy remain
        NOT AUTHORIZED. No UI, no drag endpoints, no Map/List + Arrange, no
        staff-color — the operational/admin interface remains entirely out
        of scope for this slice, unaffected.
```

First implementation slice of the approved Floor Plan Builder design
(`docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md` §3-4): the two new
`RestaurantTable.map_x_per_mille`/`map_y_per_mille` columns (nullable, no
default), their `ADDED_COLUMNS` entries, and `_backfill_table_map_positions`
wired into both `run()` (SQLite) and `_run_postgres()` independently.
Implements `ELIGIBLE(table, zone)` as the literal `INNER JOIN` the design
specifies, the step-0 orphaned-reference integrity check, the per-floor
`GRID_ROWS_SEEN` formula, Python-side `math.floor(x + 0.5)` rounding, the
final `[0,1000]` clamp, and the in-transaction fail-closed post-condition.
`pos_x`/`pos_y` are read-only inputs throughout; nothing reinterprets or
writes them.

**Independent review of this slice returned `FIX REQUIRED`, on two points:**

- **HIGH** — `tests/test_floor_map_coordinates.py` ran destructive
  PostgreSQL operations (`Base.metadata.drop_all()`, raw `DROP TABLE`)
  against whatever `PG_TEST_DSN` named, with no guard confirming the target
  was actually disposable. A typo, a copy-pasted production URL, or a leaked
  environment variable could have been wiped with no safeguard in the way.
- **MEDIUM** — the design artifact's transactional description of SQLite's
  failure behavior was out of date: it stated the two new columns roll back
  together with a failed backfill, in the same transaction as the column
  ALTERs. Measured execution (below) showed this is not how the actual
  driver behaves.

**Both corrected, this pass:**

1. **PostgreSQL destructive-target guard** (HIGH, fixed): added
   `assert_disposable_postgres_target()` to the test file — parses the DSN
   with `sqlalchemy.engine.make_url`, requires the PostgreSQL dialect,
   rejects a URL with no database name, rejects a database name without an
   unambiguous `test` marker, rejects a host ending in a known
   production-hosting suffix (this project's documented `render.com`/
   `neon.tech`, plus common managed-Postgres suffixes as defense in depth),
   and requires the separate `ALLOW_DESTRUCTIVE_PG_TESTS=1` opt-in — all
   before any connection or destructive statement, never printing the
   password, full DSN, or username. Called at the top of every function in
   the file that performs a destructive operation (`_fresh_schema`,
   `_drop_map_columns`, the orphan test's raw `DROP TABLE`), not only at the
   `__main__` entry point. 13 new unit tests cover every required case,
   including a timing + exception-type proof that a rejected, real-shaped
   -but-unreachable "production" DSN never attempts a network connection.
2. **Design artifact factual correction** (MEDIUM, fixed): corrected in
   `docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md` itself (§4.2's
   "Behavior on partial failure", its "Required test" paragraph, §4.5
   "Idempotency", and the top-level phase summary), plus a new "Revision
   note (fifth pass)" recording why. Real, measured evidence: under this
   project's stack (SQLAlchemy 2.0, pysqlite), `ALTER TABLE ADD COLUMN`
   auto-commits the instant it executes — even inside `engine.begin()`, DDL
   is not part of that transaction on this driver, only DML is. So on
   SQLite, a failed backfill leaves the two new columns in place (same as
   on PostgreSQL, for a different, already-correctly-described reason); the
   backfill's own DML (its UPDATEs) rolls back correctly on both dialects.
   Resulting state on both: columns present, every eligible row's position
   still NULL — safe and idempotent, indistinguishable from "columns just
   added, nothing backfilled yet". A re-run after fixing the cause
   completes the backfill — measured, not merely asserted. Every residual
   claim that SQLite removes/reverts the columns on this failure was
   removed. **This is a factual correction to the design's transactional
   description, drawn from running the tests the design itself specifies —
   it does not reopen or change the approved architecture, schema,
   `ELIGIBLE` definition, formula, or call-sites, which remain APPROVED
   from the fourth independent review.** The design document itself states
   this distinction explicitly now; it is not described here as "approved
   as-is" without that qualification.

A third, unrelated latent implementation gap (not from the review, found
independently while re-testing) was also fixed: the backfill function had
no `_table_exists` guard (unlike every other backfill in this file), so it
broke against a schema that legitimately never creates
`restaurant_table`/`zone` at all (`tests/test_pg_migration.py`'s narrow
payment-only harness). Guarded the same way `_backfill_locations` already
is.

**Second re-review of the same guard found one more real gap:**
`_TEST_MARKER_RE = re.compile(r"test", re.IGNORECASE)` accepted `test` as
any substring, not a delimited token — so `contest`, `latest`, `testament`,
and `protest` would all have passed the "database name looks disposable"
check. Fixed: the regex now requires `test` bounded by start/end of string,
`_`, or `-` (`(?:^|[_-])test(?:$|[_-])`). Explicit tests added for all ten
names named in that review — five that must pass (`test`, `floor_test`,
`test_floor`, `floor-test-db`, `floor_test_01`) and five that must still be
rejected (`contest`, `latest`, `testament`, `protest`, `restaurant`) — all
ten confirmed individually, not just the aggregate pass/fail count.

**Functional scope preserved, unchanged by this fix pass:** backfill
formula, columns, `ELIGIBLE` definition, SQLite/PostgreSQL call-sites,
model. No Floor UI, no endpoints, no Payment/Security file touched.

**Tests — real execution, not "exists but unrun":**

```text
SQLite (no PG_TEST_DSN): 57 assertions total (34 backfill/migration + 23
             guard unit tests, including the 10 explicit test-marker-token
             names from the second re-review), tests/test_floor_map_coordinates.py,
             0 failures. Executed with a real interpreter (py -3, Python
             3.14.5, SQLAlchemy 2.0.51).
Guard, dangerous-DSN rejection: confirmed zero connection/destructive
             operation on rejection — exception type is the guard's own
             UnsafePostgresTargetError (not a network/connection error
             class) and rejection is near-instant (<1s) for a real-shaped
             -but-unreachable AWS RDS-style hostname, proving no network
             I/O was attempted.
PostgreSQL (PG_TEST_DSN + ALLOW_DESTRUCTIVE_PG_TESTS=1, both required):
             disposable database `floor_map_coords_test` inside the
             existing local `rms-pgtest` Docker container
             (postgres:16-alpine, already running on this machine) — never
             production. 87 assertions, 0 failures. Confirmed separately:
             PG_TEST_DSN set WITHOUT the opt-in var is rejected before any
             table is created (database verified to still hold zero tables
             afterward). Disposable databases dropped after the run; the
             container itself was not modified.
Regression:  tests/test_migrate.py, tests/test_floor_spatial.py,
             tests/test_reservations_map.py — all pass, run both standalone
             and via this slice's own regression subprocess check.
             tests/test_pg_migration.py — passes on its own fresh disposable
             database (separate from this slice's own test database, to
             avoid cross-test contamination).
```

**Evidence boundary:** `tests/test_admin.py` was not re-verified beyond a
single run that failed on missing seed data (`no owner in the database`) —
an environment/fixture precondition, not a code assertion, and consistent
with AGENTS.md's already-recorded note that this suite has pre-existing
setup sensitivity. Not in this slice's required regression set
(migration/Floor/Reservations); not chased further. No production database
was read, written, or connected to at any point in this slice. No
credential, password, or full DSN was ever printed to output.

**Final independent re-review: APPROVED.** The delimited-token
`_TEST_MARKER_RE` fix from the second re-review (above) was confirmed
present and correct. The reviewer reproduced the 57 SQLite/guard checks
(0 failures) themselves; the embedded regression suites
(`test_migrate.py`, `test_floor_spatial.py`, `test_reservations_map.py`,
`test_pg_migration.py`) were reproduced and passed. The 87-check
PostgreSQL run (0 failures) was executed by the implementer against a
disposable local Docker Postgres target (`rms-pgtest`), guarded by
`assert_disposable_postgres_target`, and reported to the reviewer as
evidence rather than independently re-run.

**Implementation slice 1: APPROVED / READY TO COMMIT.** Integration
(merge/fast-forward into `main`), push, and deploy remain NOT AUTHORIZED
and require their own separate explicit instruction. The operational/admin
Floor Plan Builder interface (drag endpoints, Map/List + Arrange mode,
staff-color picker) remains entirely out of scope for this slice and is
unaffected by this approval.

**Implementation slice 1: VALIDATED IN PRODUCTION.** Since the approval
above, slice 1 was integrated, pushed, and deployed. Manually confirmed:

```text
Render commit    c18b9e03c264129f019341deddcad31e68b1cc05
Deploy method    Auto-Deploy
Indicator        green / concluded
Shown at         2026-09-07 02:55:19 PDT
Neon branch      production
Neon database    neondb
map_x_exists     true
map_y_exists     true
invalid_positions  0
orphaned_zones     0
```

This confirms slice 1's schema/migration/backfill in the live production
database, not the exact process SHA independent of this observation (§11 —
a visual dashboard/console reading, not a Render API/webhook confirmation).
This validation covers **slice 1 only**. It does not extend, imply, or
substitute review/approval/integration status for slice 2 below.

### Implementation slice 2 — admin visual builder — CLOSED / APPROVED (not integrated)

```text
Branch/worktree: feat/floor-admin-ui, from c18b9e0 (literal SHA, fetched
                  and captured per the immutable-baseline procedure)
Implementer: Claude
Reviewer: independent review, re-review pass — verdict APPROVED WITH
          NON-BLOCKING NOTES. Prior pass on this same slice had returned
          FIX REQUIRED (2 HIGH findings, pointer-ownership); this re-review
          confirmed both are fixed and found no new blocking issue.
Files: app/routers/admin.py, web/templates/admin_tables.html,
       web/static/app.css, docs/03_CURRENT_WORK.md,
       tests/test_floor_admin_ui.py
Status: CLOSED / APPROVED — independently re-reviewed, verdict APPROVED
        WITH NON-BLOCKING NOTES, both prior HIGH findings confirmed fixed,
        the review's own non-blocking note incorporated (see below).
        This closing pass commits the slice to its own branch/worktree
        (feat/floor-admin-ui) — it is still NOT INTEGRATED into main, NOT
        PUSHED, and NOT DEPLOYED. Closing this slice authorizes nothing
        beyond itself: integrating it into main is a separate, explicitly
        authorized step (see "Next Authorized Action" below). This does
        NOT mean the free-placement builder UI is live anywhere.
```

**Independent re-review verdict: APPROVED WITH NON-BLOCKING NOTES.**

The re-review re-read the pointer-ownership fix below line by line (not
trusting the implementer's own account of it), re-ran all four required
test suites fresh, and confirmed:

- Both prior **HIGH** findings — no re-entrancy guard on `pointerdown`, and
  no `pointerId` ownership check on `pointermove`/`pointerup`/
  `pointercancel` — are fixed exactly as described in "Both fixed, this
  pass" below: the guard runs before any side effect, `pointerId` is
  stored on all three gesture kinds, and all three consumer handlers
  reject a non-owning pointer before touching position, capture, classes,
  or the network.
- No new blocking issue in the pointer-ownership fix, the two AJAX
  endpoints, the shared `_TABLE_SHAPES` constant, the overlapping-zone
  hit-test, or this documentation file.
- **Declared limitation, unchanged and still real:** no browser/DOM test
  harness exists anywhere in this codebase. Both structural tests
  (`test_admin_tables_page_enforces_single_owning_pointer`,
  `test_admin_tables_page_has_pointercancel_cleanup`) confirm the guard
  code's *text* is present in the right handler and would fail if a guard
  were removed or misplaced — they do not execute a real PointerEvent
  sequence in an actual browser. This is a carried-forward, disclosed gap
  (§9's original carve-out), not something this slice's closing resolves.
- **Non-blocking note, incorporated this pass:** the re-review noted the
  pointercancel structural test asserted the absence of `post(` (the local
  fetch-wrapping helper) but not of a raw `fetch(` call bypassing that
  helper — a hypothetical future edit could add a direct `fetch()` inside
  the handler without failing that assertion. Fixed: the same test now
  also asserts `"fetch(" not in handler`. No implementation behavior
  changed — `pointercancel` already made no network call of any kind; this
  only widens what the test would catch if that ever changed.

**Independent review verdict: FIX REQUIRED — 2 HIGH findings.**

Root cause: the global `drag` state did not identify which pointer owned
the active gesture. A second `pointerdown` could silently replace the
active gesture's `drag` object; a `pointermove`, `pointerup`, or
`pointercancel` carrying a *different* `pointerId` than the one that
started the gesture would still be accepted, because nothing ever compared
`e.pointerId` against anything.

- **HIGH** — a second pointer going down while one gesture was already
  active overwrote `drag` outright (no ownership check existed on
  `pointerdown` at all), silently abandoning the first pointer's gesture
  mid-drag with no cleanup, and handing control of the (now-wrong) `drag`
  object to the second pointer.
- **HIGH** — `pointermove`, `pointerup`, and `pointercancel` acted on
  whichever `drag` object happened to be set, regardless of which pointer
  fired the event — a stray event from an unrelated pointer (e.g. a second
  finger lifting, or a second touch's own `pointercancel`) could move,
  autosave, or cancel a gesture it never started.

**Both fixed, this pass — single-active-gesture-with-owner policy:**

1. `pointerdown` now checks `if (drag) return;` immediately (before
   computing which element was hit or building a new `drag` object) — a
   second pointer going down while one gesture is active is ignored
   completely: no new `drag`, no `setPointerCapture` attempt, no
   `preventDefault`. The first gesture's `drag` object is untouched.
2. Every `drag` object (`table`, `move`, `resize`) now stores
   `pointerId: e.pointerId` from the `pointerdown` that created it.
3. `pointermove`, `pointerup`, and `pointercancel` each now check
   `e.pointerId !== drag.pointerId` immediately after the existing
   `if (!drag) return;` guard, and return without touching anything —
   position, capture, `.dragging`, autosave, or `.unsaved` — when the
   event's pointer is not the gesture's owner. `pointerup`'s listener
   signature gained the `e` parameter it previously lacked, since reading
   `pointerId` requires it.
4. Previously-approved cancellation behavior is unchanged and still holds
   for the owning pointer: no autosave request on cancel; the zone and all
   of its carried tables are marked `.unsaved` together; capture is
   released defensively; `.dragging` is always stripped; `drag` is cleared.

**Functional scope preserved, unchanged by this fix:** single-pointer drag
math (proportional resize, drop-into-zone hit-test via
`elementsFromPoint`), the shape-constant dedup, and every backend route —
none of those touched by this pass.

**Tests — structural, same declared limitation as the prior pointercancel
coverage (no browser/DOM test harness exists anywhere in this codebase;
adding one — jsdom or a real browser driver — for this one gesture-ownership
path was judged disproportionate rather than silently skipped: Node
24.18.0 is available on this machine but nothing in the repo currently
uses it for tests, and there is no `package.json`).** Two tests, each
isolating one handler's own source text so a removed guard fails the
specific assertion tied to it, not a loose "feature exists somewhere"
check:

```text
test_admin_tables_page_enforces_single_owning_pointer:
    pointerdown's `if (drag) return;` exists and precedes the first
    `drag = {` assignment (re-entrancy guard runs before state is built)
    pointerId is stored on all 3 drag kinds (table/resize/move)
    pointermove/pointerup/pointercancel each contain
    `e.pointerId !== drag.pointerId`
    pointerup's listener now declares the `e` parameter it needs
test_admin_tables_page_has_pointercancel_cleanup (extended, this pass):
    the existing cleanup/marking/no-post assertions, plus the new
    ownership-guard line inside the same isolated handler
```

Second implementation slice of the approved Floor Plan Builder design
(`docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md` §5-7): the admin
Tables/map page (`admin_tables.html`) gets a free-placement map, additive to
the existing grid-based table/zone management (the grid-drag interaction on
*this* page is retired per §5; `floor.html`'s own grid rendering is
untouched, out of scope per §10).

Delivered, strictly per the design:

- `POST /admin/tables/map-layout` (§6.1) — `id:x:y[:shape[:zone_id]]` per
  table, comma-separated; clamps x/y to [0,1000] server-side; validates
  shape against `{round, square, rect}` (400, not silent ignore, on an
  invalid value); reassigns zone via the existing shared `_assign_zone`
  when a zone_id is given; `pos_x`/`pos_y` are never read or written here.
- `POST /admin/zones/{zone_id}/move-layout` (§6.2) — one atomic
  `db.commit()` for a zone's rectangle and every one of its tables'
  positions; requires `tables` to name exactly the zone's current active
  table ids (no missing, no extra, no duplicate, none from another zone)
  before writing anything; rejects an incomplete/wrong/duplicate payload
  with nothing written, byte-identical to the pre-request state.
- `admin_tables.html` — zone rectangles and tables rendered absolutely
  positioned (per-mille percent) over one canvas; pointer events (not HTML5
  drag-and-drop — §7's touch/mouse note) drive table move/drop-into-zone,
  zone move, and zone corner-resize (client computes the proportional new
  table positions; the backend only validates and persists what it's
  sent, per §6.2's division of responsibility); shape-cycle button now
  persists through the new endpoint instead of the old grid endpoint's
  DOM-scraping JS, which has nothing left to read on this page (§6.1
  Correction 2); a failed autosave marks the element `.unsaved` (visible
  red outline + tooltip) and leaves it exactly where dropped, never
  snapping back or failing silently (§6.3).
- `admin.py::_table_map_positions` — the NULL-fallback placement (§4.4):
  a zone's never-placed tables, sorted by id, spread as distinct points on
  a ring centred on that zone's rectangle (a true rank/bijection, not id
  parity or `id % N`, both of which collide for some id sets) — computed
  fresh on every page render, nothing stored.
- `admin_floors.html` untouched — stays the separate floor/zone creation
  entry point, per §5/§10.
- The old `POST /admin/tables/layout` grid route is untouched and stays
  green under its own existing test; nothing on this page calls it anymore.

**Known limitation, disclosed, not fixed here (matches design §6.3's own
accepted tradeoff):** if a save fails (network error, or a zone deleted out
from under a concurrent drag) and dataset is not advanced, the *next* drag
on that same element computes its delta from the last-*saved* position, not
the currently-*displayed* one — a visual jump back on the very next
gesture after a failure, before the retry's own drop corrects it. No data
is at risk (the failed write never landed), and the failure is still
visibly flagged, never hidden. Left as a `ponytail:` comment at the call
site rather than adding version/optimistic-lock tracking not present
anywhere else in this codebase, matching §12's decision to accept
last-write-wins rather than introduce new concurrency machinery for this
slice.

**Authorial correction round 1 (self-applied — independently reviewed
afterward; that review's FIX REQUIRED verdict and its 2 HIGH findings are
recorded above, ahead of this section, along with correction round 2 that
resolved them):**

1. **Pointer cancellation.** `pointerdown`/`pointermove`/`pointerup` had no
   `pointercancel` handling — a browser-cancelled gesture (touch-scroll
   takeover, OS interrupt) left `drag` set, pointer capture held, and
   `.dragging` on the chip, so the *next* gesture on that element could
   start from stale state. Added one `pointercancel` listener covering all
   three kinds (table/move/resize, since they share the one `drag` object):
   clears `drag`, releases pointer capture defensively
   (`try/catch`, a no-op if the UA already released it), always strips
   `.dragging`, and — since the element may already sit wherever the
   cancelled gesture displaced it, with nothing sent — marks it `.unsaved`
   with an accurate tooltip ("Move cancelled before saving"), distinct from
   the existing "Save failed" text used for a real rejected/failed AJAX
   call. For zone move/resize, the zone and every one of its carried tables
   are marked together, matching how they were displaced together. No
   `post()` call anywhere in the handler.
2. **Duplicate-id + omission test.** Added
   `test_move_layout_rejects_duplicate_id_and_omission_together`: one
   payload naming a table twice while omitting the zone's other active
   table. Confirms 400 and zero partial write (zone rect and both tables'
   positions all read back unchanged). The existing endpoint code already
   raises on the duplicate before ever reaching the missing/extra check —
   this test is what actually exercises that combination; it did not exist
   before this pass.
3. **pointercancel test coverage.** No browser JS harness exists in this
   codebase (unchanged from slice 2's original scope note); a real
   pointer-event simulation is disproportionate to add for this one gesture
   path. Added `test_admin_tables_page_has_pointercancel_cleanup` instead:
   fetches the rendered page and structurally confirms, inside the isolated
   `pointercancel` handler's own source text, that state is cleared, capture
   is released, `.dragging` is removed, the affected element(s) are marked,
   the zone's tables are flagged together, and — the one negative
   assertion — no `post(` call appears in that handler. This proves the
   handler exists with the right shape; it does not execute it or prove
   runtime behavior in an actual browser.
4. **Overlapping zones.** The drop hit-test iterated `.zone-rect` in DOM
   order and kept overwriting the match, so the *last* zone in that
   `querySelectorAll` order silently won regardless of what a manager
   actually sees stacked on top at the drop point. Analysis: today's CSS
   gives zone-rects no explicit `z-index` and no transform, so DOM order
   does currently equal paint order — but nothing made that correspondence
   explicit or protected it from a future style change (e.g. a "just
   dropped" or "selected" zone getting its own `z-index`). Replaced the
   manual bounding-rect loop with `document.elementsFromPoint(cx, cy)`
   (topmost-first, native browser API) and picked the first `.zone-rect` in
   that stack — this is the browser's own answer to "what's actually
   painted on top here", correct under any future stacking, not a
   re-implementation of stacking rules that could drift from them. No
   design-doc gap remained to record as a limitation: the native API fully
   resolves the ambiguity.
5. **Shape constant duplication.** `_TABLE_SHAPES` (this slice's own
   module-level set) and a second, identical `shapes = {"round", "square",
   "rect"}` local to the legacy `/tables/layout` route were two literals for
   the same three values. Moved `_TABLE_SHAPES` next to `GRID_COLS` (before
   either route) and pointed the legacy route's membership check at it
   instead of its own local set. The legacy route's behavior is unchanged
   byte-for-byte: same three values, same silent-ignore-on-invalid-value
   semantics (still no 400 there — untouched), same everything except the
   set object's identity.

**Tests — real execution:**

```text
tests/test_floor_admin_ui.py (17 tests / 63 assertions, SQLite):
    map-layout: move, clamp-out-of-range, unknown-id/malformed rejection,
    shape valid/invalid/omitted, zone_id reassignment, 403 non-Owner
    move-layout: atomic move, omitted/extra/duplicate/wrong-zone rejection,
    duplicate-id + omission together (new, this pass — §2 above)
    (each asserting nothing was written, not just the response code),
    empty-on-empty vs empty-on-occupied, inactive-table exclusion +
    rejection-if-named, 403 non-Owner
    page render: mixed placed/NULL-fallback tables render without error
    pointercancel structural check (new, this pass — §3 above)
Regression (§9 "must stay green"), all re-run standalone, all pass —
re-confirmed fresh again in this correction pass (2026-09-07), not just
carried over from the original implementation pass:
    tests/test_floor_spatial.py — 7 tests, incl. the OLD grid-drag
        test_save_layout_sets_shape_and_position unmodified and still green
    tests/test_reservations_map.py — confirms zero position-dependency
    tests/test_migrate.py
Evidence boundary (§9, explicit carve-out): tests/test_admin.py re-run and
    still fails the same pre-existing way documented for slice 1 — "no
    owner in the database" (missing seed data), not a code assertion,
    reproduced on this same base before this slice's changes. Not a
    regression; not chased further, per the design's own instruction not
    to assume it fixed without re-checking (it isn't fixed, confirmed).
Not in scope for test evidence (§9 explicit carve-out): browser/touch/
    device validation of the pointer-drag JS — no browser test harness
    exists anywhere in this codebase.
```

Out of scope, confirmed unaffected: `floor.html`'s operational Map/List +
Arrange mode, the staff-color picker, `admin_floors.html`, CSRF (pre
-existing app-wide gap, not this slice's to fix), keyboard-accessible drag
(pre-existing gap), Payment/Security files (none touched).

## Next Authorized Action

```text
The Floor Plan Builder design (design/floor-plan-builder) is CLOSED /
APPROVED. Both implementation slices built against it are done:

  Slice 1 (model, migration, backfill, branch feat/floor-map-coordinates)
  — VALIDATED IN PRODUCTION (see "Implementation slice 1" above). Not
  future, not pending — it is live.

  Slice 2 (admin visual builder, branch feat/floor-admin-ui) — CLOSED /
  APPROVED by independent re-review (see "Implementation slice 2" above),
  committed to its own branch by this pass. NOT YET integrated into main,
  NOT pushed, NOT deployed.

**Next authorized administrative step (still requires explicit
authorization before acting — not self-authorizing):** fast-forward
`feat/floor-admin-ui` into `main` (a fast-forward is possible only if
`main` has not moved past this branch's own base, `c18b9e0` — re-verify
`git merge-base` immediately before merging, not assumed from this note).
No push and no deploy are implied by that merge; each remains its own,
later, separately authorized step. The operational Map/List + Arrange
mode and the staff-color picker (out of scope for both slices above)
still require their own separate, explicit authorization each, same as
before.

open_order_on_table's proven PostgreSQL race remains a real, tracked risk
(see "Residual risks" below) and a candidate for its own future,
separately-designed-and-authorized slice — it is NOT the next proposed
slice ahead of the Floor Plan Builder integration above; it is preserved
here as future risk/backlog, not queued ahead of it. Administrative
cleanup of the five superseded documentation branches is also optional and
requires explicit authorization, independent of either.
```

G1-G8 are complete. Release B (Payment/Security Stage 1/2a/2b), Floor UI +
Reservations, Floor spatial, test-evidence preservation, the documentation
reconciliation (`c39856a`), and its synchronization closeout (`f6bd4ef`) are
integrated and pushed. Local `main` and `origin/main` were synchronized at the
last verification; agents must resolve the current SHA rather than rely on this
historical snapshot. Render visually showed `f6bd4ef` Live and production
`/healthz` was HEALTHY.

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
