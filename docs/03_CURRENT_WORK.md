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

### Implementation slice 1 — model, migration, backfill — CLOSED / INTEGRATED / DEPLOYED

```text
Branch/worktree: feat/floor-map-coordinates, from 12131d2 (literal SHA,
                  fetched and captured per the immutable-baseline procedure)
Implementer: Claude
Reviewer: independent review — final re-review verdict: APPROVED
Files: app/models/oltp.py, app/migrate.py, docs/03_CURRENT_WORK.md,
       docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md,
       tests/test_floor_map_coordinates.py
Commit: c18b9e03c264129f019341deddcad31e68b1cc05
Status: CLOSED / INTEGRATED / DEPLOYED. Fast-forwarded into local main and
        pushed to origin/main (12131d2 → c18b9e0). No UI, no drag
        endpoints, no Map/List + Arrange, no staff-color — the
        operational/admin interface remains entirely out of scope for
        this slice, unaffected.
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
above, slice 1 was integrated, pushed, and deployed (`12131d2` → `c18b9e0`,
fast-forwarded into local `main` and pushed to `origin/main`). Manually
confirmed:

**Pre-deploy recovery point:** Neon project `restaurant-oregon`, branch
`backup-pre-floor-coordinates-20260907`, created from the `production`
branch (data and schema, state at creation time) at approximately
2026-09-07 09:51 UTC, 7-day retention (expires 2026-09-14). Created and
confirmed by the repository owner directly in the Neon console; not
independently re-verified by the implementing agent (no Neon CLI/API
access from this session).

**Post-deploy validation — reported by the repository owner, not
independently re-verified by the implementing agent** (this session has no
Render dashboard/API access and no PostgreSQL production connection):

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

`/healthz` was independently confirmed by the implementing agent itself:
HTTP 200, body `ok` (checked directly against the public endpoint,
2026-09-07 10:00:40Z).

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

## Floor Plan Builder — production diagnosis — FIX REQUIRED; fix IMPLEMENTED, NOT REVIEWED, NOT INTEGRATED

```text
Branch/worktree: fix/floor-zone-overlap, from daa6d5a (literal SHA, fetched
                  and captured per the immutable-baseline procedure)
Implementer: Claude
Reviewer: not yet assigned
Files: app/migrate.py, app/routers/admin.py, web/static/app.css,
       web/templates/admin_tables.html, tests/test_floor_zone_overlap.py
Status: DIAGNOSIS CONFIRMED (FIX REQUIRED), FIX IMPLEMENTED. NOT YET
        INDEPENDENTLY REVIEWED. Not committed, not integrated, not pushed,
        not deployed. `daa6d5a` (origin/main) itself remains deployed and
        healthy — this is a functional-validation finding on top of a
        successful deploy, not a deploy-health regression.
```

**`daa6d5a` deploy status, unaffected by this section:** confirmed Live via
Render (Auto-Deploy, green/concluded) and `/healthz` HTTP 200 body `ok` —
see the push/verification record above. Deployed and healthy. Post-deploy
*functional* validation of the admin Floor Plan Builder against that
deployed code, separately, found the problem below — a real defect in
already-deployed behavior, not a sign the deploy itself failed.

**Diagnosis (read-only investigation, no production access used):** table
reassignment via drag worked for some zones (Patio, Window) but silently
never reached others (Main, Bar) on the same floor. Root cause, confirmed
by reproducing the real `Zone`/`create_zones` code against a disposable
SQLite database: every zone — old and new — is created without an explicit
position, so `Zone.pos_x/pos_y/width/height` default to the same fixed
`(60, 60, 360, 300)`. Two zones neither has ever been dragged/resized land
on the byte-identical rectangle; the admin builder's drop hit-test
(`document.elementsFromPoint`, `web/templates/admin_tables.html`) can then
only ever resolve to ONE of them for any point inside that shared
rectangle — the other is permanently unreachable by drag, regardless of
where exactly a manager drops a table. Verified: the backend endpoint
itself (`POST /admin/tables/map-layout`) correctly persists a reassignment
to either zone when given its `zone_id` explicitly — the defect is entirely
in the geometry/hit-test path, not the backend. No production database or
dashboard access was used or is required to reach this conclusion; it
follows from the shared model default and the client's own hit-test logic.
Full write-up, including the reproduction transcript and a feature-parity
comparison against the historical `feat/floor-map` builder: prior
diagnostic session (this same investigation), not duplicated here.

**No reliable "never positioned" marker exists.** Checked before writing
any fix: `Zone` carries no "ever moved"/"is default" flag, and
`/admin/floors`'s "Map box" numeric fields (`web/templates/admin_floors.html`)
let a manager reproduce the exact default by hand anyway, indistinguishably
from a zone that simply was never touched. **Decision:** the fix treats
every EXACT `(pos_x, pos_y, width, height)` match among active zones on the
same floor the same way, regardless of cause, and touches nothing that
merely overlaps without being byte-identical — overlap itself stays
allowed by design (this project's own inherited `/tables/layout` docstring:
*"Overlap is allowed ... the manager arranges the room as it really is."*).

**Fix implemented, this pass (NOT yet reviewed):**

1. **New zones never collide** — `POST /admin/floors/{id}/zones/create`
   (`app/routers/admin.py`) now assigns `pos_x/pos_y/width/height`
   explicitly via a new deterministic placement helper,
   `place_new_zone_rect` (`app/migrate.py`, shared with the backfill below
   so the algorithm lives in exactly one place). Placement is seeded from
   the count of active zones already on THAT floor — not `sort_order`,
   which can repeat — and explicitly checked against every active zone's
   real rect on the same floor before being accepted; other floors are
   never read or touched.
2. **Existing coincident zones are corrected** — a new idempotent
   migration backfill, `_backfill_zone_overlap` (`app/migrate.py`), wired
   into both `run()` (SQLite) and `_run_postgres()`, each its own explicit
   `engine.begin()` call site (not shared with the ADDED_COLUMNS/ALTER
   work, and not try/except-swallowed — fail-closed, matching
   `_backfill_table_map_positions`'s existing discipline). Per group of
   active zones on the same floor sharing an exact rect: the lowest-id zone
   is the anchor and never moves; every other zone in the group is
   repositioned (position only — width/height preserved exactly) against
   every active zone's rect on that floor, so it can land on neither the
   anchor nor any unrelated zone already there.
3. **Tables follow their corrected zone** — a moved zone's own active
   tables that already carry a real (non-NULL) `map_x_per_mille`/
   `map_y_per_mille` are translated by the exact same delta as their zone
   (a pure translation, not a proportional rescale, since width/height
   never change during this correction). `pos_x`/`pos_y` — the separate,
   older grid columns — are never read or written by this backfill.
4. **NULL/inactive table policy, explicit and tested:** an inactive table
   is never read or written (retired tables' stale coordinates don't
   matter — same convention as `_backfill_table_map_positions`,
   `_free_cells`, `move_zone_layout`, all already in this codebase). A
   table with a NULL map position is left NULL — there is nothing real to
   translate, and `_table_map_positions()` (`app/routers/admin.py`)
   already computes a fresh ring-fallback position around the zone's
   rectangle at render time, from whatever that rectangle currently is —
   so a never-placed table "follows" the corrected rectangle for free,
   with no write needed.
5. **Dialect behavior, documented honestly:** this backfill is pure DML
   (`UPDATE` only) — no `ALTER TABLE`, unlike the ADDED_COLUMNS work
   elsewhere in `migrate.py`. That distinction matters: on this driver, an
   `ALTER` auto-commits immediately on SQLite even inside
   `engine.begin()` (`_backfill_table_map_positions`'s own docstring) —
   but plain DML has no such quirk on either dialect. A raised exception
   anywhere in `_backfill_zone_overlap` rolls back every `UPDATE` it
   already issued, completely and identically, on both SQLite and
   PostgreSQL — verified directly (see tests below), not merely asserted
   from the driver docs.
6. **Non-blocking overlap warning** — `POST /admin/zones/{id}/move-layout`
   now adds an advisory `X-Zone-Overlap-Warning: exact` response header
   (status code and body unchanged, still `204`) when a move/resize
   results in an exact-rectangle coincidence with another active zone on
   the same floor. A *partial* overlap (still allowed by design) never
   sets it, and a rejected (400) request never sets it either — the
   client only shows this on an already-successful save, distinct from
   `.unsaved` (a failed save), both visually (dashed blue outline vs solid
   amber) and in the tooltip text.
7. **Shape and capacity are visually real again** — `round`/`square`/
   `rect` now render as an actual ellipse/rounded-square/stretched-rect on
   the map (`web/static/app.css`, driven off the existing `data-shape`
   attribute the shape-cycle button already maintains — no backend
   contract change), and chip width scales with seat count (a new
   `--cap-w` custom property, clamped to `[72, 140]px` by both the
   generating formula and a hard CSS `min-width`/`max-width` backstop, so
   no capacity value can distort the layout). `floor.html` (the
   operational page) was not touched — it does not use the `.chip`/
   `.map-chip` classes at all, confirmed before making any CSS change.

**Tests — real execution, new file `tests/test_floor_zone_overlap.py`
(SQLite always; PostgreSQL gated by the exact same
`assert_disposable_postgres_target`/`ALLOW_DESTRUCTIVE_PG_TESTS=1` guard
`tests/test_floor_map_coordinates.py` already defines — imported directly,
never a second copy of that safety logic):**

```text
SQLite: 13 tests, all pass — new-zone placement (batch distinctness,
    avoids an existing active zone's exact rect, floors independent),
    backfill (2-way and 3-way group determinism, anchor preserved,
    non-identical zones on the same floor untouched, the OTHER floor's
    own coincident zone untouched, table translation matches the zone's
    delta exactly, pos_x/pos_y never touched, NULL stays NULL, inactive
    untouched, idempotent re-run is a true no-op, a forced mid-function
    failure — two zones sized to the full 1000x1000 canvas, so
    place_new_zone_rect's search exhausts deterministically — leaves an
    earlier, already-successful group's UPDATE rolled back too, not
    half-applied), migrate.run() wiring, the move-layout warning header
    (exact-only, never on a failed/rejected request), shape/capacity
    markup and CSS.
PostgreSQL (PG_TEST_DSN + ALLOW_DESTRUCTIVE_PG_TESTS=1, both required):
    disposable database inside the existing local `rms-pgtest` Docker
    container (postgres:16-alpine, already running on this machine) —
    never production. Backfill de-collides the seeded group, anchor
    unchanged, second run is a no-op — same assertions as SQLite,
    executed for real against a real PostgreSQL server. Disposable
    database dropped after the run; the container itself untouched.
Regression, all re-run standalone, all pass:
    tests/test_floor_admin_ui.py, tests/test_floor_map_coordinates.py
    (including its own embedded PostgreSQL scenarios, re-verified against
    the same disposable database), tests/test_floor_spatial.py,
    tests/test_reservations_map.py, tests/test_migrate.py.
git diff --check: clean.
```

**Risks and limitations, disclosed:**
- The exact production values of Main/Bar's `pos_x/pos_y/width/height`
  were never read (no production database access, by design) — the root
  cause was confirmed by reproducing the real code path, not by observing
  production data directly. The fix corrects the general defect
  regardless of whatever those specific values turn out to be.
- The non-blocking overlap warning only covers zone move/resize
  (`move-layout`); it does not warn on table drag-to-reassign
  (`map-layout`) — a table successfully reassigning to a zone that happens
  to exactly coincide with another gives no signal that the *other* zone
  is now unreachable. Judged acceptable for this pass since the
  underlying coincidence itself is what's being eliminated by the
  backfill; flagged here rather than silently scoped out.
- ~~`place_new_zone_rect`'s grid is exact and collision-checked for any
  realistic zone count; its own internal safety cap (2000 attempts) exists
  only to fail loudly rather than loop forever in a pathological case~~ —
  **superseded, see "Second pass" below.** Independent review found this
  claim both incomplete (the real threshold was width/height > 960, not
  only the literal 1000x1000 case this section originally called out) and
  its consequence understated (a "fail loudly" here meant an unhandled
  `RuntimeError` inside a fail-closed, un-guarded, always-run boot-time
  backfill — i.e. the *entire application* refusing to start, in any
  environment, not merely "the migration fails"). Fixed; see below.
- Independent review of this fix found the HIGH issue above (and several
  lower-severity ones), described completely in "Second pass" below.

**This section records diagnosis and a first implementation pass, since
corrected by a second pass below in response to independent review. It
does NOT constitute approval, integration, or deployment of this fix** —
those remain separate, later, explicitly-authorized steps, same
discipline as every other slice above.

## Floor Plan Builder zone-overlap fix — second pass (independent review response)

```text
Branch/worktree: fix/floor-zone-overlap (same branch, second pass), from
                  daa6d5a (unchanged — still the literal base SHA)
Implementer: Claude
Reviewer: independent review — verdict FIX REQUIRED (1 HIGH, 2 MEDIUM,
          2 LOW); this pass addressed all five
Files (this pass): app/services/zone_geometry.py (NEW), app/migrate.py,
       app/routers/admin.py, web/static/app.css, web/templates/
       admin_tables.html, tests/test_floor_zone_overlap.py
Status: superseded by "third pass" below, which incorporates the
        re-review's own two LOW notes and closes this slice. See that
        section for the current, final status of this fix.
```

**HIGH — the backfill could crash the entire boot, not just fail to fix
geometry.** The first pass's `place_new_zone_rect` raised `RuntimeError`
after 2000 failed attempts, and `_backfill_zone_overlap` was wired
fail-closed and unconditionally into both `run()` and `_run_postgres()` —
called at module level on every app startup
(`app/main.py`, `migrate.run(engine, strict=is_production())`), with no
try/except. Independent review found the real trigger condition is any
group of 2+ active coincident zones with **width > 960 AND height > 960**
(the grid/scatter's minimum offset is 40, and 1000 − 960 = 40 — not only
the literal 1000×1000 example originally documented) — and confirmed the
consequence is not "the migration step fails", it is **the whole
application refusing to start, in any environment**, including the very
admin UI a manager would need to open to fix the offending zone. The
diagnosed real-world case (Main/Bar at the 360×300 default) does not
trigger it — the diagnosis itself was correct — but the mechanism is real
and was exactly what `test_backfill_failure_leaves_no_partial_dml`
deliberately forced to fire.

**Fixed — availability over perfect geometry, made explicit and tested:**

- `place_new_zone_rect` (moved to `app/services/zone_geometry.py`, see
  architecture below) now tries the existing grid/scatter first, then a
  bounded, deterministic systematic sweep of the rectangle's actual legal
  position space (`x ∈ [0, 1000-width]`, `y ∈ [0, 1000-height]`) — no
  randomness, and bounded at 200,000 checks (a pragmatic ceiling, not a
  claim of exhaustiveness past it — comfortably covers any zone size up to
  roughly half the canvas in both dimensions, including every size this
  review exercised: 961×961, 961×300). Returns `None`, never raises, when
  every legal position for that exact size is already occupied — the true
  impossible case (e.g. 1000×1000, whose only legal position is (0, 0)).
- `_backfill_zone_overlap` now returns a small `ZoneOverlapResult(moved,
  unresolved)` instead of a flat list. A zone `place_new_zone_rect` cannot
  place is left **completely unchanged** — not resized, not force-moved —
  and reported in `.unresolved` (also surfaced in the boot log, prefixed
  `UNRESOLVED:`, with no credentials or connection details — only this
  project's own zone/floor ids and dimensions). Every other group in the
  same run is still processed normally. Startup never halts for this.
- The fail-closed `RuntimeError` is **narrowed, not removed**: it still
  fires — still halting startup, matching every other backfill's own
  discipline in this file — but only when a zone the function itself
  should have moved or flagged is found still coincident afterward (a
  genuine internal defect). It never fires for an honestly-reported
  unresolved zone. Verified with two different forced scenarios: the
  now-legitimate impossible case (no raise) and a simulated broken
  placement function that lies about resolving a collision (still raises,
  and rolls back an earlier group's already-issued `UPDATE` too).
- **Policy, stated plainly for anyone reading this later:** for this
  backfill, keeping the application available takes priority over fully
  resolving an impossible zone-overlap geometry. A data condition that
  cannot be perfectly fixed automatically must never become a reason the
  whole system cannot run.

**MEDIUM — architecture: layering violation, corrected.**
`place_new_zone_rect` lived in `app/migrate.py` (a boot-time
migration/backfill module) but was imported live by
`app/routers/admin.py` (an HTTP handler) — no circular import today, but
the wrong module for a live request path to depend on. Moved to a new,
neutral `app/services/zone_geometry.py` (following this codebase's
existing `app/services/*` convention): pure functions only, its only
import is `from __future__ import annotations` — no router, no migration
module, no Engine, no Session, confirmed by a test that inspects its
actual import lines, not just a substring search. Both `app/migrate.py`
and `app/routers/admin.py` import `place_new_zone_rect` from there
symmetrically; the residual definition in `migrate.py` is gone.

**MEDIUM — CSS: rect lost its visual distinction at high capacity,
fixed.** `.map-chip[data-shape="rect"]` computed a wider `width`, but the
base `.map-chip` rule's `max-width: 140px` still capped the *rendered*
width regardless — at capacity ≳ 11, a rect table's final width was
silently clamped back down to the exact same 140px ceiling as round/square,
erasing the shape distinction exactly where it mattered most (no existing
test caught this — the previous test only checked that CSS tokens were
textually present, never the resulting computed width). Fixed: rect now
declares its own `min-width`/`max-width` (`[97, 190]px`, the same ×1.35
stretch applied to round/square's own `[72, 140]px`), which — by CSS
specificity, not source order — overrides the inherited ceiling for rect
specifically, leaving round/square's own bounds untouched. A new test
computes the actual cascade-resolved effective width (formula → clamp,
the same two-step a browser applies) for round/square vs. rect at
capacities 1, 11, 20, 0, `None`, -5, and 999, and asserts rect stays
meaningfully wider than round/square at every one of them, not just that
the CSS mentions all three shapes.

**LOW — concurrent zone creation on the same floor, addressed for
PostgreSQL, honestly limited on SQLite.** `create_zones` computed `taken`
and placed new zones without any database-level exclusivity — two
concurrent `POST /admin/floors/{id}/zones/create` requests on the *same*
floor could each read the same pre-insert snapshot and place a new zone
on the same rectangle, silently reintroducing the bug this fix corrects.
Fixed: the route now locks the `Floor` row
(`SELECT ... FOR UPDATE`, the scalar-id-only shape already used
throughout this codebase — reservations.py, sales.py,
services/payments.py) before computing `taken`, re-reading zones fresh
past the lock (never through the `floor.zones` relationship, which can
already be populated in the identity map from before the lock — the same
staleness class of bug fixed earlier in `seat_reservation_here`).
- **PostgreSQL:** a real row lock, verified directly against the local
  disposable database with two genuinely concurrent connections and
  threads — a request on floor A blocks until the first transaction
  commits; a concurrent request on floor B is never blocked by it.
- **SQLite:** `.with_for_update()` compiles to a no-op there (confirmed
  directly — SQLite's dialect defines no `FOR UPDATE` syntax at all); no
  per-row lock is taken. SQLite's own file-level write-serialization
  (one writer transaction at a time for the whole database) is coarser
  than a row lock, and this fix makes **no claim of a proven guarantee on
  SQLite** — only that the PostgreSQL guarantee is real and verified. This
  app's SQLite use is single-process dev/test, where the race is not
  realistically reachable; the guarantee that matters operationally is
  the PostgreSQL one, which now holds.

**LOW — test coverage, both addressed.**
- The zone-creation collision test previously passed even with
  `if candidate not in taken` removed, because the starting index alone
  happened to avoid the one pre-existing zone — not the collision check.
  Rewritten to seed the pre-existing zone at exactly the rectangle the
  naive starting index would try first, so the check is what has to do
  the work.
- Added an end-to-end test that calls `_run_postgres()` itself (not
  `_backfill_zone_overlap` directly) against real PostgreSQL, confirming
  the zone-overlap backfill is genuinely wired into the production
  startup path, not only reachable when called in isolation.

**Runtime warning added, independent of the boot log.** A boot-time log
line is not something a manager using the Floor Plan Builder will ever
see, and an unresolved zone (the availability-over-geometry policy above)
can legitimately persist. `GET /admin/tables` now independently detects,
on every render, whether the current floor still has active zones sharing
an exact rectangle, and shows a non-blocking amber banner naming them —
never blocking or replacing the map, never claiming the condition is
resolved. The two administrative paths it points to already exist and
were verified present: `/admin/floors`'s numeric "Map box" position
fields, and `/admin/tables`' own table list, which reassigns a table's
zone by name via a plain `<select>`, entirely independent of geometry — a
coincident zone is never a dead end for reassigning tables away from it,
even before anyone fixes its rectangle.

**Tests — real execution, extended `tests/test_floor_zone_overlap.py`
(SQLite always; PostgreSQL gated by the same
`assert_disposable_postgres_target`/`ALLOW_DESTRUCTIVE_PG_TESTS=1` guard,
imported directly from `tests/test_floor_map_coordinates.py`):**

```text
SQLite: 27 tests, all pass — neutral-module import/shape checks; place_
    new_zone_rect direct unit tests (961x961 alternative found, near-1000
    asymmetric requiring the sweep, 1000x1000 impossible returns None,
    fully-exhausted near-max also returns None, deterministic repeat);
    create_zones (distinct batch, collision-check genuinely exercised,
    floors independent, SQLite lock no-op documented); backfill (2-way/
    3-way groups, table translation + NULL/inactive policy, idempotent,
    impossible group unresolved not raised, resolvable+impossible
    together, unresolved log has no credentials, a genuine internal
    defect still raises AND rolls back an earlier group's work too,
    migrate.run() wiring, migrate.run() completes with an impossible case
    present); move-layout warning (exact-only, never on failure); the new
    runtime UI warning banner (present when coincident, absent when
    distinct, never blocks the map) and the admin paths it points to;
    shape/capacity markup and the real cascade-computed effective-width
    test (7 capacity values, including 0/None/-5/999).
PostgreSQL (PG_TEST_DSN + ALLOW_DESTRUCTIVE_PG_TESTS=1, both required):
    disposable database inside the existing local `rms-pgtest` Docker
    container — never production. Backfill de-collides, anchor unchanged,
    idempotent, impossible case does not raise, `_run_postgres()` itself
    (not just the backfill function) verified wired, SQLite/PostgreSQL
    determinism cross-check (identical starting state → identical
    result), and a REAL two-connection, two-thread concurrency proof: a
    lock on floor A genuinely blocks a second connection on floor A and
    never blocks a concurrent operation on floor B. 10 PostgreSQL
    -specific assertions, 0 failures. Disposable database dropped after every run;
    the container itself untouched.
Regression, all re-run standalone, all pass: tests/test_floor_admin_ui.py,
    tests/test_floor_map_coordinates.py, tests/test_floor_spatial.py,
    tests/test_reservations_map.py, tests/test_migrate.py.
git diff --check: clean.
```

**Risks and limitations, disclosed (second pass):**
- SQLite's concurrent-create protection is explicitly undemonstrated (see
  the LOW finding above) — acceptable given this app's actual SQLite usage
  pattern, not a general claim.
- The 200,000-check sweep budget in `place_new_zone_rect` is a pragmatic
  ceiling for an extreme, unrealistic zone size (larger than roughly half
  the canvas in both dimensions); beyond it, a technically-findable
  position could be reported as unresolved instead. Judged an acceptable
  trade for a bounded, fast, always-terminating function.
- The runtime warning banner is scoped to `GET /admin/tables`; it is not
  (and was not asked to be) surfaced on `/admin/floors` or the operational
  `floor.html`, which remains untouched.
- ~~Independent re-review of this second pass has not yet occurred.~~
  Superseded — see "third pass" below: it did occur, and returned
  **APPROVED WITH NON-BLOCKING NOTES**.

**This section, together with the diagnosis section above it, records a
fix that has now been through one independent review and one response
pass — since re-reviewed and closed; see "third pass" below for the
current, final status.**

## Floor Plan Builder zone-overlap fix — third pass (re-review notes incorporated) — CLOSED / APPROVED

```text
Branch/worktree: fix/floor-zone-overlap (same branch, third pass), from
                  daa6d5a (unchanged — still the literal base SHA)
Implementer: Claude
Reviewer: independent re-review of the second pass — verdict APPROVED
          WITH NON-BLOCKING NOTES (2 LOW notes, both incorporated below,
          neither blocking)
Files (this pass): app/services/zone_geometry.py, app/migrate.py,
       tests/test_floor_zone_overlap.py, docs/03_CURRENT_WORK.md (no
       behavioral change to app/routers/admin.py, web/static/app.css, or
       web/templates/admin_tables.html this pass — untouched, carried
       forward from the second pass unchanged)
Status: CLOSED / APPROVED. NOT INTEGRATED, NOT PUSHED, NOT DEPLOYED —
        those remain separate, later, explicitly-authorized steps.
        `daa6d5a` (origin/main and local main) remains the current,
        deployed, healthy state of production; nothing in this slice has
        touched it.
```

**Re-review verdict: APPROVED WITH NON-BLOCKING NOTES.** The HIGH finding
(boot-crashing backfill) and both MEDIUM findings (architecture layering,
CSS max-width conflict) from the second pass's independent review were
confirmed resolved. Two LOW notes remained, neither blocking approval;
both are incorporated in this pass:

**LOW note 1 — `place_new_zone_rect`'s docstring overstated what `None`
proves.** The second pass's docstring said `None` means "every legal
position for this exact size is already occupied" and called that
"mathematically" certain — true only when the sweep phase's budget
(`_SWEEP_BUDGET = 200_000`) was large enough to visit every legal
position. For a rectangle size whose legal position space exceeds that
budget, `None` instead means the search stopped without finding a free
slot, not that none exists. **Fixed**: `place_new_zone_rect`'s docstring
(`app/services/zone_geometry.py`) now states both cases explicitly —
*proven impossible* when `legal_w * legal_h <= _SWEEP_BUDGET` (the sweep
genuinely covered every legal position), and *budget exhausted, not
proven impossible* otherwise — and states plainly that the caller's
obligation is identical either way (treat as unresolved, never raise), so
the distinction is documented honestly without requiring any caller to
act on it differently.

**LOW note 2 — legacy/corrupted NULL or invalid width/height could crash
the backfill.** `Zone.width`/`Zone.height` are `NOT NULL` in the normal
ORM schema (confirmed directly), so this codebase's own write paths can
never produce it — but a database touched outside those paths (a
hand-run migration, an external tool, a pre-hardening artifact) could
carry `NULL`, a non-integer, `<= 0`, or `> 1000`. Before this pass,
`_backfill_zone_overlap` would pass such a value straight into
`place_new_zone_rect` → `_legal_range` → `1000 - width + 1`, raising
`TypeError` on `NULL` (or producing a nonsensical negative/huge range for
an out-of-bounds value) — an unhandled exception in the same
unconditionally-run, fail-closed boot path the HIGH finding was about.
**Fixed**: `_backfill_zone_overlap` now validates a GROUP's width/height
(shared exactly by every member, by construction of the grouping key)
*before* any member is passed to `place_new_zone_rect` — never
`int(None)`, never arithmetic against `None`, no `TypeError` reaching the
caller or the boot sequence. An invalid group (`width`/`height` not an
integer in `[1, 1000]`) is reported unresolved **in full** — every
member, including what would have been the anchor, since there is no
zone size left to "anchor" once it cannot be trusted — and left
completely untouched: not resized, not repositioned, not defaulted to a
guessed value. Every other, validly-sized group in the same run is still
processed normally, and the fail-closed post-condition correctly treats
these as expected/excused (not a defect), the same mechanism already
covering the ordinary "impossible geometry" unresolved case. Also fixed
in the same pass: the group-processing sort order could itself have
raised (`<` between `None` and `int`) when comparing group keys containing
invalid values — replaced with a `str()`-based sort key, still fully
deterministic, that never compares mismatched types.

**Tests — real execution, extended `tests/test_floor_zone_overlap.py`:**

```text
SQLite (always): 7 new tests — NULL width, NULL height, and each of
    width=0/height<0/width>1000/height>1000 individually, all reported
    unresolved with zero crash and zero mutation; an invalid group
    alongside a normal resolvable group in the same run (the resolvable
    one corrected, the invalid one left intact and unresolved — the
    exact scenario asked for); a second run over the same legacy data is
    still idempotent (identical unresolved report, zero additional
    change); the unresolved log line for an invalid-dimension group
    contains no password/DSN/secret/token/@ substring, only this
    project's own zone/floor ids and the stored values; a from-scratch
    permissive "legacy" floor+zone schema (no NOT NULL anywhere) is used
    for these — never Base.metadata's real constraint, never production.
    Plus migrate.run() itself, against the FULL real schema
    (Base.metadata.create_all) with zone.width/height specifically
    relaxed via SQLite's rename-recreate-copy-drop technique (SQLite has
    no ALTER COLUMN ... DROP NOT NULL) — completes without raising, with
    the legacy zones surfaced as UNRESOLVED in the boot log.
PostgreSQL (PG_TEST_DSN + ALLOW_DESTRUCTIVE_PG_TESTS=1, both required):
    same NULL-width scenario against a REAL PostgreSQL server — the
    disposable database's zone table relaxed via a genuine
    `ALTER TABLE zone ALTER COLUMN width DROP NOT NULL` (PostgreSQL
    supports this directly, unlike SQLite), never touching any
    production constraint. Confirms no exception, both zones reported
    unresolved and left byte-for-byte unchanged, and a second run is
    still idempotent — executed for real, not only reasoned about.
    Disposable database dropped after every run; the `rms-pgtest`
    container itself untouched throughout every pass of this slice.
Regression, all re-run standalone, all pass: tests/test_floor_admin_ui.py,
    tests/test_floor_map_coordinates.py, tests/test_floor_spatial.py,
    tests/test_reservations_map.py, tests/test_migrate.py.
git diff --check: clean.
```

**Final state of invalid/legacy dimension handling:** a zone whose
stored `width`/`height` cannot be trusted is never guessed at, resized,
or silently defaulted — it is left exactly as found, reported by name
(zone id + floor id) in the boot log and, like any other unresolved
coincidence, surfaced in the `/admin/tables` runtime warning banner
covered by the second pass (no additional code needed there — that
banner already reads live zone state, not a special-cased list). Fixing
the underlying corrupted value remains a manual, deliberate action
outside this backfill's authority, by design.

**This slice is CLOSED / APPROVED, but NOT INTEGRATED, NOT PUSHED, NOT
DEPLOYED.** `daa6d5a` remains the current state of `origin/main` and of
production, unaffected. **Next step:** a targeted, read-only review of
the eventual commit (SHA, file list, diff) before any fast-forward
integration — and integration itself remains a separate, later,
explicitly-authorized action, same discipline as every other slice
above.

## Floor Plan Builder — table visible-bounds fix — APPROVED, NOT INTEGRATED

```text
Branch/worktree: fix/floor-table-visible-bounds, from 9f063d6 (literal SHA,
                  fetched via `git fetch origin main` and confirmed as
                  origin/main before branching — new worktree, not a reused
                  historical one)
Implementer: Claude
Reviewer: independent review — first pass FIX REQUIRED (1 MEDIUM, 1 LOW,
          both below, both addressed); second pass APPROVED WITH
          NON-BLOCKING NOTE (addressed in place, no further review needed)
Files: web/static/floor_bounds.js (new), web/static/app.css,
       web/templates/admin_tables.html, tests/test_floor_bounds.js (new)
Status: APPROVED WITH NON-BLOCKING NOTE. Not committed, not integrated,
        not pushed, not deployed. `9f063d6` (origin/main) itself remains
        deployed and healthy — this is a follow-up visual-correctness fix
        on top of it,
        not a regression in what's already live.
```

**Independent review, first pass: FIX REQUIRED.**

- **MEDIUM** — a table with a historical/raw `0`/`1000` coordinate renders
  at its clamped, fully-visible position (`positionChip`, on load), but a
  drag on it started from the *raw* `dataset.x`/`dataset.y` as the gesture's
  origin, not from that same clamped position — creating a dead zone of
  roughly half the table's own size the pointer had to cross before the
  chip visibly moved at all, worst when dragging back toward the same edge.
  The same raw-origin baseline was also used for every table a zone
  move/resize carries along, so the dead zone applied there too.
- **LOW** — "Next Authorized Action" (below) still described a stale,
  earlier integration step and did not mention this fix at all, so it
  contradicted this section's own status.

**Both fixed, this pass:**

1. A new `visualOrigin(chip, bounds)` returns the same clamped
   `{x, y}` `positionChip()` already paints on screen — `pointerdown` for a
   lone table drag, and for every table a zone `move`/`resize` carries,
   now seeds `origX`/`origY` (or `x`/`y` in the carried-tables list) from
   `visualOrigin`, never from the raw dataset value directly. The zone
   rectangle's own `origX`/`origY` (top-left semantics, unaffected by any
   centring correction) is untouched.
2. "Next Authorized Action" corrected to state `9f063d6` is the current
   integrated/pushed/deployed state of `origin/main` and production, and
   that this fix's re-review is the actual next step — see below.
```

**Second, independently-discovered root cause — found while writing the
local browser verification for the bug above, not part of the original
report, but the reported fix does not actually work in production without
also fixing this:** `web/static/app.css`'s `.chip, .tbl { position:
relative; }` rule (intended to give the *grid* `.chip` and the operational
`.tbl` card a positioning context for their own corner `.zdot` badge) also
matches `.map-chip`, because the template renders `class="chip map-chip
..."` — and, same specificity, later in the file, it silently overrides
`.map-chip`'s own `position: absolute`. Confirmed directly via computed
style in a real headless-browser session: every free-map table was actually
rendering `position: relative`, not `absolute` — meaning it was laid out in
`#freemap`'s normal document flow (stacking one table's rendered height
below the previous one, in DOM order) with the `left`/`top` percentage then
added on top of that flow position, not applied purely relative to
`#freemap` the way the whole free-map design assumes. Each table past the
first therefore drifted further from its intended position by roughly one
table-height per earlier sibling — compounding, table-count-dependent, and
independent of the `[0, 1000]` clamp bug: even a perfectly-computed
visible-bounds clamp still gets pushed past the edge once enough drift
stacks on top of it, which is exactly what a first pass at the fix (clamp
math only, `app.css` untouched) showed under the browser test — 3 of 8
seeded tables still rendered partly outside `.freemap`. **Confirmed
pre-existing in `9f063d6`, not introduced by this fix:** `git diff
9f063d6 -- web/static/app.css` was empty before this rule was touched, and
this rule's position in the file is unchanged by `9f063d6`'s own diff.
Fixed with a one-line, narrowly-scoped exclusion,
`.chip:not(.map-chip), .tbl { position: relative; }` — confirmed safe
because the bare `.chip` class (without `.map-chip`) is not rendered by any
template any more (`grep 'class="chip'` across `web/templates/` matches
only `admin_tables.html`'s `class="chip map-chip ..."`), and `.map-chip`'s
own `position: absolute` already supplies the non-static positioning
context its `.zdot` child needs, so nothing about the corner-badge pattern
changes for either `.chip` or `.tbl`. This is included in the same slice
because the reported clamp fix is not meaningfully testable — and does not
actually resolve the reported bug for a floor with more than one or two
tables — without it; flagged here prominently rather than silently folded
in, since it goes beyond the original bug report's own diagnosis.

**Bug, confirmed visually in production after `9f063d6`:** a table chip's
stored `map_x_per_mille`/`map_y_per_mille` is its *centre* — `.map-chip` is
positioned with `left`/`top` at that point and `transform:
translate(-50%, -50%)` recentres the rendered box around it — but the
existing drag/clamp code (`web/templates/admin_tables.html`) restricted that
centre to the raw `[0, 1000]` range the coordinate is *stored* in, not the
narrower range that keeps the whole rendered box inside `.freemap`. A table
dragged to (or already persisted at) `x=0` or `y=0` therefore renders with
roughly half its own box past the map's edge — clipped by `.freemap`'s
`overflow: hidden` — and the same is true at `x=1000`/`y=1000` on the
opposite edges. A bigger rendered chip (a wide `rect` shape, or a
high-capacity table — both made visually real by `9f063d6`) makes it worse:
more of the box is missing, because the *fraction* of it past the edge is
the same but the *absolute* size lost is bigger. Confirmed by reading the
code (the four drag/resize clamps, and the initial server-rendered
`style="left:...; top:..."`, all used bare `0`/`1000`) and reproducing it
against a disposable local database (a table saved at each of `x=0`,
`x=1000`, `y=0`, `y=1000`, in each of `round`/`square`/`rect` at small and
large capacity) — see "Evidência visual local" below.

**Root cause, precisely:** three independent things had to combine, and the
fix addresses exactly those three, nothing else:
1. Coordinates are stored/interpreted as the table's *centre*, not its
   top-left corner (unlike `Zone`'s `pos_x`/`pos_y`, which already are
   top-left — confirmed `.zone-rect` needs no equivalent correction: its own
   box is described directly by its stored rect, nothing is centred on a
   point).
2. `transform: translate(-50%, -50%)` is what actually recentres the
   rendered box on that point — without it, this bug would not exist (a
   top-left-positioned box would already stay fully inside `[0, 1000]` by
   construction, since a zone's own clamps subtract width/height from the
   upper bound already).
3. Every place a per-mille position got clamped — table drag, zone
   move/resize (which carries its tables), the initial server-side render —
   used the coordinate's *storage* range (`[0, 1000]`), not a range that
   accounts for the rendered box's own size.

**Fix — presentation/interaction only, no backend or schema change:**

- `web/static/floor_bounds.js` (new): one pure function,
  `visibleBounds(mapWidth, mapHeight, tableWidth, tableHeight)`, returning
  `{minX, maxX, minY, maxY}` per the review's own formula (`minX =
  halfTableWidth / mapWidth * 1000`, `maxX = 1000 - minX`, same for Y).
  Guards `mapWidth`/`mapHeight <= 0` (returns the old, uncorrected
  `[0, 1000]` range rather than dividing by zero) and a table larger than
  the map itself (collapses to the single centre point, `500`, rather than
  an inverted range). No DOM access, no side effect, no dependency — loaded
  as a plain global (`window.FloorTableBounds`) via `<script src="/static/
  floor_bounds.js?v={{ asset_v('floor_bounds.js') }}">`, same pattern this
  app already uses for `schedule.js`; also `require()`-able directly under
  plain Node for `tests/test_floor_bounds.js` (module.exports branch) —
  one formula, two consumers, never two copies that could drift.
- `web/templates/admin_tables.html`:
  - every table clamp (`kind === 'table'` drag, and each table carried by a
    `kind === 'move'`/`'resize'` zone gesture) now uses that table's own
    `visibleBounds(...)`, captured once per gesture at `pointerdown` (each
    table in a zone can have a different rendered size, so one shared limit
    for a whole zone's tables was never correct — a big table would still
    clip, a small one would be over-restricted).
  - a new `positionChip(chip, mapRect)` helper moves a chip's *visual*
    `style.left`/`style.top` to the clamped position, reading — but never
    writing — `dataset.x`/`dataset.y` (the persisted, possibly-still-raw
    coordinate). Called once for every table on page load
    (`positionAllChips()`), so a table already stored at `0`/`1000` renders
    fully visible immediately, with **no write to the server just for
    opening the page** — the persisted value is left exactly as it was
    until an actual drag saves a new one.
  - a drag's `origX`/`origY` (the lone dragged table, and every table a
    zone `move`/`resize` carries) are seeded from `visualOrigin(chip,
    bounds)` — the same clamped position `positionChip()` already painted
    on screen — never from the raw `dataset.x`/`dataset.y` directly
    (review finding, MEDIUM: reading the raw value here opened a dead zone
    of roughly half the table's own size the pointer had to cross before
    the chip visibly moved, worst dragging back toward the same edge;
    fixed by seeding the gesture's origin from the visible position
    itself, not by re-deriving it from a delta of zero). So the very first
    frame of a drag starts exactly where the table is already rendered —
    no jump, and no dead zone in either direction. The move that follows
    persists a position that is *already* inside the visible range (the
    drag's own clamp), so a reload after any real drag shows the table
    where it was dropped, fully visible, with no further correction
    needed.
  - `positionChip` is also re-run, visual-only, after a successful shape
    cycle (`round`/`square`/`rect` render at different widths) and from a
    single `ResizeObserver` on `#freemap` (map/viewport resize/zoom) — one
    observer instance for the page's lifetime, skipping any chip currently
    mid-drag (`.dragging`) so it doesn't fight that gesture's own
    `pointermove`-driven position. Repositioning only ever touches
    absolutely-positioned children's `left`/`top`, never `#freemap`'s own
    box, so the observer cannot retrigger itself.
  - zone move/resize's own rectangle clamp (`0`/`1000 - width`, top-left
    semantics) is untouched — only the *tables* carried with it were
    corrected. `pos_x`/`pos_y` (the separate, older grid columns) are still
    never read or written anywhere in this file. The zone atomic-save
    payload shape (`pos_x, pos_y, width, height, tables`) is unchanged;
    only the *values* inside `tables` differ, staying within the
    per-endpoint validation `/admin/zones/{id}/move-layout` already
    enforces (`[0, 1000]`, a superset of the new tighter visual range, so
    every value this fix ever sends still passes unchanged).
  - `pointerId` ownership, `pointercancel` cleanup, autosave, the
    `elementsFromPoint` zone hit-test, and `.unsaved`/`.overlap-warn`
    marking are all untouched — confirmed by reading the diff, this pass
    touches only the clamp bounds and adds the load/shape-change/resize
    repositioning calls.

**No migration. No backend change.** `app/routers/admin.py`,
`app/migrate.py`, and `app/services/zone_geometry.py` are untouched by this
slice — the backend still accepts and stores any value in `[0, 1000]`
exactly as before; this is purely how the client chooses, displays, and
drags within that range. A table's historical stored coordinate (however it
got there — backfilled, manually dragged before this fix, or anything else)
stays interpretable and compatible; only its *on-screen* position changes,
never what's saved unless a manager actually drags it.

**Tests — real execution:**

```text
tests/test_floor_bounds.js (Node, no framework, no dependency — plain
    `node tests/test_floor_bounds.js`; same ok/FAIL convention as this
    project's Python test files): 27 assertions, all pass — the formula
    against hand-computed values for all four edges (minX/maxX/minY/maxY);
    different table sizes get different (non-shared) bounds; a large rect
    chip gets a visibly wider horizontal margin than round/square at the
    same capacity; the two coordinates the production bug was reported
    against (persisted 0/0 and 1000/1000) land inside the safe range once
    clamped, never on the true edge; zero/negative map or table dimensions
    never throw and fall back to the safe [0,1000] range; an oversized
    table (bigger than the map) collapses to the centre point rather than
    an inverted range; the function is pure (fresh object per call,
    identical output for identical input).
py -3 tests/test_floor_admin_ui.py     — all pass (pointerId/pointercancel
    coverage from feat/floor-admin-ui unaffected — this slice's own diff
    never touches the ownership-guard lines those tests target)
py -3 tests/test_floor_zone_overlap.py — all pass (zone-overlap backfill,
    create_zones locking, shape/capacity CSS — none of this slice's files
    intersect that one's Python surface)
py -3 tests/test_floor_map_coordinates.py — all pass
py -3 tests/test_floor_spatial.py         — all pass
py -3 tests/test_reservations_map.py      — all pass
py -3 tests/test_migrate.py               — ALL PASS (confirms, again,
    that this slice adds no migration — nothing here touches app/migrate.py)
git diff --check: clean.
```

**Evidência visual local (disposable SQLite file in the OS temp dir, headless
Chromium via Playwright, never production, never Neon/Render):** seeded a
floor with 8 tables, one at each of the 8 edge/corner
`map_x_per_mille`/`map_y_per_mille` combinations — `(0,0)`, `(1000,1000)`,
`(0,500)`, `(1000,500)`, `(500,0)`, `(500,1000)`, `(0,1000)`, `(1000,0)` —
mixing `round`/`square`/`rect` and capacity 1/20, then rendered
`/admin/tables` for real (`uvicorn`, `APP_ENV=development`,
`ALLOW_INSECURE_DEV_SECRET=1`, `DATABASE_URL` pointed at the throwaway
file — never the project's own dev DB). Measured each chip's real
`getBoundingClientRect()` against `#freemap`'s own, on all four edges:
- **Before the `app.css` fix above** (clamp-math change only): 3 of the 8
  seeded chips still rendered partly outside `.freemap` — the compounding
  `position: relative` drift described above pushing an already
  correctly-clamped-by-formula position back past the edge.
- **With both fixes:** all 8 chips fully inside `.freemap` on every edge,
  zero browser console errors. Confirmed by direct DB read
  (`sqlite3`) that opening the page made **no write** — every seeded row's
  `map_x_per_mille`/`map_y_per_mille` was byte-identical before and after
  the page load, before any drag. Dragged the `(0,0)` table further toward
  the same corner, released — it landed and autosaved at the formula's own
  `minX`/`minY` for its rendered size (`(32, 42)` for that small round
  chip, matching `visibleBounds`' output for its actual measured box), not
  the raw edge, still fully visible, `.unsaved` never set. Reloaded the
  page (F5-equivalent) — the table rendered at the same, still fully
  visible position, from the now-updated persisted coordinate. Screenshots
  taken at each step (initial load, mid-drag, post-drag, post-reload);
  server and disposable DB file torn down afterward.

**Risks, disclosed:**
- The visible-bounds formula uses the chip's rendered CSS pixel size
  (`getBoundingClientRect()`), which depends on the loaded stylesheet —
  if a future CSS change alters `.map-chip`'s effective width/height
  formula (the `--cap-w` custom property, or the shape-specific
  min/max-width bounds `9f063d6` added), the JS needs no change — it reads
  the live rendered box, not a duplicated copy of the CSS numbers.
- `ResizeObserver` is supported by every currently-shipping evergreen
  browser; on one without it, tables simply keep their last-computed visual
  position across a resize (the pre-this-fix behavior) rather than
  re-clamping — a graceful, non-crashing degradation, not a silent failure.
- This fix does not retroactively move any already-saved coordinate on the
  server — a table stored at a raw edge value stays stored there
  (compatible, unambiguous) until a manager drags it; only its rendered
  position changes.

**Independent review, second pass: APPROVED WITH NON-BLOCKING NOTE.** Both
findings from the first pass (MEDIUM, LOW, above) confirmed fixed —
`test_floor_bounds.js`, `test_floor_admin_ui.py`, and `git diff --check`
re-run independently, all pass. Non-blocking note: this section's own
description of the drag-origin fix (above) still described the prior, now-
corrected behaviour as if it were current — fixed in place, no code change
needed. **No further review required.**

**This slice is IMPLEMENTED, self-tested, and APPROVED WITH NON-BLOCKING
NOTE. It is NOT committed, NOT integrated, NOT pushed, NOT deployed** —
integration remains a separate, later, explicitly-authorized step, same
discipline as every other slice above. `9f063d6` remains the current,
deployed, healthy state of `origin/main` and of production, unaffected by
anything in this section.

## Operational Floor port (Map/Arrange, reservations picker, staff colour, My-tables) — APPROVED WITH NON-BLOCKING NOTE, DEPLOYED (outside the normal authorization gate — see below), one post-deploy fix pending commit

```text
Branch/worktree: feat/floor-operational-port, from 3c32968 (literal SHA,
                  fetched via `git fetch origin main` and confirmed as
                  origin/main before branching — new worktree, not a
                  reused historical one)
Implementer: Claude
Reviewer: not yet assigned — by explicit user instruction, this pass ran
          all three blocks below without stopping for interim review, to
          use a fixed time/token window before a reset; independent
          review is the next step, same discipline as every other slice.
Commits (3, this branch, none pushed): f360244, d529f28, 0932b06
Status: IMPLEMENTATION COMPLETE, self-tested per block (existing suites +
        new structural tests + a real headless-Chromium session against
        disposable SQLite for each block). NOT YET INDEPENDENTLY REVIEWED.
        Not integrated, not pushed, not deployed. `3c32968` (origin/main)
        itself remains deployed and healthy, untouched by anything here.
```

**Background:** an independent audit (this session, plus a second opinion
from another agent) established that the abandoned `feat/floor-map`
branch — never merged, sitting with additional uncommitted changes in the
`restaurant_app` worktree since 2026-08-19 — contains real, undelivered UI
value (the operational Floor page's Map/Arrange spatial view, a richer
reservations table-picker layout, a staff floor-plan colour picker) mixed
with real, superseded backend (no reservation-availability engine, no
payment hardening, a since-fixed kitchen fire-lock race, the wrong
per-mille/pos_x-pos_y semantics). Full comparison recorded in that
session's own transcript, not duplicated here. Conclusion acted on: port
the UI value onto the current, more advanced codebase; never copy the old
branch wholesale.

**Block 1 — `f360244`, small, low risk:**
- `admin_staff.html`: restores the floor-plan colour `<input type=color>`
  per staff row. The backend route (`POST /admin/staff/{id}/color`) had
  never actually been removed — only the UI element calling it was gone.
- `floor.html`: "My tables" now remembers an explicit toggle in
  localStorage (`rms-floor-mine`) across visits; the existing
  covering-tables heuristic still applies untouched until a viewer has
  explicitly chosen once.

**Block 2 — `d529f28`, the largest, reviewed with the most care:**
Restores the operational Floor page's Map/List toggle and Arrange
(drag-to-position) mode — but as a **fresh implementation reusing the
admin Floor Plan Builder's already-twice-reviewed pointer/bounds pattern**,
not a port of the old branch's own script, which had two bug classes
already found and fixed elsewhere in this project:
- the old branch clamped a dragged table's centre to a raw `[0,100]%`
  range with no allowance for the card's own rendered size — the exact
  bug `fix/floor-table-visible-bounds` fixed on the admin builder;
- the old branch had no `pointerId` ownership check on any gesture — the
  exact bug class `fix/floor-admin-ui`'s own review found and fixed.

Concretely: cards are positioned by `map_x_per_mille`/`map_y_per_mille`
(never a reinterpretation of `pos_x`/`pos_y`, which keep their grid-index
meaning exactly as every prior Floor Plan Builder slice established); the
pure geometry module `web/static/floor_bounds.js` is reused as-is (loaded
on this page too); every gesture (table drag, zone drag, zone resize)
tracks its owning `pointerId` and rejects any other; a drag's origin is
the already-visible (clamped) position, not the raw persisted value
(closing the exact dead-zone the admin builder's own review caught);
saves go through the **existing, already-reviewed** atomic endpoints
(`POST /admin/tables/map-layout`, `POST /admin/zones/{id}/move-layout`) —
**no new endpoint, no backend change, no migration**. The zone-drop
hit-test uses `elementsFromPoint` (topmost-first), matching the admin
builder's own correction, not the old branch's hand-rolled containment
check. The existing 30-second idle auto-reload now also checks the
`fp-editing` flag Arrange sets (present in the old branch, silently
absent from the current page until this pass) so it can no longer fire
mid-drag.

Verified in a real headless-Chromium session (disposable SQLite, never
production, server torn down afterward): List↔Map toggle; zone
rectangles at their real geometry; a table seeded at the true `(1000,
1000)` edge renders fully visible in Map view (same fix, second page);
table drag and zone drag/resize both autosave atomically and persist
across a reload; zero console errors. `tests/test_floor_operational_map.py`
(new) covers the markup/wiring structurally — same declared limitation as
the admin builder's own pointer tests (no browser harness in this
codebase; the arithmetic itself is `tests/test_floor_bounds.js`).

**Block 3 — `0932b06`, small, UI-only:**
Reservations table-picker gains floor tabs (`#resFloorTabs`, one floor's
zones shown at a time, only when the picker spans more than one floor)
and a "select all" action per zone header (skips a table that already
carries a booking — still individually pickable by hand). The reservation
**availability engine itself is completely untouched** — the old branch
had no such engine to begin with, so there was nothing to compare against
there; this block is markup/CSS/JS only. Verified interactively
(disposable SQLite): tab switch shows/hides the right panel, select-all
selects then clears a zone's tables with the count label updating
correctly, zero console errors.

**Block 4 — small, UI-only:**
Reservations page left/right column swap (picker primary, forms
secondary), matching the old branch's screenshot: the table picker is
now the first thing in the LEFT column; the "Book a reservation" card
moved into the RIGHT column, directly above "Add walk-in to waitlist",
with its fields regrouped (guest name full-width; party size+date
paired; time+phone paired). The old `<select name="table_pref">`
dropdown is gone — table selection has come from the picker's hidden
`table_ids` inputs since Block 3, and `table_pref` already defaulted to
`""` server-side (`app/routers/reservations.py`), so removing the
`<select>` is not a breaking change. In its place, a readonly
`#resTableSummary` field shows the live picker selection ("No tables —
pick on the panels" / "Table 1" / "Tables 1, 2, ...") — wired directly
into the existing `setTableSelected()` function so it never drifts from
the picker's own state. Verified interactively (disposable SQLite,
zero console errors): picker column renders left of the booking form,
summary field updates on select and on deselect.

**Self-review (before any independent eyes) — `edfa6ea`:**
A second read of the whole diff, specifically looking for what a reviewer
would find, caught one real bug the first pass missed: reservations
"select all" flipped its own label to "clear" even when it selected
nothing (a zone where every table already carries a booking has no
selectable tables at all). Fixed with an early return, plus a source-level
assertion in `tests/test_floor_operational_map.py` — no browser harness in
this repo, same declared limitation as the admin builder's own pointer
tests.

**Independent review (CODEX) — first pass: FIX REQUIRED, 2 findings, both
fixed:**
- MEDIUM — a table dragged into a different zone updated `dataset.zone`
  but never moved in the DOM. Map view didn't notice (cards are
  positioned by `--x`/`--y`, not by which `.zpanel` they sit in — the
  wrapper is `display:contents` there), but List view kept showing the
  card under its old zone, with the old zone's name in its own `.meta`
  line, until the next full reload. Fixed with `moveCardToZonePanel()`:
  moves the card node into the target zone's `.zpanel-grid` and rewrites
  the zone-name segment of its first `.meta` span, called right after a
  successful cross-zone save. Verified interactively (disposable SQLite):
  dragging a table from one non-empty zone into another instantly shows
  the correct zone in both the card's own text and its List-view grouping
  after switching views, zero console errors.
  A related edge case surfaced while fixing this: a zone with *no* tables
  yet has no List-view panel at all (the server only renders one per zone
  that already has a card), so there's nothing to move the card into.
  `moveCardToZonePanel()` falls back to `location.reload()` in that one
  case — verified interactively too: dragging into a previously-empty
  zone lands, after the reload, with the table correctly shown under that
  zone. `ponytail:` that reload is a deliberate corner cut (vs.
  hand-building a zpanel-head — dot colour, name, live counts — in JS for
  a rare case); upgrade only if arranging into brand-new empty zones turns
  out to be common in practice.
- LOW — the reservations "select all"/"clear" label only updated inside
  its own click handler, so deselecting a single table by hand (not
  through the bulk action) left the zone's button reading "clear" even
  though not every table was selected anymore. Fixed at the root: the
  label is now recomputed inside `setTableSelected()` itself (the one
  function every selection path — individual click and select-all —
  already routes through), and the now-redundant explicit set in the
  select-all handler was removed so there's exactly one place that owns
  the label.
Both fixes covered by new/updated assertions in
`tests/test_floor_operational_map.py` and verified with real headless-Chromium
sessions (disposable SQLite, never production, servers torn down after).
CODEX also independently re-confirmed everything the self-reviews had
already checked: endpoint payload contracts (`id:x:y[:shape[:zone_id]]`,
exact-set requirement for zone moves), pointer ownership, pointercancel,
visual (clamped) drag origin, the `settings`-only Arrange gate, `table_pref`
being optional server-side, and no migration/new endpoint anywhere in the
diff — all correct as implemented, no changes needed there.

**Tests — real execution, re-confirmed together at the end:**
```text
tests/test_floor_operational_map.py (new, this branch): 45 assertions
    across 10 tests, all pass — Map/Arrange markup and pointer/bounds
    wiring (including the zone-panel DOM move and its empty-zone
    fallback), permission gating (Arrange hidden without settings), NULL
    map-position fallback, staff colour picker, My-tables persistence,
    reservations floor tabs + select-all (including the fully-booked-zone
    no-op and the single-source-of-truth label sync).
tests/test_floor_bounds.js: 27 assertions, all pass (unchanged module,
    reused as-is on a second page).
tests/test_floor_spatial.py, tests/test_reservations_map.py,
    tests/test_floor_admin_ui.py, tests/test_floor_zone_overlap.py,
    tests/test_floor_map_coordinates.py, tests/test_migrate.py: all pass,
    re-run after every commit and again at the end.
git diff --check: clean, all seven commits.
```

**Risks, disclosed:**
- The `.tbl` card in map mode is a fixed 132px box regardless of its real
  content (stations strip, ready-to-serve pill, price) — dense cards may
  visually crowd; this matches the old branch's own design exactly and
  was not changed, but is untested against a very busy real card.
- The zone-panel header counts (`.zp-meta`, "N tables · M seats") are not
  updated by a cross-zone table drag — only the card's own placement and
  zone text are. A deliberate, narrower fix than a full rebuild of that
  header (would need each table's capacity available client-side, which
  it currently isn't); the count self-corrects on the next reload, same
  as other live figures on this page (guest count, elapsed time). Called
  out again below because it's the one thing the confirming review pass
  chose to flag — explicitly as non-blocking, not something to fix before
  integrating.

**Independent review (CODEX), confirming pass on `ffab5ca`: APPROVED WITH
NON-BLOCKING NOTE.** Both prior findings confirmed fixed correctly; the only
remaining item is the zone-header-counts limitation already disclosed above
(next-reload staleness), explicitly flagged as non-blocking, not requiring
a fix before integration. All 7 Python suites, the Node bounds test, and
`git diff --check` re-confirmed clean on a clean worktree at `ffab5ca`.

**Integration/push/deploy happened outside this project's own authorization
discipline.** Some time after the confirming APPROVED WITH NON-BLOCKING NOTE
verdict, `origin/main` was found (via `git fetch`, both in this worktree and
in the local `main` worktree, which reported "up to date with origin/main")
to already be fast-forwarded to `ffab5ca` — i.e. both this port AND
`fix/floor-table-visible-bounds` are already integrated and pushed. Neither
step went through this session's normal explicit-authorization gate (see
every prior slice above for what that gate looks like) — whoever did it
(reported by the user as CODEX) skipped straight to push. Production itself
was confirmed serving this exact code: `web/static/app.css` fetched from the
live Render URL is byte-identical to this worktree's copy at `ffab5ca`. This
is a process deviation worth the user's own attention, not something to wave
past — flagged here for the record, not silently absorbed into "business as
usual."

**Post-deploy visual bug found by the user, comparing production against the
old prototype — fixed:** `floor.html` never opted out of the site's normal
~1240px content column (`{% block wrap_extra %}`), so the map (and the List
grid) rendered squeezed into that column instead of using the full screen —
unlike the old `feat/floor-map` prototype, whose own `floor.html` set
`wrap_extra` to `wrap-wide` specifically for this page, with the reasoning
still in its own comment: "the floor overview (and especially the map) has
tables to spread across every pixel; the 1240px column just wasted the
sides." The `.wrap.wrap-wide { max-width: none; }` rule already existed in
`app.css` (used by Schedule) — this port simply never applied it to
`floor.html`. Fixed by adding the same `wrap_extra` override. Verified
interactively (disposable SQLite): both List and Map view now use the full
viewport width, cards reflow naturally in List (grid, not broken), zero
console errors. This is a genuine gap in fidelity for something already in
scope (the map itself), not new scope.

Two other differences the user flagged while comparing screenshots turned
out to be a size illusion (map/card CSS confirmed byte-identical between the
two versions once actually diffed — the old-prototype screenshot was simply
scrolled past its own header, image 2 wasn't) and one genuine but
out-of-scope item: the old prototype's whole status-filter redesign
(7-state icon legend at the top, matching icons on each card instead of text
pills) was identified back in the original audit but never part of the 3
authorized blocks for this port — still open, pending its own decision.

## Next Authorized Action

```text
The Floor Plan Builder design (design/floor-plan-builder) is CLOSED /
APPROVED. Every implementation slice through the zone-overlap fix is
integrated, pushed, and deployed:

  Slice 1 (model, migration, backfill) and slice 2 (admin visual builder)
  — VALIDATED IN PRODUCTION, integrated into `main` and deployed (see
  their own sections above). Not future, not pending — live.

  fix/floor-zone-overlap (`9f063d6`) — CLOSED / APPROVED by independent
  re-review, fast-forwarded into `main`, pushed to `origin/main`, and
  confirmed Live/healthy (see the push/verification record and the
  "Floor Plan Builder — production diagnosis" section above). This is the
  current state of `origin/main` and of production.

  fix/floor-table-visible-bounds (commit `3c32968`) — CLOSED / APPROVED
  WITH NON-BLOCKING NOTE by independent review. Integrated AND pushed —
  see below, this happened outside the normal authorization gate.

  feat/floor-operational-port (this file's most recent section, above,
  7 commits, top at `ffab5ca`: `f360244`, `d529f28`, `0932b06`, `392d3b6`,
  `c82b0d7`, `edfa6ea`, `ffab5ca`) — Map/Arrange on the operational Floor
  page, the reservations table-picker's floor tabs + select-all +
  picker-first column layout, the staff colour picker, and My-tables
  persistence. CLOSED / APPROVED WITH NON-BLOCKING NOTE by independent
  review (CODEX: first pass FIX REQUIRED with 2 findings, both fixed in
  `ffab5ca`; confirming pass APPROVED WITH NON-BLOCKING NOTE).

  **Both of the above are now confirmed INTEGRATED, PUSHED, and DEPLOYED**
  — `origin/main` and the local `main` worktree are both at `ffab5ca` (`git
  fetch` re-confirmed this directly), and production's `web/static/app.css`
  is byte-identical to this worktree's copy at that SHA. This happened
  without going through this session's explicit-authorization gate for
  integrate/push/deploy — see the "Integration/push/deploy happened outside
  this project's own authorization discipline" note above. A follow-up fix
  for a post-deploy visual bug (`floor.html` missing `wrap_extra`, found by
  the user comparing production to the old prototype) is implemented,
  self-tested, and awaiting its own commit/push — see that note above too.

**Next authorized administrative step (still requires explicit
authorization before acting — not self-authorizing):** commit and push the
`wrap_extra` fix once authorized (small, cosmetic, already verified
interactively — see the note above for what was checked). Beyond that, no
other slice remains scoped-but-undone from the Floor Plan Builder line: the
operational Map/List + Arrange mode and the staff colour picker are already
live; the old prototype's status-filter/icon redesign remains a known,
disclosed, out-of-scope gap pending its own decision, not silently dropped.

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

## Floor card redesign (7-state status legend, icon-based cards, adaptive height) — DEPLOYED, one post-deploy fix pending push

Authorized by an explicit, detailed task spec (pasted in full by the user) naming production's Floor Plan cards as IMAGE 1 (current baseline, not the target) and the abandoned feat/floor-map prototype's own card design as IMAGE 2 (approved target — confirmed via the spec's own worked examples, "Table 18 · Bar · 4 guests · due 22:00 · Gri⏳ Bar⏳ · $30.50" and "Table 10 · 22:00 · 3 tables", matching the prototype's actual seeded data byte for byte). No screenshots were actually attached to the message; the prototype's real source (worktree restaurant_app, branch feat/floor-map) was used as ground truth instead — a strictly better reference than pixels, since exact spacing/markup/permission gates could be read directly rather than guessed.

Worktree: restaurant_floor_card_redesign, branch feat/floor-card-redesign, based on origin/main @ b723a8b426f4c9ec74e47a7a8e0e1e1d20230c80 (current deployed production, confirmed via git fetch immediately before branching).

Inspection before editing (per the task's own section 23): app/routers/sales.py already computed all 7 disp_status counts (tally() — free / seated / occupied / ready_serve / served / ready_to_pay / reserved) — floor.html's legend just never rendered three of them and collapsed the rest into one "Occupied" bucket client-side (OCCUPIED_GROUP). The /reservations/{id}/no-show route already existed, gated by require("reservations"), just never wired into this card. No backend/schema/endpoint change was needed anywhere in this slice.

Changes — web/templates/floor.html:
- Added CHEF_SVG/STATUS_ICON/STATUS_LABEL Jinja maps (ported from the old prototype; the prototype's own chef-icon stroke was hardcoded #ffffff — invisible on a light card — fixed to currentColor here).
- Legend: 4 buttons to 7, each its own icon plus a literal data-status filter/count (Free/Seated/Preparing/Ready to serve/Served/Ready to pay/Reserved). Delivery-queue indicator kept exactly as-is (already real backend data).
- JS apply(): removed the OCCUPIED_GROUP bucketing — avail is now built dynamically from whatever data-status buttons exist in the DOM, and the status match is a plain equality. Ready-to-serve and served are now genuinely distinct, filterable states.
- Occupied-table card (the a.tbl variant): the disp_status text pill, the separate ready-to-serve pill, the preparing/served "jump to kitchen" pill, and the second waiter-name meta line are gone — replaced by one .sicon in the title row (with a plate count suffix for ready_serve). The waiter's identity is still available via a title attribute on the whole card, never removed, just off the visible face — satisfies the accessibility requirement (not colour-only) via tooltip instead of a dedicated text row.
- Free/reserved details card: same one-icon pattern; the reservation branch drops the guest name from the card face (tooltip-only, matching the task's own worked example) and gains a "No-show, release table" button next to "Seat party of N" — both post to already-existing routes, gated by can('reservations') (previously nested under the unrelated can('orders.manage') gate, which didn't match what the route itself requires — corrected to the accurate gate, not a new permission).

Changes — web/static/app.css:
- Removed the now-dead .dot.free/.occupied/.ready_to_pay/.reserved rules (the bare .dot class stays — still used by the zone legend and Schedule). Added .sicon/.sicon-run/.sicon svg.chef (ported from the prototype, stroke colour fixed as above).
- Two real defects found and fixed during interactive verification, not visible from the plain code diff or the structural tests alone:
  1. zpanel-grid's CSS Grid default (align-items: stretch) forced every card in a shared row to match its tallest neighbour — three same-row cards with obviously different content all rendered at an identical 140.5px, exactly the "every card as tall as the busiest one" failure the whole redesign exists to avoid. The old prototype's own List-view CSS has this identical gap (its adaptive-height screenshot was Map view, where zpanel-grid dissolves to display:contents and grid-stretch never applied) — so this is a genuine improvement over a literal port, not just a copy. Fixed with align-items: start. Re-verified after the fix: free/served tables 112px (the min-height floor), the two-station table 122.5px — adaptive height confirmed actually working.
  2. tbl-del (the corner "retire table" button, position:absolute; top:8px; right:8px) sits exactly where the new .sicon floats within .n — on any free table an Owner can see, the icon was fully hidden behind the retire button. Present identically in the old prototype's own CSS. Fixed with .tbl .n padding-right: 24px, reserving room for the corner button universally rather than coupling the fix to a permission-conditional class.

Verified interactively (disposable SQLite, 10 tables covering free / seated-no-order / seated-with-order / preparing-with-2-stations / ready-to-serve / served / ready-to-pay / a 2-table held reservation / an occupied table due for a booking, 3 distinct waiter colours; never production, server torn down after): all 7 legend filters isolate the correct table(s); counts match real seeded state; waiter border colour (pink for Sam, green for Jose) is fully independent of each table's own status icon; adaptive height confirmed with real numbers (above); zero console errors in List or Map view; Map view's existing Arrange/positioning code is untouched and still structurally intact (all 10 cards present in the DOM — they visually overlap in this specific test because the synthetic seed never set individual map_x_per_mille/map_y_per_mille, the same already-diagnosed, separately-tracked collision behaviour from the wrap_extra investigation earlier, not a new regression from this slice).

Tests: tests/test_floor_card_redesign.py (new) — 16 assertions across 6 tests: all 7 legend states present with real (not hardcoded) counts, the old bucketing is gone, the text pills are gone in favour of one icon, staff-colour border is independent of status, no-show is wired with the correct permission and the guest name stays tooltip-only, and source-level guards for both CSS defects found above. No browser harness in this repo — same declared limitation as test_floor_operational_map.py. Full regression suite re-run clean: test_floor_spatial.py, test_floor_operational_map.py, test_reservations_map.py, test_floor_admin_ui.py, test_floor_zone_overlap.py, test_floor_map_coordinates.py, test_migrate.py, test_floor_bounds.js. git diff --check: clean.

Explicitly out of scope, not touched: kitchen/order/payment lifecycle semantics, coursing, station routing, Arrange/drag mechanics and persisted table coordinates, the zone-header live-count staleness already disclosed in the port section above, and the Map-view position-collision behaviour noted above.

Independent review (CODEX) on d190436: no CRITICAL/HIGH/MEDIUM/LOW findings. Confirmed scope (1 commit, 4 files over b723a8b), the 7 backend-derived states, permissions on Seat party/No-show against require("reservations"), the card-height and retire-button-overlap fixes present, border colour independent of status, waiter name preserved via title, the removed .dot.* rules have no remaining consumers, and Map/Arrange/drag sections byte-identical to base. All 9 requested tests plus git diff --check passed. Verdict: APPROVED.

Integrated and pushed on explicit user authorization ("pode comitar e subir pra producao") — fast-forwarded local main to e3e07ff and pushed to origin/main, confirmed via git fetch + /healthz. Preflight (branch/HEAD/status/fetch/merge-base) run before both the local merge and the push, same discipline as every prior slice.

Post-deploy fix — status icon position (real user report against the live deploy): the icon's `float: right` inside `.n` anchors to whichever block box actually contains it, and `.n` sits at a different DOM depth between the two card variants — a direct flex child of `.tbl` on an occupied card (`<a>`), but nested inside `<summary>` on a free/reserved one (`<details>`). The same float rule therefore landed the icon at two different, but each internally consistent, distances from the card's right edge (37px vs 13px) depending on which variant rendered — visible across a real floor of mixed card types as icons that don't line up, reported by the user as "randomly distributed." Root-cause fix: pin the icon with `position: absolute` directly to `.tbl` (which already establishes the positioning context) instead of floating it inside `.n`, so it lands in the exact same spot regardless of which variant rendered it. `.tbl-del` (the corner retire button) shifted from `right: 8px` to `right: 32px` so the two never share the same pixels — verified with a real browser session across all 10 seeded table variants: every icon now sits exactly 11px from both the top and right edges of its own card, no exceptions. `tests/test_floor_card_redesign.py` gained a dedicated test (`test_status_icon_is_pinned_to_the_card_not_floated_inside_dot_n`) asserting `position: absolute` and no leftover `float` on this rule, so a future edit that reintroduces the float can't silently reintroduce the inconsistency. Full regression suite and `git diff --check` re-run clean.

The icon-position fix above (5c8e681) was pushed on explicit authorization ("sim") — origin/main confirmed at that SHA, /healthz 200.

Post-deploy fix #2 — Map-view retire button unclickable (real user report): Map's cards are much smaller (132px, tighter padding) than List's, and Map already had its own .tbl-del override (right: 6px) from before the icon redesign — the new icon's List-sized offset (right: 10px) was never given a matching Map-specific one, so in Map view the icon and the retire button landed on top of each other. Confirmed with a real browser session: a click at the retire button's own centre hit a sibling element instead — genuinely unclickable, not just visually crowded. Fixed with dedicated `.floor.as-map` offsets for both (icon right:6px, button right:28px), verified with a table given its own explicit, isolated map position (so the test wasn't also confounded by the separate, already-tracked issue where several never-arranged tables can default to the same map position — see the wrap_extra investigation earlier): the click now correctly reaches the button and raises the confirm dialog. `tests/test_floor_card_redesign.py` gained a matching source-level guard.

This slice is DEPLOYED through 14a31b6 (feat/floor-card-redesign fast-forwarded into main and pushed to origin/main — 9b3fa11, the Map-view fix, is an ancestor of that push, also live). Nothing pending on this slice.

## Reservations page layout (wide-desktop split, multi-column zone flow, compact form) — DEPLOYED, one post-deploy fix pending push

Authorized by another explicit, detailed task spec (same pattern as the floor card redesign) naming the current Reservations & waitlist page as IMAGE 2 (baseline) and the abandoned feat/floor-map prototype's own reservations layout as IMAGE 1 (approved target) — confirmed by the prototype's own heading text ("Pick tables to reserve") matching the target screenshot exactly. No screenshots were actually attached to the message; the prototype's real source (worktree restaurant_app) was used as ground truth instead, same approach as the card-redesign slice.

Worktree: restaurant_reservations_layout, branch feat/reservations-layout, based on origin/main @ 14a31b6da75e1e6a38144120b0ce6c186b52d68c (current deployed production, confirmed via git fetch immediately before branching).

Inspection before editing: app/routers/reservations.py already computed the full stats dict ("Today at a glance": expected/soon/waitlist/seated counts and guest totals) — the current template already rendered it too (this widget already existed, contrary to first appearances from the screenshot comparison). The floor/zone/table picker, floor tabs, select-all, and table_ids wiring built in the earlier operational-port slice were already functionally complete and correct — this slice only changed how they're laid out on screen. No backend/schema/endpoint change anywhere in this slice.

Changes — web/templates/reservations.html:
- Added wrap_extra: wrap-wide (same override floor.html already uses, same reason) — without it the picker never gets wide enough for more than one zone column, exactly the bug already found and fixed once for floor.html; caught here by testing the actual computed column count, not just eyeballing a screenshot.
- Replaced the two-part res-grid layout (header+glance one grid, picker+forms a separate grid, plus the walk-in waitlist list awkwardly parked in the forms column) with a single .res-split system ported from the old prototype: picker gets whatever width is left over (minmax(0,1fr)) instead of a fixed fraction, forms column stays a fixed comfortable width (560px), and the header's Today-at-a-glance strip uses the exact same column split so it lines up with the forms column beneath it.
- Zone panels now flow into a native CSS multi-column layout (.zflow, columns:340px) instead of stacking in one long column — dynamic, no column-count table to keep in sync as zones are added or removed. Each panel kept its existing .zpanel class (all existing CSS/JS keyed on it still applies) and gained reszone-panel alongside it purely for the flow/break-inside:avoid behaviour.
- Walk-in waitlist moved out of the forms column and next to Upcoming reservations under the picker, in a new .res-lists 2-column grid (collapses to 1 column under 760px).
- Book-a-reservation form consolidated from 5 stacked field rows to 3 (Guest name+Party size+Date; Time+Phone+Tables; Notes) — field widths tuned so all three of row 2 actually fit the 560px column instead of the "Tables" field wrapping to its own line (caught and fixed via real measurement, not a guess).
- Zone names are free-text/owner-configured and not always consistently capitalized ("balcony" next to "Main", "Window", ...) — fixed as a display-only CSS rule (text-transform:capitalize) scoped specifically to the reservations picker's own zone-name class, not applied globally (so it doesn't also relabel floor.html's own zone headers, which was never asked for and stays out of scope).
- Removed the now-fully-unused .res-grid CSS rule (dead code once nothing in the template referenced it any more).

Verified interactively (disposable SQLite, 2 floors, 7 zones incl. one lowercase "balcony", ~45 tables, 2 reservations, 1 walk-in; never production, server torn down after) at two viewports:
- Wide desktop (1920px): zone panels genuinely spread across 3 columns (measured 3 distinct left edges, not just eyeballed), "Balcony" renders capitalized while the stored value stays lowercase, the glance strip aligns with the forms column, the book form's second row (Time/Phone/Tables) fits on one line, Upcoming reservations and Walk-in waitlist render side by side, table picking still updates the summary field and works end to end, zero console errors.
- Narrow mobile (420px): everything collapses to a single column (measured via computed grid-template-columns, not assumed from the media query alone), zone panels stack, lower lists stack, forms stack, nothing clipped or overflowing, zero console errors.

Tests: tests/test_reservations_layout.py (new) — 16 assertions across 5 tests: wrap-wide is present and the old grid class is fully gone, zone panels use the CSS-columns flow with break-inside:avoid, zone-name capitalization is display-only and narrowly scoped, the two lower lists render side by side in the right order, and the book form is exactly 3 rows grouped as described. No browser harness in this repo — same declared limitation as tests/test_floor_operational_map.py and tests/test_floor_card_redesign.py. Full regression suite re-run clean: test_reservations_map.py, test_floor_operational_map.py, test_floor_card_redesign.py, test_floor_spatial.py, test_floor_admin_ui.py, test_floor_zone_overlap.py, test_floor_map_coordinates.py, test_migrate.py, test_floor_bounds.js. git diff --check: clean.

Explicitly out of scope, not touched: reservation/waitlist business logic and routes, floor/zone/table data model, the picker's select-all/floor-tab JS (already correct from the earlier port), any Floor Plan page behaviour, payment/kitchen/order logic.

This slice was pushed directly on explicit authorization ("comit e push") without an independent review round this time — the user's own call, made after I'd already offered to prepare a CODEX review prompt. origin/main confirmed at e295cd8, /healthz 200. Flagged here for the record, not silently treated as the review having happened.

Post-deploy fix — zone-column gap (real user report, with a detailed spec of its own): the multi-column `.zflow` (CSS `columns: 340px`) used the browser's default height-balancing, which tries to equalize *total* column height rather than simply filling one column then the next — with zones of very different heights (e.g. a 2-table Main next to a 10-table Patio), this left a large empty gap under the shorter zone while it waited to "line up" with its taller row-mate, instead of the next zone starting immediately after it. Root-caused as a genuine limitation of CSS multi-column for this specific case, not a bug in the earlier implementation's execution — confirmed by reading the CSS spec's balancing behaviour, not guessed.

Fixed by replacing the browser-balanced multi-column flow with two literal, independent DOM columns: `floor_entry.zones` split by alternating index (`[0::2]` / `[1::2]`) into two `.zsplit-col` stacks (plain flexbox, `display:flex;flex-direction:column`), rendered side by side via `.zsplit { display:flex }`. Each column's height depends only on its own content — no balancing, no shared rows, no JS, no masonry, no fixed heights, no spacers, matching every constraint the follow-up spec asked to avoid. Collapses to a single stack under 700px width.

Disclosed trade-off (flagged directly in the same turn, not discovered later): the requested narrow-viewport order "Main / Patio / Window / Bar" (the *original*, unsplit order) is not what this produces — collapsing two independent column stacks naturally yields "column 1 fully, then column 2" (here: Main, Window, Patio, Bar), not the interleaved original order. Reproducing the exact original order at narrow width would need either duplicating the zone panels in the DOM (toggled by media query) or JS-driven reordering — both add real complexity/risk (duplicated interactive `.restbl` elements per table is a correctness risk for selection state) for a purely cosmetic narrow-viewport ordering concern, and the follow-up spec's own explicit escape hatch ("unless the existing responsive order is intentionally defined differently") anticipated exactly this kind of case. Left as documented, not silently accepted.

Verified interactively (disposable SQLite reproducing the exact reported scenario — Main 2 tables, Patio 10 tables, Window 2 tables, Bar 4 tables; never production, server torn down after): measured gap between Main and Window is exactly 14px (the plain column gap), not an inflated balancing gap; Patio sits in the independent right column entirely unaffected by Main/Window's height; table selection still works end to end; narrow viewport (420px) collapses to one column, single-column order confirmed as the disclosed Main/Window/Patio/Bar (not the original interleaved order — see above); zero console errors at either width.

tests/test_reservations_layout.py: the old CSS-multi-column-specific test was replaced with one asserting the two-column split explicitly (exactly two `.zsplit-col` elements, alternating-index assignment verified against a controlled `sort_order`, plain flexbox not `columns:`, the old `.zflow` mechanism fully gone). Full regression suite and `git diff --check` re-run clean.

This slice is IMPLEMENTED, self-tested, and committed locally (fix commit pending — see below). It is NOT pushed, NOT deployed — awaiting the same explicit authorization every push on this project requires.
