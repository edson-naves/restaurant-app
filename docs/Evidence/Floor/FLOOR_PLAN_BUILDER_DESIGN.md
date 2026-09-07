# Floor Plan Builder — Technical Design (DESIGN ONLY, NOT AUTHORIZED FOR IMPLEMENTATION)

```text
Worktree: C:\Users\enave\Projetos\Restaurant\restaurant_floor_plan_builder_design
Branch:   design/floor-plan-builder
Base SHA (literal, captured via git fetch origin main + git rev-parse origin/main):
          99e2490dd1c124184ca92e8a88f149fbaec93979
Author:   Claude (design only)
Status:   DESIGN CLOSED / APPROVED — fourth independent review returned
          APPROVED WITH NON-BLOCKING NOTES (both notes incorporated, see
          "Revision note (fourth pass)" below). IMPLEMENTATION REMAINS NOT
          AUTHORIZED — design approval is not implementation authorization.
Scope of this document: DESIGN ONLY. No application code, migration, or test
was written or modified by this slice. See "Confirmation nothing was
implemented" at the end.
```

**Revision note (first pass):** an independent review of the first version of
this document returned `DESIGN REVISION REQUIRED` on three points in §4 — a
false claim that the backfill needs no PostgreSQL-specific call site, a
resilience property misattributed to the wrong existing precedent function,
and an underspecified Y-axis formula. That revision corrected those three
points (plus the NULL-fallback/eligibility items from the same review's
open-question recommendations), as authorial correction, not a second
independent review.

**Revision note (second pass):** a second independent review of the
corrected document returned `DESIGN REVISION REQUIRED` again, on four
points: (1) the backfill's fail-closed guarantee was still a manual/CI step
outside the transaction, not something the code itself enforced, and its
algorithm and its own verification query risked disagreeing on which rows
needed a position; (2) shape's persistence still pointed at the old grid
endpoint's DOM-scraping JavaScript, which has nothing left to read once the
grid leaves this page; (3) a zone move/resize and its tables' repositioning
were two independent, non-atomic `fetch()` calls with no rule tying their
success together; (4) several smaller documentation inconsistencies
(this section's own "Confirmation" text being stale, `docs/03_CURRENT_WORK.md`
not reflecting the second review, an imprecise claim about newly-added
tables). This pass corrects all four, again as authorial correction. §4.2
gains a formal `ELIGIBLE` definition and an in-transaction, code-enforced
check (Correction 1 continued); §6 is restructured into §6.1 (individual
table drag, now carrying shape) / §6.2 (new atomic zone-move-with-tables
endpoint, replacing the standalone `/rect` endpoint) / §6.3 (shared
concurrency/authorization/CSRF policy, unchanged in substance); §5, §8, §9,
§11 are updated to stay consistent. No section outside those changed in
substance.

**Revision note (third pass):** a third independent review, on the
second-pass document, again returned `DESIGN REVISION REQUIRED`, on two
points: (1) `/admin/zones/{zone_id}/move-layout`'s validation checked that
every *listed* table belonged to the zone, but never checked that the
listing was *complete* — a payload that silently omitted one of the zone's
tables passed validation and would have moved the zone while leaving that
table visually behind; (2) `ELIGIBLE` was defined in prose as "active,
non-NULL `zone_id`," which is not formally identical to the `INNER JOIN`
the rest of §4 described using — a table could satisfy the prose without
having an actual matching `Zone` row (an orphaned reference), a case the
previous revision's `ELIGIBLE` didn't name and didn't require checking for.
This pass corrects both: §6.2 now requires an exact-set match
(`received_ids == current_active_table_ids_for_zone`) before any write,
with dedicated tests for every way that can fail; §4.2 restates `ELIGIBLE`
as `ELIGIBLE(table, zone)` over the literal `INNER JOIN` relation, used
verbatim everywhere in §4 rather than re-described per section, and adds a
step-0 integrity check that raises on an orphaned reference rather than
letting the join quietly exclude it with no signal. §4.2's top-level phase
summary and rollout (§8) are also corrected to stop stating generically that
column creation and backfill roll back together on both dialects — they do
on SQLite, not on PostgreSQL, and that distinction is now stated precisely
everywhere it's relevant rather than smoothed over. §9 and §11 are updated
to match. No section outside §4.2, §4.3's cross-reference, §4.4's wording,
§6.2, §8, §9, and §11 changed in substance.

**Revision note (fourth pass) — DESIGN CLOSED / APPROVED:** a fourth
independent review, on the third-pass document, returned `APPROVED WITH
NON-BLOCKING NOTES` — two notes, both incorporated by the author before
closing this design:

1. The disposable-PostgreSQL half of the orphaned-zone-reference integrity
   test (§9) must not depend on disabling or bypassing constraint
   enforcement on that database, since a disposable instance is not
   guaranteed to grant the privileges that requires. Corrected: that half
   of the test builds the orphan using deliberately-legacy, FK-less scratch
   tables of its own, never touching the real schema's constraint at all —
   §9's test description and §11's matching risk-matrix row are both
   updated.
2. §4.5's "a partial run rolls back entirely" was stated generically enough
   to read as true on both dialects the same way; it isn't. Corrected to
   name explicitly which transaction it means (the backfill's own,
   §4.2 steps 0-4) and to restate, rather than silently drop, the
   already-correct per-dialect distinction §4.2 establishes: on SQLite that
   is the same transaction as the column ALTERs; on PostgreSQL the column
   ADDs from the prior phase are already committed and only the backfill
   transaction's own writes are undone.

No other change was made in this pass. **This design is CLOSED / APPROVED.
Implementation remains NOT AUTHORIZED** — approval of this document
authorizes nothing beyond itself; a separate, explicitly authorized
migration/backfill implementation slice is the proposed next step (see
`docs/03_CURRENT_WORK.md`), not implied by this closure.

## 1. Purpose and historical reference

Port the free-placement administrative Floor/Zone builder from the abandoned
`feat/floor-map` branch (`restaurant_app` worktree, commit `5d8eee4`) onto the
current `main` line, selectively — not the branch tip, not the operational
Map/List + Arrange mode, not the staff-color picker (both out of scope, see
§10). This follows the audit already recorded in `docs/03_CURRENT_WORK.md`
("`feat/floor-map` branch inventory — HISTORICAL, READ-ONLY AUDIT").

Historical reference inspected (read-only, not used as a base):

```text
web/templates/admin_tables.html   (free-placement version, ~435 lines over the
                                    shared ancestor)
app/routers/admin.py              save_layout() — combined table+zone AJAX save
app/models/oltp.py                RestaurantTable/Zone — same column names,
                                    different Table semantics (see §2)
```

## 2. The coordinate collision — confirmed, not assumed

Verified directly in both lines' current code:

```text
Zone.pos_x/pos_y/width/height   — SAME semantics on both lines: per-mille
                                   (0-1000) of the plan. main's edit_zone()
                                   already clamps 0-1000 exactly like the
                                   historical branch's save_layout(). No
                                   collision. No migration needed for Zone.

RestaurantTable.pos_x/pos_y     — DIFFERENT semantics, same column names:
  main (99e2490):    grid cell index, 0 <= x < GRID_COLS (10), y unbounded row
                      index. Enforced in save_layout(): "if not (0 <= x <
                      GRID_COLS and 0 <= y): raise 400". Model docstring:
                      "Floor-plan grid coordinates for the visual layout."
  feat/floor-map:     per-mille (0-1000) of the plan, table's centre point.
                      Enforced by `_clamp(v) = max(0, min(1000, v))`. Comment:
                      "Overlap is allowed ... values are just clamped."
```

Reusing the historical free-placement code verbatim against `main`'s existing
`pos_x/pos_y` values (0-9) would place every existing table in the top-left
~1% of the map. This must never happen silently. §3 is the mitigation.

Also confirmed, narrowing the actual gap versus the original audit:

```text
RestaurantTable.shape          already exists on main (VARCHAR(10) DEFAULT
                                'round', added by the Floor-spatial slice) AND
                                is already wired end-to-end in the current
                                grid-based admin_tables.html: shape-cycle
                                button, SHAPES array, saved via save_layout's
                                4th ':shape' field. Shape cycling is NOT new
                                work — it already works and must simply keep
                                working once positioning goes free-form.

set_zone_tables()              already implements the exact declarative
(admin.py:630, AJAX/204)       "N tables x M seats" grow/shrink behavior the
                                historical branch has, including: shrink only
                                ever retires FREE tables with no live order,
                                all-or-nothing (never a partial retire that
                                could pick the wrong table), capacity only
                                changes on free tables. This is a direct
                                backend reuse, not a port.

_assign_zone() helper           shared code, already used by both lines'
                                drag-to-a-zone re-link behavior. Direct reuse.
```

## 3. Coordinate strategy — decision

**Adopt new, explicitly-named columns. Do not reinterpret `pos_x`/`pos_y`.**

```text
RestaurantTable.map_x_per_mille  INTEGER, nullable, no DB default
RestaurantTable.map_y_per_mille  INTEGER, nullable, no DB default
```

Rationale for new columns over reinterpreting `pos_x`/`pos_y`:

- `pos_x`/`pos_y` remain load-bearing for the grid view that
  `admin_tables.html` (current) and `floor.html` (operational Map/List, out of
  scope this slice) both still read. Silently changing their meaning breaks
  both without a code change there.
- A `NULL` per-mille pair is an honest, queryable "never placed on the free
  map yet" state — a `0` or `60` default would be indistinguishable from a
  deliberate placement and would need a sentinel anyway.
- This mirrors the `Zone` precedent exactly: Zone's per-mille columns exist
  alongside no other conflicting representation, because zones never had a
  grid model. Tables did, so tables need the same separation Zone never
  needed.

Rejected alternative — reinterpret `pos_x`/`pos_y` in place: requires a
flag-day cutover of every reader (`admin_tables.html`, `floor.html`,
`save_layout`, `_free_cells`, `_add_tables`) in one atomic release with no
partial-rollout path, and permanently loses the grid position if the free map
is ever rolled back. Rejected.

**Backfill (see §4) is deterministic and one-directional: grid → per-mille.**
There is no reverse sync. Once a table is dragged on the free map, its
`map_x_per_mille`/`map_y_per_mille` become that table's source of truth for
free-map rendering; its `pos_x`/`pos_y` are frozen at whatever the grid last
had them at and continue to serve the grid view and `floor.html` unchanged.

**Removal criteria for `pos_x`/`pos_y` (future, NOT this slice):** only after
(a) the operational Map/List + Arrange slice (§10, out of scope) ports the free
map to `floor.html` too, so nothing reads grid coordinates anymore, and (b) a
separate, explicitly authorized migration slice removes the columns with its
own review. This design does not schedule that removal.

## 4. Migration and data

Three pipeline phases, not one — plus a fail-closed guarantee that lives
*inside* phase 2, not bolted on afterward as a hopeful manual step. The first
version of this document got two things wrong here: it conflated "the
backfill ran" with "the backfill provably succeeded," and it put the only
check for the second of those outside the transaction, as a manual/CI step —
which is evidence, not a guarantee. Corrected below.

```text
1. COLUMN CREATION   ADDED_COLUMNS entries land on both dialects (existing
                      generic loop). Purely structural — every value is NULL
                      immediately after this step, on SQLite AND on Postgres.
2. BACKFILL          _backfill_table_map_positions(conn) — computes AND
   (self-verifying,   writes real values for every eligible existing table,
    fail-closed)      THEN, inside its own transaction, verifies that every
                      eligible table now has an in-range, non-NULL position —
                      and raises if not, aborting that transaction. This is
                      the correction: the guarantee is code, enforced every
                      time this function runs, on both dialects — not a
                      separate manual step someone might skip. What "that
                      transaction" rolls back is NOT the same on both
                      dialects — see §4.2's transactional behavior, corrected
                      (third review) to state this precisely instead of
                      generically: on SQLite it is the same transaction as
                      phase 1's column ALTERs; on PostgreSQL, phase 1's
                      columns are already committed by the time phase 2
                      runs, so a phase-2 failure there rolls back only
                      phase 2's own writes, and the columns remain.
3. UI ACTIVATION      the free-map route/template ships as its own later,
                      separately authorized slice (§8), strictly after phase
                      2 has completed without raising, on both dialects —
                      this gate does not depend on which dialect's failure
                      mode applies, only on whether phase 2 raised.
```

A manual/CI check against the actual target environment (§4.2's closing
note, §8) still has a place — as *additional* evidence for a human doing a
deploy runbook, not as the mechanism that makes the guarantee real. The
mechanism is the in-transaction check in step 2, which runs unconditionally,
every time, with no separate step for anyone to forget.

### 4.1 Column creation

```text
ADDED_COLUMNS gets two new entries:
    ("restaurant_table", "map_x_per_mille", "INTEGER")
    ("restaurant_table", "map_y_per_mille", "INTEGER")
```

Applied through the existing generic loop — quoted table/column names, SQLite
via PRAGMA introspection + guarded ALTER, Postgres via the existing
`_run_postgres` / `ADD COLUMN IF NOT EXISTS` branch (verified at
`app/migrate.py:276`: `f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS
{column} {_pg_boolean_ddl(ddl)}'` — `_pg_boolean_ddl` passes a plain
`INTEGER` DDL through unchanged, only boolean defaults are translated). A
**fresh** database gets both columns from `create_all()` (model gets the two
new `Mapped[int | None]` columns) — the loop is then a no-op, exactly as
documented for every other entry in `ADDED_COLUMNS`.

**This step alone leaves every row NULL, on both dialects.** Column creation
running does not mean the backfill ran. They are separate calls (§4.2) that
must both be wired into both `run()` and `_run_postgres()` independently —
this is the correction this revision makes; see §4.2.

**No DB-level CHECK constraint for the 0-1000 bound.** Precedent: `Zone`'s
existing per-mille columns carry no CHECK constraint either — bounds are
enforced only in the write path (`edit_zone`'s `max(0, min(1000, ...))`). The
migration framework here has no precedent for adding a CHECK constraint to an
existing table (only `UNIQUE_CONSTRAINTS` has that machinery), and SQLite
cannot ALTER a CHECK onto an existing table without a full rebuild this
framework doesn't do. New table columns follow the same enforcement boundary
Zone already established: clamp in Python at every write, not in the schema.

**Indexes:** none proposed. These columns are read per-page-load (all tables
on one floor, already an existing full scan bounded by `< 50` tables per
restaurant) and written one row at a time on drag-end; no query filters or
sorts by per-mille position. Matches the "do not claim index use without
query-plan evidence" rule in `01_ARCHITECTURE.md`.

### 4.2 Backfill — corrected: dialect wiring and failure policy

**Correction 1 — PostgreSQL call site (the first version of this document was
wrong here).** The first version claimed "no dialect-specific branch needed
beyond the existing split." That is false for the backfill. Verified
directly in `app/migrate.py`:

```text
app/migrate.py:187-188   if engine.dialect.name != "sqlite": return _run_postgres(...)
app/migrate.py:202       applied.extend(_backfill_locations(conn))   — only inside
                          the SQLite branch of run()
app/migrate.py:240+      def _run_postgres(...): — never calls _backfill_locations
                          anywhere; its only backfill call is
                          _backfill_prep_tasks at line 320
```

`_backfill_locations`, the function this document originally said the new
backfill would be "modeled directly on," has **no PostgreSQL call site at
all** in the current codebase. Modeling the new backfill on it without an
independent, explicit Postgres call site would silently reproduce that same
gap: columns would exist in production (phase 1/§4.1 runs fine on both dialects)
but every value would stay NULL forever in Postgres, because nothing would
ever call the function that fills them in.

**Binding requirement for implementation:** `_backfill_table_map_positions(conn)`
must have two call sites, both added explicitly, neither inherited implicitly
from the ADDED_COLUMNS loop:

```text
- inside run()'s SQLite branch, alongside (not replacing) the existing
  applied.extend(_backfill_locations(conn)) call
- inside _run_postgres(), as a new call — there is no existing Postgres
  backfill call site to piggyback on; this one has to be added from scratch,
  the same way _backfill_prep_tasks's Postgres call (line 320) was added
  alongside its SQLite one (line 212) rather than assumed
```

Column creation succeeding on Postgres is not evidence the backfill ran there
— they are two different calls now required at two different, explicitly
authored sites.

**Correction 2 — failure/resilience policy (the first version misattributed
this too).** The first version said the new backfill would be "best-effort
... never blocks startup," describing that as `_backfill_locations`'s
existing behavior. Verified directly — it is not:

```text
_backfill_locations(conn)    called plainly at app/migrate.py:202, INSIDE the
                              same `with engine.begin() as conn:` block as the
                              column ALTERs (lines 189-202), with NO try/except
                              of its own. A raised exception there rolls back
                              that whole transaction (including the column
                              ADDs just made) and propagates out of run().
                              This function has NO best-effort protection.

_backfill_prep_tasks(conn)   called at app/migrate.py:206-212 (SQLite) and
                              :320 (Postgres), each wrapped in its own
                              try/except that turns a failure into an
                              "SKIPPED ..." log entry instead of raising.
                              THIS is where "best-effort, never blocks
                              startup" actually comes from.
```

**Explicit decision for the new backfill (not left to whichever precedent it
happens to resemble): fail-closed, not best-effort.** Rationale: the free-map
builder is useless with NULL positions for every existing table — silently
"succeeding" with an empty or partial backfill is worse than failing loudly,
because phase 3 (UI activation) would then ship a broken-looking screen
instead of a blocked deploy. `_backfill_prep_tasks`'s best-effort posture
makes sense *for its own domain* (a missing prep-task backfill degrades one
feature, KDS task reads, without preventing the app from booting for
orders/payments/every other domain); this backfill's failure mode is
different — nothing downstream depends on it *except* the one screen this
whole slice exists to build, so there is no broader system to protect by
swallowing the error.

**Correction 3 (second review) — the guarantee must be code, inside the
transaction, not a manual step someone can skip.** The previous revision put
"verify every eligible table got a position" outside the transaction, as a
manual/CI count query run against a deployed environment — that is evidence
after the fact, not a guarantee. `_backfill_table_map_positions(conn)` is
corrected to enforce this itself, every time it runs, on both dialects:

```text
def _backfill_table_map_positions(conn) -> list[str]:
    0. Integrity check (third review) — run BEFORE anything else, see below.
    1. SELECT the eligible set (ELIGIBLE, defined below) — every row this
       function is responsible for.
    2. For each eligible row: clamp pos_x/pos_y defensively (§4.3), compute
       map_x_per_mille/map_y_per_mille via the §4.3 formula, UPDATE that row.
       Every eligible row is processed — none are silently skipped.
    3. In the SAME transaction, immediately after step 2, re-SELECT the
       eligible set (same ELIGIBLE definition) and check: does every row now
       have non-NULL map_x_per_mille AND map_y_per_mille, both within
       [0, 1000]?
    4. If step 3 finds even one violation, raise. Do not return a "SKIPPED"
       string, do not log and continue — raise, so the surrounding
       transaction (§4.1's column ALTERs plus this function) rolls back
       entirely and migrate.run() propagates the exception. This is what
       "fail-closed" means concretely: a coding error in step 2 cannot ship
       silently, because step 3/4 catches it in the same breath, before the
       transaction ever commits.
```

**`ELIGIBLE`, one formal definition over a table AND a zone, used
identically everywhere in this section — corrected (third review) to name
the join explicitly rather than describe it only in prose:**

```text
ELIGIBLE(table, zone) :=  table.is_active = TRUE
                       AND table.zone_id = zone.id

Conceptually, always via:
    RestaurantTable INNER JOIN Zone ON RestaurantTable.zone_id = Zone.id
    WHERE RestaurantTable.is_active = TRUE
```

This single relation is used, unchanged, in the initial selection (step 1),
the `GRID_ROWS_SEEN` computation (§4.3 — that per-floor query is this same
join with one more predicate, `Zone.floor_id = <this floor>`, added on top),
the backfill loop (step 2), the in-transaction post-condition (step 3), the
manual/CI query (below), and every test that needs to state "the rows this
backfill is responsible for" (§9). There is exactly one place this relation
is defined and every other mention names it, rather than re-describing it —
that is what actually prevents the class of drift the second review found
(an algorithm and its own verification quietly disagreeing on the set).

`table.zone_id IS NOT NULL` is not stated as a separate condition because an
`INNER JOIN ON table.zone_id = zone.id` already can't match a NULL
`zone_id` — there is nothing for it to equal. Writing `zone_id IS NOT NULL`
as an extra, separately-worded clause (as the previous revision did) invites
exactly the kind of "two definitions that happen to agree today" risk this
correction closes; the join makes it structural instead of coincidental.

**Integrity check, step 0 — new in this revision, runs before step 1 and
raises rather than proceeding:** an *orphaned* reference — an active table
whose `zone_id` is non-NULL but does not match any existing `Zone` row — is
neither ignored nor treated as a quiet success. Concretely:

```text
orphans = SELECT restaurant_table.id FROM restaurant_table
          LEFT JOIN zone ON restaurant_table.zone_id = zone.id
          WHERE restaurant_table.is_active = TRUE
            AND restaurant_table.zone_id IS NOT NULL
            AND zone.id IS NULL
IF orphans is non-empty: raise (naming the orphaned table ids), before
   step 1 ever runs. Do not skip these rows and continue with the rest of
   the backfill, and do not treat their absence from ELIGIBLE (the INNER
   JOIN correctly excludes them, since they have no matching Zone row to
   join to) as "handled" — being excluded from ELIGIBLE and being a
   detected data-integrity fault are two different things, and this design
   requires both: excluded from processing, AND loud about why.
```

**Why this can be checked but is expected to find nothing, on this
codebase's actual write paths:** `app/database.py` enables
`PRAGMA foreign_keys=ON` for SQLite, and `RestaurantTable.zone_id` is a real
foreign key to `zone.id` on both dialects — so a normal write through this
app cannot create an orphan on either engine. Zone rows are also never hard
-deleted anywhere in this codebase (soft-retire via `is_active=False` only,
verified — no `db.delete(zone)` / `DELETE FROM zone` call exists). This
check therefore should never find anything through this app's own code
paths. It exists anyway because a migration cannot assume the database in
front of it was only ever touched through this app's own code paths — a
restored backup, a manual SQL edit, or an external bulk-load with
constraints temporarily disabled could all produce exactly this state, and
the backfill is exactly the wrong place to discover that silently.

No other condition on `ELIGIBLE` beyond the join and `is_active`. In
particular, **not** conditioned on `pos_x`/`pos_y` being present or in
range — because that condition can never actually distinguish anything:
`RestaurantTable.pos_x` and `.pos_y` are `NOT NULL` columns with
model-level defaults (`oltp.py:378-379`), enforced by the schema itself, for
*every* row, eligible or not. There is no reachable state where an eligible
row has a NULL `pos_x`/`pos_y` — the schema makes that impossible, not this
function's judgment. A row's `pos_x`/`pos_y` being merely *out of the
expected grid range* (negative, or `>= GRID_COLS`) is not a NULL and is
handled by the existing defensive clamp (§4.3) before the formula runs — it
always yields an in-range, non-NULL result, so it can never be why step 3/4
raises. If step 3/4 ever does raise, it is because of an actual bug in step
2's arithmetic or wiring (exactly the class of thing Correction 1 found) —
that is the point: the check exists to catch an implementation defect, not
a data shape, because the data shape is already constrained to never
produce one on its own.

**Behavior on partial failure, both dialects, now explicit — the check lives
inside the transaction on both:**

```text
SQLite      Column ALTERs, backfill (steps 2-4 above), all run in one
            `with engine.begin() as conn:` transaction. A raised exception —
            from the ALTERs, from step 2's writes, or from step 4's
            fail-closed check — rolls the whole block back: no orphaned
            state where columns exist with a half-finished or silently
            -incomplete backfill. run() then propagates the exception (there
            is no strict=False swallow path for this block specifically —
            the surrounding try/except in run() only wraps
            _backfill_prep_tasks, not this one).

PostgreSQL  Each column ADD in _run_postgres already runs in its own
            transaction, independently guarded (existing code, line
            274-282) — a column-add failure there is caught, and either
            raises MigrationError (strict=True) or is logged as SKIPPED
            (strict=False), per column, same as today. The new backfill call
            added to _run_postgres runs in its own transaction, AFTER all
            column ADDs, running steps 1-4 above exactly as on SQLite; it is
            NOT wrapped in the per-column try/except — a failure at step 2
            or a raise at step 4 aborts that transaction and propagates,
            regardless of `strict`. Unlike the column loop, this is a single
            all-or-nothing step with no "per-column" granularity to log
            partial progress against — there is nothing meaningful to SKIP
            and continue with when the fail-closed check has just found some
            tables positioned and others not.
```

**Required test — this is what makes Correction 1 durable, not just
documented:** a test that creates the two columns (phase 1, either by
running the real migration against a schema that predates them, or by
starting from a schema that already has them empty) against a **disposable
PostgreSQL** database seeded with at least one active, zoned table with grid
`pos_x`/`pos_y` set, runs `migrate.run()` (the real Postgres path, not the
SQLite path), and asserts `map_x_per_mille IS NOT NULL AND map_y_per_mille IS
NOT NULL` for that table afterward. This test must fail against the
implementation this document originally described (columns created, backfill
never invoked on Postgres) and pass once the Postgres call site required
above exists.

**Required test, second review's addition — the fail-closed guarantee itself
must have a test, not just a design description of it:** inject a failure
into step 2 or step 4 (e.g. a test-only monkeypatch that leaves one eligible
row's `map_y_per_mille` NULL after step 2, or corrupts it out of `[0,1000]`)
and assert (a) `migrate.run()` raises, and (b) re-reading the schema/table
afterward shows **no** partial state — on SQLite, the columns added by step
1 in that same run are gone too (full transaction rollback, re-verified by
introspecting the schema post-failure); on the disposable PostgreSQL, the
column-ADDs from step 1 (already committed in their own per-column
transactions, per the existing `_run_postgres` design) remain, but zero rows
show a written value from the failed backfill attempt — consistent with
step 1 and step 2 being genuinely separate transactions on that dialect,
already documented above.

A manual/CI count check against the actual deployment target —
`SELECT COUNT(*) FROM restaurant_table JOIN zone ON restaurant_table.zone_id
= zone.id WHERE restaurant_table.is_active = 1 AND (map_x_per_mille IS NULL
OR map_y_per_mille IS NULL OR map_x_per_mille NOT BETWEEN 0 AND 1000 OR
map_y_per_mille NOT BETWEEN 0 AND 1000)` (the same `ELIGIBLE` set, spelled
out as SQL, with the range check added) — still has a place as additional,
human-facing evidence in the deploy runbook (§8), precisely because the
in-transaction guarantee above is what makes that query *expected* to always
return zero, not because the query is itself the guarantee. Both tests
above are listed again in §9. This query's `JOIN` is the same `ELIGIBLE`
relation defined above (a plain `JOIN` is an inner join), not a
separately-worded approximation of it.

**UI activation gate, both dialects, explicit (third review):** phase 3
(§4, §8) does not ship on *either* dialect until phase 2 has completed on
*that* dialect without raising — a SQLite success does not license skipping
Postgres, and vice versa; each dialect's own phase 2 outcome gates phase 3
for that environment. This was already true structurally (phase 3 is a
separate, later deploy per §8) but is stated here directly rather than only
implied by the ordering.

**Retry after fixing the root cause, both dialects, explicit (third
review):** once whatever caused phase 2 to raise is fixed (a code bug, a
malformed row from outside this app's write paths, etc.), re-running
`migrate.run()` is safe and idempotent on both dialects — §4.5 already
establishes this for the ordinary case (nothing left to redo once complete);
the same holds after a fixed failure, because a failed run — by the
fail-closed guarantee above — never left any eligible row's position
half-written on either dialect (SQLite: the whole attempt rolled back with
the columns; PostgreSQL: the columns persisted but zero rows show any
backfill-attempt value), so the next run starts from exactly "columns exist,
ELIGIBLE rows all still NULL" on both, not from some rows done and others
not.

### 4.3 Y-axis definition — corrected, formal

**`GRID_ROWS_SEEN` is now fully specified, per floor, computed once before
the per-table loop:**

```text
GRID_ROWS_SEEN(floor) = max(1, MAX(pos_y) + 1)
    over ELIGIBLE(table, zone) (§4.2's INNER JOIN relation, unchanged),
    with one additional predicate on top: Zone.floor_id = <this floor>
```

This is deliberately not restated as its own independent join — it is
`ELIGIBLE`, filtered to one floor. Restating the join separately here (as
the previous revision did, spelling out `RestaurantTable JOIN Zone ON ...`
a second time with its own slightly different predicate list) is exactly
the kind of duplication that let the join and the eligibility rule drift
apart; naming `ELIGIBLE` and adding only the one floor-scoping predicate
keeps there being a single relation, everywhere in §4.

**Why the join, not a direct `GROUP BY floor_id`:** verified in
`app/models/oltp.py:397-399` — `RestaurantTable.floor_id` is a **Python
property** (`self.zone_ref.floor_id if self.zone_ref else None`), not a real
column. There is no `restaurant_table.floor_id` for SQL to group by directly;
grouping "per floor" requires joining through `zone.floor_id`, exactly as
written above. This is a fact about the current schema this document's first
version did not check and would have produced a non-working query against.

**Per-table formula, the per-row `max()` removed as instructed:**

```text
map_x_per_mille = clamp(round((pos_x + 0.5) / GRID_COLS * 1000), 0, 1000)
map_y_per_mille = clamp(round((pos_y + 0.5) / GRID_ROWS_SEEN * 1000), 0, 1000)
```

`GRID_COLS` is the existing module constant in `admin.py:69` (10). Once
`GRID_ROWS_SEEN` is computed correctly per floor as above, `pos_y + 1 <=
GRID_ROWS_SEEN` holds for every table on that floor by construction (it's
literally `MAX(pos_y)+1` over that same set) — the old `max(pos_y + 1,
GRID_ROWS_SEEN)` per table was always equal to `GRID_ROWS_SEEN` and is
removed as redundant and misleading (it read as if some table could ever
exceed the floor's own maximum, which cannot happen).

**Handling for null/negative/invalid inputs, explicit:**

```text
floor_id resolves to None
(zone_id IS NULL)             This table has no floor to backfill against —
                               it is not "eligible" (§4.4). Left untouched:
                               map_x_per_mille/map_y_per_mille stay NULL,
                               rendered via the NULL fallback (§4.4) if/when
                               it is ever placed in a zone and rendered.

pos_y IS NULL                 Not handled as a special case, deliberately —
                               per §4.2's Correction 3, `ELIGIBLE` is
                               unconditional on `pos_x`/`pos_y` because the
                               schema (`NOT NULL`, oltp.py:379) makes a NULL
                               `pos_y` impossible for *any* row, eligible or
                               not. There is no branch here that "excludes"
                               such a row, because there is no such row to
                               exclude — adding one would silently reintroduce
                               the eligible-but-unprocessable gap §4.2 closes.
                               If this were ever violated (a schema/DB bug
                               outside this design's control), the write in
                               step 2 would itself raise (a `NOT NULL`
                               constraint or a Python `TypeError` on the
                               arithmetic), which is correctly fail-closed —
                               not a silent skip.

pos_y < 0 or pos_x < 0         Cannot occur today: save_layout's own
                               validation (`0 <= x < GRID_COLS and 0 <= y`,
                               admin.py:724) rejects negative input before
                               persisting, and every other writer (seed.py,
                               _free_cells) computes non-negative values
                               programmatically. Defensive handling anyway:
                               clamp to 0 before use in both the per-table
                               formula and the GRID_ROWS_SEEN MAX for that
                               floor, rather than letting a negative value
                               produce an out-of-formula-range result or
                               skew GRID_ROWS_SEEN downward.

pos_x >= GRID_COLS             Cannot occur today (save_layout enforces
                               `x < GRID_COLS`). Defensive handling: clamp to
                               GRID_COLS - 1 before use, so the formula still
                               produces a value inside (0, 1000) rather than
                               overflowing past 950 before the final clamp
                               absorbs it anyway (the final clamp(...,0,1000)
                               is a second, redundant safety net either way —
                               belt and suspenders, not a single point of
                               failure).
```

**Reproducible rounding rule, explicit — this was unspecified before:** the
per-mille value for each table is computed **once, in Python**, using
`math.floor(x + 0.5)` for `x >= 0` (deterministic round-half-up on
non-negative input; deliberately not Python's built-in `round()`, which uses
round-half-to-even and would differ from a naive `ROUND()` call in SQL) — and
written as a literal integer parameter in the `UPDATE ... SET
map_x_per_mille = :x, map_y_per_mille = :y WHERE id = :id` for that one row.
**No SQL-side `ROUND()` function is used anywhere in this backfill, on either
dialect** — SQLite and PostgreSQL round differently in edge cases (and
neither is guaranteed to match Python), so the arithmetic must happen exactly
once, in Python, and the already-rounded integer is what both dialects ever
see. This makes "reproducible between Python, SQLite and PostgreSQL" trivially
true by construction: there is only one implementation of the rounding rule,
not three that need to agree.

### 4.4 Eligibility, NULL coordinates, and inactive tables — corrected

**Backfill scope, explicitly declared (the first version left this
implicit):** the backfill targets exactly `ELIGIBLE(table, zone)` (§4.2) —
this paragraph is prose for that same relation, not a second,
separately-worded criterion: active (`is_active = TRUE`) tables that join
to an existing zone (`table.zone_id = zone.id`), and therefore have a
resolvable `floor_id` (§4.3). A table with no zone (`zone_id IS NULL`) has
no row to join to and is excluded by the join itself, structurally, the
same way an orphaned `zone_id` (§4.2's integrity check) is. Soft-retired
tables (`is_active = false`) and zone-less tables are **not** backfilled
and keep `map_x_per_mille`/`map_y_per_mille` at NULL indefinitely — this is
a deliberate choice, not an oversight: retired tables never render on any
floor plan (every existing rendering path already filters
`is_active`-false tables out), and a zone-less table cannot be placed on a
per-floor map it doesn't belong to. Leaving both NULL is correct, not a gap.

**Reactivating a retired table (`toggle_table`):** does not retroactively
backfill it immediately — this design does not add backfill-on-reactivate
logic to `toggle_table` (that would be new application-code behavior, out of
this migration-focused design's boundary). It stays NULL and renders via the
same fallback (below) as any never-placed table, until either the manager
drags it (gets a real position immediately, backfill becomes moot for that
row) or a later app restart re-runs `migrate.run()`, which will now see it as
eligible-and-NULL and backfill it like any other row that newly became
eligible. No data is lost or corrupted either way; the only effect is how
long a reactivated table shows the fallback position instead of its old grid
-derived one.

**Fallback is render-time only — never written to the database.** A `NULL`
`map_x_per_mille`/`map_y_per_mille` stays `NULL` in the row. No sentinel
value (`0`, `60`, or otherwise) is ever written to mean "not placed yet" —
`NULL` already means exactly that, unambiguously, and writing a sentinel
would make a genuinely-placed-at-that-exact-spot table indistinguishable from
an unplaced one. The template computes a display position for a NULL row
every time it renders; the row itself is untouched until a real drag writes
real coordinates through §6.1's endpoint.

**Fallback placement, corrected — the original "table id parity" version
only produced 2 distinct offsets (odd/even), which stacks 3+ unplaced tables
in the same zone on top of each other.** Corrected, and tightened once more
on re-reading this same revision: an id-modulo scheme (`table.id % N`) was
the first fix attempted here, but it does not actually guarantee N distinct
values either — ids 3, 6, 9 all reduce to 0 mod 3, colliding exactly like the
parity version did. The correct construction needs an actual **rank**, not a
modulus of an arbitrary id:

```text
For a zone rectangle rendered with its NULL-position (unplaced) tables:
  siblings = that zone's NULL-position tables, sorted by id ascending
             (deterministic, computed from the already-fetched list — no
             extra query)
  rank     = siblings.index(this_table)          # 0, 1, 2, ... N-1 — a true
                                                    bijection onto {0..N-1},
                                                    unlike id % N
  offset   = point `rank` on a ring (or compact sub-grid for larger N)
             centred on the zone's rectangle, sized to the zone's own
             width/height
```

Sorting by id and taking the list index (not the id itself, and not any
modulus of it) is what actually guarantees N unplaced tables get N distinct
positions, for any N. `N` (and every table's rank) is recomputed at render
time from whatever is NULL *then* — no stored state — so the spread
naturally tightens back toward a single point as tables leave the NULL set
one drag at a time.

### 4.5 Idempotency and rollback

**Idempotency:** re-running `migrate.run()` after the backfill has already
completed is a no-op — the eligible-and-NULL set (§4.4) is empty, so
`_backfill_table_map_positions` has nothing to update on either dialect, on
any subsequent run. A partial run (interrupted mid-transaction) rolls back
entirely per §4.2's fail-closed decision — clarified (fourth review, note
2), since "entirely" means different things per dialect and this section
had stated it generically: "rolls back entirely" refers to the **backfill's
own transaction** (§4.2 step 0-4), not the column-creation phase that
preceded it. On SQLite, that backfill transaction is the same one as the
column ALTERs (§4.1), so an interruption there does roll back both
together — see §4.2's transactional behavior for that dialect specifically.
On PostgreSQL, the column ADDs from the prior phase are already committed,
each in its own transaction, by the time the backfill's transaction even
starts (§4.2) — a partial/interrupted backfill there rolls back only the
backfill transaction's own writes; the already-committed columns remain,
exactly as §4.2's per-dialect transactional behavior already describes.
Either way, "partial" and "complete" are the only two reachable states for
the backfill step itself on a single run — there is no partially-applied
backfill state to resume from, and re-running from "complete" only ever
adds newly-eligible rows (a table that changed from ineligible to eligible
since the last run — e.g. a zone-less table that got assigned a zone),
never redoes already-backfilled ones.

**Rollback:** dropping the two new columns is out of scope for a rollback of
this slice's *code* — the standing convention in `app/migrate.py` is
additive-only; nothing in this codebase drops columns as a rollback path.
Reverting the route/template commit (phase 3, the UI-activation step above) leaves the columns and
any manager-entered or backfilled positions intact and simply unused until a
forward-fix re-ships — no data loss on a code rollback, by construction
(§3-4).

## 5. Functional scope of the builder

```text
IN SCOPE:
  - free (per-mille) positioning of tables on the existing Tables/map page
    (admin_tables.html), additive to the grid view, not replacing it (see
    below)
  - zone rectangles rendered and drag-resizable on that same page (zones
    already have the data; today nothing renders/edits them as rectangles
    outside the separate admin_floors.html form); moving/resizing a zone
    atomically repositions its tables with it (§6.2) — never two independent
    saves for one gesture
  - drag-to-move a single table on the free map; drop-into-a-zone-rectangle
    re-assigns zone_id (reuses _assign_zone, already shared code, via §6.1)
  - table shape cycling: the interaction itself carries over unchanged
    (already works, see §2), but its persistence moves from the current
    grid endpoint's 4th field to the new free-map endpoint's own payload
    (§6.1) — the old grid endpoint has no DOM left to read shape from once
    this page stops rendering the grid
  - zone add/rename/color/retire: UNCHANGED, still admin_floors.html AND the
    existing zone card on admin_tables.html — this slice does not touch that
  - "N tables x M seats" declarative add/remove per zone: UNCHANGED, already
    exists (set_zone_tables), the free map just needs to render tables that
    route already creates
  - autosave on drag-end (see §6), matching the historical branch's UX and
    admin_tables.html's own existing autosave precedent for
    set_zone_tables/set_staff_color (both already 204-AJAX)
  - error handling: a failed autosave call must not silently revert the
    manager's drag or hide the failure (see §6)

OUT OF SCOPE for the builder itself (decided against, not deferred):
  - removing or restructuring admin_floors.html. It stays as the current
    floor/zone creation entry point; unifying it into one page (as
    feat/floor-map did) is a separate, larger navigational change this slice
    does not make. Keeping it also means create_zone_tables' separate
    add-only path keeps working unmodified.
  - a toggle between "grid view" and "free view" as two different rendering
    modes of the SAME table set is NOT how this design frames the map — see
    below.
```

**Grid view vs. free view — resolved as one page with a graceful default, not
two competing UIs.** The Tables/map page renders the free-placement plan
using `map_x_per_mille`/`map_y_per_mille` when present, falling back per-table
to the stable NULL-position placement (§4.4) for tables never dragged yet. The
existing grid-drag interaction (dragging a `.chip` between fixed `GRID_COLS`
cells) is retired from this page and replaced by free-drag — it cannot
sensibly coexist with free positioning in the same viewport without asking
the manager to pick a mode, which the historical branch never did either (it
replaced the grid outright). The grid semantics on `pos_x`/`pos_y` are
**not removed** (§3) — only this page's *interaction* changes; `floor.html`'s
own grid rendering (out of scope, §10) is untouched and keeps reading
`pos_x`/`pos_y` exactly as it does today.

## 6. Write contracts

Two write paths, one per **gesture**, not one per resource — this distinction
is why there are still two endpoints, corrected from the first version's
now-wrong reason for having two (see below).

### 6.1 Individual table drag — corrected payload (Correction 2)

**The first version of this document said shape would keep being saved
through the CURRENT `/admin/tables/layout` route's existing `:shape` field,
"reused as-is." That is not implementable once the grid leaves this page —
corrected here.** Verified directly: the current shape-cycle button's
payload is built by reading `.chip` elements positioned inside `.cell` grid
cells (`admin_tables.html`'s existing serialization walks the grid DOM).
Once free positioning replaces that DOM structure on this page (§5), there
is no grid `.cell`/`.chip` structure left for that old JavaScript to read —
nothing on the new page would ever call the old endpoint. Saying "shape
keeps saving through the old route" described a code path that could not
run.

**Corrected contract — shape moves onto the new endpoint's own payload:**

```text
POST /admin/tables/map-layout
  tables: str = Form("")   "id:x:y:shape[:zone_id]" per table,
                            comma-separated. shape and zone_id are optional
                            per-entry ("id:x:y" alone is valid — a plain
                            move with no shape/zone change).
  -> 204 on success
```

Backend behavior, all inside one `db.commit()` per request:

```text
- table id must exist (400 "Unknown table id", matching current
  save_layout's convention) — checked for every entry before any write
- x/y clamped server-side to [0, 1000] regardless of client input (never
  trust client-side clamping alone)
- shape, if present, validated against {"round", "square", "rect"} — an
  unrecognized value is a 400 ("Invalid shape '{value}'"), not a silent
  ignore (silently ignoring a bad shape would leave the client believing
  it saved when it didn't)
- zone_id, if present, reassigns via the existing shared _assign_zone(table,
  zone) helper (same call the drag-into-a-zone-rectangle gesture already
  uses) — this is how dragging one table into a different zone's rectangle
  is persisted; it does NOT touch that zone's rectangle or any other table
  in it (that is the separate, atomic zone-operation endpoint, §6.2 —
  moving one table into a zone and moving/resizing the zone itself are
  different gestures and stay on different endpoints)
- malformed payload -> 400 ("Malformed layout entry '{chunk}'"), same
  message style already used
- pos_x/pos_y (the grid columns) are never read or written by this
  endpoint — untouched, per §3
```

The old `POST /admin/tables/layout` route is not removed by this design —
nothing forces that, and other integrations could still target it — but it
is explicitly **not** the write path the new free-map page's JavaScript
calls for shape (or anything else). The claim that it would remain "the
functional path for shape" on the new page is withdrawn; §9's test coverage
is updated to match (new endpoint gets its own shape-validation tests; the
old endpoint's existing test is unaffected because nothing about the old
route changed).

### 6.2 Zone move/resize — atomic with its tables (Correction 3)

**The first version saved a zone's rectangle (`/admin/zones/{zone_id}/rect`)
and its tables' positions (§6.1's endpoint) as two independent `fetch()`
calls, with no rule tying them together — even though moving or resizing a
zone visually repositions its tables at the same time. That let half a drag
gesture persist while the other half failed. Corrected: one atomic endpoint,
replacing the standalone `/rect` endpoint entirely.**

```text
POST /admin/zones/{zone_id}/move-layout
  pos_x, pos_y, width, height: int = Form(...)   the zone's final rectangle
  tables: str = Form("")                          "id:x:y" per table this
                                                    zone operation moved —
                                                    every table proportionally
                                                    repositioned by the drag
                                                    (move) or resize on the
                                                    client; empty string if
                                                    the zone currently holds
                                                    no tables
  -> 204 on success
```

Division of responsibility, explicit: the client computes the proportional
new position of each affected table as the manager drags/resizes (client
-side math, same as the historical branch's own "resizing a zone repositions
its tables proportionally" behavior) — the backend does not recompute or
second-guess that math; it only validates and persists the *already-computed
final* rectangle and table positions, atomically, in the same transaction.
The backend is a dumb, atomic write, not a geometry engine.

**Validation, all performed BEFORE any write — matches `set_zone_tables`'s
existing "reject before mutating" and `save_layout`'s "Unknown table
id"/"Malformed" conventions. Corrected (third review): "every listed table
belongs to this zone" is necessary but not sufficient — it never checked
that the payload named *every* one of the zone's tables. A payload that
silently dropped one table used to pass validation and would have moved the
zone and the tables it did mention while leaving the omitted one behind,
visually detached from the zone it's still actually in. Corrected to an
exact-set requirement:**

```text
received_ids  = the ids parsed out of `tables`, as sent
current_ids   = SELECT id FROM restaurant_table
                WHERE zone_id = :zone_id AND is_active = TRUE
                — queried fresh, immediately before validation, never
                  reused from an earlier request or a client-supplied count

REQUIRE: received_ids has no duplicate (400 "Duplicate table id {id} in
          payload" otherwise — checked before the set comparison, since a
          duplicate would otherwise silently collapse into the set and
          could mask a genuinely missing id)
REQUIRE: set(received_ids) == set(current_ids), exactly — not a subset,
          not a superset, not "every received id happens to be valid"
    - any id in current_ids but not received  -> 400 naming the missing
      table id(s) — a table this zone actually has, silently left out
    - any id in received but not current_ids  -> 400 naming the
      unexpected table id(s) — covers a genuinely unknown id, an id
      belonging to a DIFFERENT zone, AND an id that belongs to this zone
      but is not currently active (see below) — all three are just "not a
      member of current_ids", one rule, one error path
```

**`tables=""` is valid in exactly one case, and it falls out of the same
rule rather than needing a special-cased branch:** empty payload parses to
an empty `received_ids`, which only equals `current_ids` when the zone
genuinely has zero *active* tables right now. A zone with any active table
sent an empty payload fails the same missing-id check as any other
incomplete payload — "the zone has no tables" is not an assumption this
endpoint takes on faith from an empty string, it's a fact re-verified
against the database every time.

**Inactive tables, explicit:** `current_ids` is built with `is_active =
TRUE` — a soft-retired table in this zone is never part of the required
set, is never expected in the payload, and if a client's payload names one
anyway that id fails the "received but not in current_ids" check above (an
inactive table's id is, by definition, not in `current_ids`) and the whole
request is rejected — it is not silently accepted and it is not silently
ignored while the rest of the payload proceeds. An inactive table's
`map_x_per_mille`/`map_y_per_mille` are therefore never touched by this
endpoint, under any payload: either the request that would have touched
it is rejected outright (present in the payload), or it was correctly never
expected to be there (absent, as it should be).

- rect clamps: pos_x/pos_y in [0, 1000], width/height >= 60 — the exact
  clamp values edit_zone already uses
- each listed table's x/y clamped server-side to [0, 1000]
- malformed payload (unparseable entry, non-integer id/coordinate) -> 400,
  same message style already used

**If any of the above fails, nothing is written — the zone rectangle and
every listed table's position are one atomic unit, one `db.commit()`, not
two independent calls that could each partially succeed.** This is the
correction: replacing two `fetch()` calls with one endpoint, and requiring
the exact-set check above to pass before touching the session, is what
actually makes "if any input is invalid, nothing is written" true, rather
than a requirement the design stated without a mechanism to enforce it.

`_assign_zone` is **not** called by this endpoint — a zone move/resize never
changes which zone a table belongs to (only its position within the same
zone), so there is nothing to reassign here. `_assign_zone` stays where §6.1
already put it: the individual-table-drag-into-a-different-zone gesture.

**Concurrency note, explicit:** this endpoint's atomicity is scoped to *one
zone operation* (its own rect plus its own tables) — it does not serialize
against an unrelated concurrent request, e.g. someone individually dragging
one of this zone's tables via §6.1 at the same moment. That remains a
last-write-wins race, same category and same acceptance already given for
two managers touching the same row (§6.3, §12) — not solved here, and not
newly introduced by this endpoint; only the atomicity *within* a single
zone-move gesture is what this correction guarantees.

**Visual failure:** a non-2xx response marks the zone rectangle AND every
table that was part of that same move/resize gesture as unsaved together —
never just the zone, never just a subset of its tables — because the server
-side unit of success or failure is now the same unit the UI must show as
saved or not. Same non-snap-back, retry-friendly behavior as §6.3.

### 6.3 Shared validation, transactions, and concurrency policy

**Transactions:** one `db.commit()` per request — for §6.1 that unit is
however many tables were in that drag-end payload; for §6.2 that unit is the
zone's rect plus every table it carried with it. No cross-request
transaction spans more than one autosave gesture either way; no request's
transaction spans two separate `fetch()` calls.

**Concurrent autosave / lost updates:** each write is a full overwrite of
exactly the rows named in that request — not a read-modify-write on a stale
client copy of some larger layout. Two managers touching *different* rows
concurrently never conflict. Two managers touching the *same* row (a table
via §6.1, or a zone-and-its-tables via §6.2) concurrently is a genuine
last-write-wins race; this is accepted, not solved, in this slice: it
mirrors the existing `save_layout`'s behavior today (also last-write-wins,
no optimistic locking on `RestaurantTable`), and a visual arrangement tool
for a single-owner-role screen (`require("settings")` = Owner-only, never
two concurrent owners in practice per the current permission model) has low
real-world collision risk. If this needs to change, it is a decision for the
review, not something this design silently resolves either way — flagged in
§12 as an open question with a recommended default (accept last-write-wins).

**Authorization:** identical to every other write route on this page —
`Depends(require("settings"))`, i.e. Owner only, unchanged. No new permission
is introduced.

**CSRF/nonce:** **the current base has no working CSRF/nonce framework to
integrate with.** Verified directly: the only `nonce` reference in the entire
codebase is `app/routers/sales.py:346` (`create_nonce`), which is generated
into the template context but never rendered into a form field or verified
by any route — it is dead code today. Every existing POST route (including
every write route this design reuses or extends) relies solely on the signed
session cookie for authentication, with no CSRF token of any kind. This
design **does not introduce CSRF protection** for the new endpoints — adding
real CSRF defense app-wide is a cross-cutting security change outside a
Floor Plan slice's boundary (`AGENTS.md` "Production / Security Safety") and
would need its own explicit authorization and design, not a one-off token on
two new routes while every other POST route in the app remains unprotected.
This gap is pre-existing, not introduced by this design; noted for the
record and left for a separate security-scoped slice.

**Error responses and visual recovery:** an autosave failure (a non-2xx
response to the new `fetch()` calls) must (a) visibly flag the affected
table/zone as unsaved — reusing the label pattern the historical branch's
`label(r.ok ? 'Arrange · saved' : 'Save failed')` already demonstrates is
workable — and (b) leave the element exactly where the manager dropped it in
the DOM rather than snapping it back, so a retry (re-drag or explicit retry
control) doesn't require redoing the whole gesture. Silent failure (network
error swallowed, no visible state) is explicitly rejected. §6.2 restates this
per-gesture for the atomic zone-operation endpoint specifically.

## 7. Compatibility

```text
Reservations / "Seat here"   Verified: app/services/reservations.py never
                              reads pos_x, pos_y, shape, or zone_id — only
                              capacity and is_active. The new columns and the
                              free-map UI have zero interaction surface with
                              availability/overlap logic. Seat-here's table
                              selection is unaffected.

Floor spatial (current)       zone_groups / due-soon / reservation-picker
                              rendering on floor.html is untouched — this
                              slice does not modify floor.html. That page
                              keeps reading pos_x/pos_y exactly as today.

Kitchen Stations               No interaction. Stations route via
                              menu_item/order_item.station_id, never via
                              table position.

Permissions                   require("settings") unchanged, Owner-only,
                              same as every other write route on this page
                              today.

Touch/mouse                   The historical branch's drag implementation
                              uses generic pointer events (mode = {type,
                              el, dx, dy} with dragstart/move/end handling),
                              not the HTML5 native drag-and-drop API the
                              CURRENT grid page uses (draggable="true" +
                              dragstart/dragover/drop) — native HTML5 DnD is
                              poorly supported on touch. Porting the
                              historical pointer-event approach is a
                              deliberate improvement, not an incidental
                              change, and should be called out as such in
                              the eventual implementation handoff.

No-JavaScript behavior         Explicitly degrades: without JS, no drag is
                              possible, and per-mille positions can only ever
                              be their backfilled/NULL-fallback value. This
                              is accepted, not solved — the grid page has the
                              identical limitation today (drag also requires
                              JS there), so this is not a regression.

Basic accessibility            Not solved by this design. A drag-only
                              interaction with no keyboard-operable
                              equivalent is a known, carried-forward gap —
                              identical to the current grid page's drag
                              interaction, which also has no keyboard
                              alternative today. Flagged as pre-existing, not
                              newly introduced, and out of this slice's
                              authorized scope to fix (fixing it is an
                              app-wide interaction-pattern change).

Payment / Security             Zero files under app/services/payment_*.py,
                              app/services/square.py, app/security.py, or
                              app/config.py are touched by this design. No
                              incidental change proposed or implied.
```

## 8. Rollout

```text
Feature flag: NOT proposed. Precedent exists in this codebase
  (show_table_admin / show_zone_waiter, app/services/settings.py) but that
  precedent is now dead config with no UI consumer (per the historical audit
  already on record). A flag here would gate replacing the *only* Tables/map
  page's interaction model — there is no "old" page to fall back to once the
  route ships, unlike show_table_admin's optional secondary panel. If a
  staged rollout is wanted, the natural seam is the three phases §4 already
  defines (creation → backfill[self-verifying, fail-closed] → UI activation),
  not a runtime flag — each is its own deploy/checkpoint, in order:

    1. column creation ships (§4.1) — no visible behavior change, grid page
       keeps working exactly as today, columns exist but are NULL.
    2. backfill ships and runs on BOTH dialects (§4.2) — this does not
       happen for free as a side effect of step 1, on either dialect,
       requires its own explicit call site on each (Correction 1), opens
       with the orphaned-zone-reference integrity check (third review,
       §4.2 step 0), and is fail-closed with the pass/fail check *inside*
       its own transaction (Correction 3, second review) — a startup abort
       here, on either dialect, means step 3 never ships, rather than
       shipping silently incomplete. What exactly rolls back on an abort
       differs by dialect (§4.2's transactional behavior) — SQLite undoes
       step 1's columns along with step 2; Postgres keeps step 1's
       already-committed columns and only step 2's writes are undone —
       but on both, re-running after fixing the cause is safe and
       idempotent (§4.2, §4.5).
    3. UI/route change (new endpoints, free-placement template) ships as its
       own reviewed slice, strictly after step 2 has completed without
       raising, on both dialects.

  A manual/CI count query against the actual deployment target (§4.2's
  closing paragraph) is still worth running before step 3, as independent,
  human-facing confirmation — but step 2 having completed without raising is
  what the deploy actually depends on, not that query.

Observability: no new logging/metrics infrastructure proposed (none exists
  for admin_tables.html today either — this is consistent with, not a
  regression from, current practice). The existing `applied` list returned
  by migrate.run() already surfaces "backfilled N tables" style strings to
  whatever currently consumes that return value (startup log) — the new
  backfill function follows the exact same convention on both dialects now
  (§4.2), so its result is observable the same way every other migration
  step's is today, on both.

Rollback of code without losing new data: §4.5.

Objective activation criteria: 
  - column creation (§4.1) and backfill (§4.2) both applied on the target
    environment, i.e. `migrate.run()` completed there without raising —
    which by §4.2's Correction 3 already means the in-transaction check
    found zero violations, on that dialect;
  - the same confirmed independently on SQLite AND on the actual Postgres
    environment being deployed to — not inferred from one dialect to the
    other;
  - the Correction-1 Postgres-parity test, the Correction-3 fail-closed
    test, and the third review's orphaned-reference integrity test (§4.2,
    §9) all passing — "ran on both dialects," "fails loudly if it didn't
    work," and "fails loudly on a corrupt reference rather than silently
    excluding it" all have test evidence, not just a manual check;
  - the third review's `/admin/zones/{zone_id}/move-layout` exact-set tests
    (§6.2, §9 — omitted/extra/duplicate/wrong-zone/empty-on-empty/
    empty-on-occupied/inactive-excluded/no-partial-write) passing, so the
    zone-operation endpoint's atomicity claim also has test evidence;
  - test suite in §9 (new + reused) green on both SQLite and a disposable
    PostgreSQL;
  - independent design review of this document returns APPROVED or APPROVED
    WITH NON-BLOCKING NOTES before an implementation slice is authorized.
```

## 9. Tests and evidence

```text
REUSABLE AS-IS (no change needed, must stay green):
  tests/test_floor_spatial.py::test_set_zone_tables_grows_and_shrinks_softly
  tests/test_floor_spatial.py::test_set_zone_tables_never_retires_an_occupied_table
  tests/test_floor_spatial.py::test_set_staff_color
  tests/test_floor_spatial.py::test_edit_zone_saves_rect   (zone per-mille —
                                                             untouched by this slice)
  tests/test_reservations_map.py  (16 tests — availability/seat-here have zero
                                   dependency on table position, per §7)
  tests/pg_reservation_overlap_race_proof.py
  tests/pg_seat_here_race_proof.py

MUST BE UPDATED, NOT JUST REUSED:
  tests/test_floor_spatial.py::test_save_layout_sets_shape_and_position
      — currently asserts grid (x,y) semantics against the CURRENT
        /admin/tables/layout route. That route is UNCHANGED by this design
        (shape-cycling still goes through it) so this test stays green
        as-is; a NEW sibling test is needed for the new
        /admin/tables/map-layout route, not a modification of this one.

NEW TESTS REQUIRED (none of these exist today):
  - MANDATORY, Correction 1 — Postgres backfill parity: against a disposable
    PostgreSQL (tests/_pay_fixture.py's pg_dsn, skip if unavailable), seed at
    least one active, zoned table with grid pos_x/pos_y set, run the real
    _run_postgres() path (not the SQLite path), then assert
    map_x_per_mille/map_y_per_mille are NOT NULL for that table. This test
    must FAIL against an implementation that creates the columns on Postgres
    but never calls the backfill there (exactly the gap the first review
    found) and PASS once §4.2's required Postgres call site exists.
  - MANDATORY, Correction 3 (second review) — backfill fail-closed guarantee:
    on SQLite, inject a failure so that after step 2 (§4.2) one ELIGIBLE
    row's `map_y_per_mille` is left NULL (or pushed out of `[0,1000]`), run
    `migrate.run()`, and assert it raises AND that re-introspecting the
    schema afterward shows the two new columns absent again (full
    transaction rollback — column ADDs and backfill are one unit on this
    dialect). On a disposable PostgreSQL, same injected failure inside the
    backfill's own call site, assert `migrate.run()` raises AND that zero
    rows show any written `map_x_per_mille`/`map_y_per_mille` value from the
    failed attempt (column ADDs, already committed in their own per-column
    transactions on this dialect per existing `_run_postgres` behavior,
    remain — only the backfill's effect is what must show nothing partial).
    This test must FAIL against an implementation that merely logs a
    "SKIPPED" string on a violation instead of raising, and PASS once §4.2's
    step 3/4 (the in-transaction ELIGIBLE re-check that raises) exists.
  - MANDATORY, third review — orphaned zone reference integrity check
    (§4.2, step 0): on SQLite, force an active table's `zone_id` to point at
    a zone id that does not exist (bypassing the app's own FK-enforced
    write paths via a raw `PRAGMA foreign_keys=OFF` connection, or inserting
    the row before the FK is established — since going through the app's
    normal paths cannot produce this state), run `migrate.run()`, and
    assert it raises, naming that table's id, BEFORE any column ALTER or
    backfill write happens.
    On the disposable PostgreSQL (fourth review's non-blocking note 1):
    build the orphan without touching that database's own constraint
    enforcement — a disposable instance is not guaranteed to grant the
    privileges needed to disable or defer a constraint, so the test must
    not depend on that. Instead, construct deliberately-legacy, throwaway
    tables in their own scratch schema (e.g. `zone_id INTEGER` with no `
    REFERENCES` clause at all, populated to already contain the orphaned
    row) and point the test at those instead of the real, FK-backed
    `restaurant_table`/`zone` tables — the orphan exists by construction,
    with the real schema's FK never in the picture, so nothing needs
    disabling. Both variants must FAIL against an implementation that
    silently omits the orphan from `ELIGIBLE` (via the `INNER JOIN`) and
    calls that "handled," and PASS once step 0's explicit raise exists.
  - migration: fresh SQLite gets both columns via create_all (no-op loop) —
    modeled on tests/test_migrate.py's existing style
  - migration: pre-existing SQLite DB (columns absent) gets them added by
    the guarded ALTER loop (SQLite direct path)
  - migration: pre-existing Postgres DB (columns absent) gets them added by
    the guarded `ADD COLUMN IF NOT EXISTS` path — modeled on
    tests/test_pg_migration.py's engine fixture. Distinct from the mandatory
    parity test above: this one only checks column creation, the parity test
    checks that the backfill also ran.
  - backfill: deterministic grid->per-mille conversion produces distinct,
    in-bounds (0-1000) positions for a multi-table, multi-row floor,
    verified against the exact formula and the per-floor GRID_ROWS_SEEN
    definition in §4.3 (including a multi-floor fixture, to prove the join
    through Zone.floor_id actually scopes per floor and doesn't mix rows
    from different floors into one GRID_ROWS_SEEN)
  - backfill: a zone-less table (zone_id IS NULL) is excluded from both the
    GRID_ROWS_SEEN computation and from receiving a computed position,
    per §4.4's eligibility rule
  - backfill: idempotent — running migrate.run() twice produces identical
    per-mille values the second time (no drift, no re-randomization),
    verified on both dialects, not just SQLite
  - backfill: a table added AFTER the migration has run (map columns already
    exist) is not touched by that run's backfill, and stays NULL until
    EITHER the manager drags it (real position written immediately) OR a
    later invocation of `migrate.run()` re-evaluates `ELIGIBLE` and finds it
    newly eligible (e.g. it was zone-less and got assigned a zone since) and
    backfills it then — not "until dragged" alone; both paths are valid and
    must be covered, matching §4.4's reactivation behavior exactly
  - POST /admin/tables/map-layout (§6.1): valid `id:x:y` payload updates
    exactly the named tables, clamps out-of-range x/y, 400s on
    malformed/unknown-id input, requires require("settings") (403 for a
    non-Owner role — reuse the existing permission-test pattern already used
    elsewhere for other require("settings") routes)
  - POST /admin/tables/map-layout, shape (Correction 2): `id:x:y:shape`
    persists a valid shape (round/square/rect); an unrecognized shape value
    is a 400, not a silent ignore; `id:x:y` alone (no shape segment) leaves
    the table's existing shape untouched
  - POST /admin/tables/map-layout, zone_id (Correction 2): `id:x:y::zone_id`
    (or `id:x:y:shape:zone_id`) calls _assign_zone and persists the new
    zone_id + denormalized zone name — the NEW route's call site needs its
    own test; the OLD route's equivalent test does not cover it
  - POST /admin/zones/{zone_id}/move-layout (Correction 3), complete payload:
    `received_ids` exactly equals the zone's current active table ids — the
    zone's rect moves, every listed table's position moves with it, all
    persisted in one request
  - POST /admin/zones/{zone_id}/move-layout, proportional resize: a zone's
    width/height changes, its listed tables land at the client-supplied
    (already-proportionally-computed) positions — the backend does not
    recompute the proportions, only persists what was sent, per §6.2's
    division of responsibility
  - POST /admin/zones/{zone_id}/move-layout, omitted table (third review):
    the zone has N active tables, the payload lists only N-1 — 400, the
    missing table's id named in the error, and neither the rect nor any of
    the N-1 listed tables' positions are written
  - POST /admin/zones/{zone_id}/move-layout, extra/unknown id (third
    review): the payload lists every current table plus one id that does
    not exist at all — 400, nothing written
  - POST /admin/zones/{zone_id}/move-layout, duplicate id (third review):
    the payload lists one of the zone's table ids twice (and, to still be
    "complete" by naive count, omits a different one, or simply has one
    duplicate flagged before the set comparison ever runs) — 400 for the
    duplicate specifically, not silently collapsed into the set check,
    nothing written
  - POST /admin/zones/{zone_id}/move-layout, table belonging to another
    zone (third review, sharpened from the previous "table that changed
    zones" case): the payload includes an id that is currently active and
    assigned to a DIFFERENT zone — rejected the same way as any other id
    not in `current_ids` for THIS zone (one rule, §6.2), nothing written;
    also covers a table reassigned by a concurrent §6.1 call between the
    client building its payload and this request validating
  - POST /admin/zones/{zone_id}/move-layout, empty payload on an empty zone
    (third review): the zone currently has zero active tables, `tables=""`
    — valid, rect-only update persists, no table rows touched
  - POST /admin/zones/{zone_id}/move-layout, empty payload on an occupied
    zone (third review): the zone currently has one or more active tables,
    `tables=""` — 400 (every current table is "missing" from an empty
    payload), nothing written, exercising the same rule as the omitted
    -table case at N=0-received/N>0-expected
  - POST /admin/zones/{zone_id}/move-layout, inactive table correctly
    excluded (third review): the zone has one active and one soft-retired
    table; a valid payload names only the active one (matching
    `current_ids`, which excludes the retired one by construction) — 204,
    and the retired table's map_x_per_mille/map_y_per_mille are unchanged
    before and after; separately, a payload that also names the retired
    table's id is rejected (that id is not in `current_ids` either) with
    nothing written
  - POST /admin/zones/{zone_id}/move-layout, rejection without partial write
    (third review, general case beyond the specific scenarios above):
    parametrize the above failure cases (missing/extra/duplicate/wrong-zone/
    malformed-rect) and assert, for each, that BOTH the zone's rect columns
    AND every table's map_x_per_mille/map_y_per_mille are byte-identical to
    their pre-request values afterward — not just "the response was 400"
  - POST /admin/zones/{zone_id}/move-layout, error and retry: a rejected or
    failed request leaves both the zone and its tables marked unsaved
    together (§6.2), never a subset; retrying re-sends the same atomic
    payload and succeeds once the payload is valid
  - POST /admin/zones/{zone_id}/move-layout, authorization: require("settings")
    unchanged, 403 for a non-Owner role, same pattern as the other new route
  - regression: existing test_admin.py suite re-run (AGENTS.md already
    records this suite failing at documentation-consolidation time while
    backend authorization assertions still passed — the implementer must
    re-verify current status before relying on it as a green baseline, not
    assume it is fixed)
  - integration smoke: full floor with mixed NULL and backfilled tables
    renders without error (covers the NULL-fallback placement path, §4)

NOT IN SCOPE for this slice's test evidence (belongs to the out-of-scope
items in §10):
  - browser/device/touch validation — no real-device testing exists anywhere
    in this codebase today (01_ARCHITECTURE.md's own evidence boundary
    already states this); this slice does not change that boundary.
  - a PostgreSQL concurrency proof for the last-write-wins autosave race —
    decided as accepted, not mitigated (§6.3), so no proof is needed unless
    review changes that decision.
```

## 10. Out of scope for this slice (confirmed against the request)

```text
- any application code, migration, or template implementation — this
  document is the only artifact this slice produces
- operational Floor page Map/List + Arrange mode (floor.html) — separate
  slice, per the governance note already on record
- staff visual color picker (admin_staff.html) — separate slice, per the
  same governance note
- any change to Payment/Security files
- deploy or production mutation of any kind
- integrating any part of the feat/floor-map branch tip wholesale
- removing admin_floors.html or its wizard flow
- removing pos_x/pos_y or the grid view from floor.html
- adding CSRF/nonce protection app-wide (pre-existing gap, not this slice's to fix)
- keyboard-accessible drag alternative (pre-existing gap, not newly introduced)
```

## 11. Risk matrix

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Backfill runs on SQLite but never on PostgreSQL, so every table stays NULL in production even though the columns exist (Correction 1) | Would be certain if the original "model it on `_backfill_locations`" instruction were followed without an independent Postgres call site — that function has none today | High (the builder ships unusable in production while looking fine in dev/test) | Explicit, separate, required call site inside `_run_postgres()` (§4.2); mandatory Postgres-parity test (§4.2, §9); in-transaction fail-closed check on both dialects (§4.2, Correction 3) means this can no longer ship silently even if a future edit reintroduces it |
| Backfill algorithm and its own correctness check disagree on which rows must have a position, so a row could pass verification-as-eligible-but-skipped-by-algorithm (Correction 3, second review) | Would be certain with two independently-worded definitions of "needs a position," which the first revision had | High (exactly the silent-partial-success class of bug this whole section exists to prevent) | One formal `ELIGIBLE` definition (§4.2), used verbatim by both the selection query and the in-transaction re-check; unconditional on `pos_x`/`pos_y` because those are schema-`NOT NULL` for every row already, so no row can be eligible-yet-unprocessable |
| Silent grid/per-mille reinterpretation scatters existing tables | Would be certain if naively ported | High (visual corruption, manager confusion, possible mis-clicks on wrong table) | New, separately-named columns (§3); backfill is additive-only, never touches `pos_x/pos_y` |
| Backfill produces overlapping/degenerate positions on an unusual floor (e.g. 1 table, or 50 tables in one row) | Medium | Low (visual only — free map explicitly allows overlap, manager re-drags) | Deterministic, fully-specified per-floor formula (§4.3); fail-closed on error rather than silently producing a wrong-but-plausible-looking result (§4.2) |
| Two managers drag the same table concurrently, last write wins silently | Low (single Owner role in practice) | Low (one dropped edit, no data corruption, no financial/data-integrity effect) | Accepted explicitly, matches existing `save_layout`'s behavior; flagged as an open question in case review disagrees (§12) |
| New AJAX endpoints inherit the app-wide CSRF gap | Certain (gap is app-wide already) | Same as every other existing POST route — not a new exposure | Documented explicitly (§6.3) rather than silently accepted; not fixed here because fixing it app-wide is out of this slice's boundary |
| Postgres `ALTER TABLE` for the two new columns fails in production (locking, permissions, etc.) | Low (plain nullable INTEGER add, no data conversion in the DDL itself) | High if it happened (blocks deploy) | Existing `strict=True` `MigrationError` contract already fails closed rather than starting against a mismatched schema — no new failure mode, reuses existing safety net |
| Free-map UI accidentally becomes reachable without `require("settings")` | Low (copy-paste from existing routes that already have the dependency) | High (unauthorized table/zone rearrangement) | Explicit test requirement in §9 for 403 on a non-Owner role, for both new routes |
| Regression in the existing grid-based `save_layout`/shape-cycling while adding the free-map path | Medium (touches admin_tables.html, shared template) | Medium (shape cycling and grid view both currently work and must keep working per §5) | Existing `test_save_layout_sets_shape_and_position` must stay green unmodified (§9); free map is additive UI, not a replacement of that route |
| `test_admin.py`'s already-known failure is mistaken for a regression this slice caused | Medium (easy to conflate) | Low (wastes review time, not a real defect) | Explicitly flagged in §9 as a pre-existing condition to re-verify, not assume fixed |
| Shape stops saving entirely once the grid leaves the page, because the design pointed at a write path (the old grid endpoint's DOM-scraping JS) that no longer has anything to read (Correction 2) | Would be certain — verified the old JS reads `.chip` inside `.cell`, which won't exist on the new page | Medium (a real, visible feature quietly breaks, not just a design-doc inconsistency) | Shape moves onto the new endpoint's own payload (§6.1); explicit tests for valid/invalid shape values (§9) |
| Zone move/resize and its tables' repositioning persist as two independent `fetch()` calls, so a mid-gesture failure leaves the zone rectangle and its tables disagreeing with each other (Correction 3) | Would be certain with two separate endpoints and no rule tying their success together, which the first revision had | Medium (visual desync between a zone's box and its own tables; manager has to notice and manually reconcile) | One atomic endpoint, one transaction, for a zone's rect and every table it moved (§6.2); explicit atomicity/incomplete-payload/table-changed-zone tests (§9) |
| `/admin/zones/{zone_id}/move-layout` accepts a payload that silently omits one of the zone's tables (or includes an extra/duplicate/wrong-zone id), moving the zone and the tables it did mention while a real table of that zone is left visually detached (third review) | Would be certain — the second revision's validation only checked "every listed id belongs to this zone," never "every one of this zone's ids is listed" | Medium (a table quietly stops matching its own zone's rectangle; not caught until a manager notices) | Exact-set requirement, `received_ids == current_active_table_ids_for_zone`, checked before any write (§6.2); dedicated tests for omitted/extra/duplicate/wrong-zone/empty-on-empty/empty-on-occupied/inactive-excluded payloads, plus a general no-partial-write assertion (§9) |
| An active table's `zone_id` points at a `Zone` row that no longer exists (orphaned reference), and the backfill either crashes on an unexpected null from a bad join or silently treats the row's absence from `ELIGIBLE` as if the situation were fine (third review) | Low — `PRAGMA foreign_keys=ON` (SQLite) and a real FK constraint (both dialects) make this unreachable through the app's own write paths; possible only via external DB manipulation (restored backup, manual SQL, bulk load with constraints off) | High if it happened silently (a data-integrity fault discovered, if ever, only much later, by a manager wondering why a table never appears) | Explicit step-0 integrity check before the backfill runs, raising and naming the orphaned table id(s) rather than proceeding (§4.2); dedicated tests on SQLite (bypassed FK) and, where available, PostgreSQL (via deliberately-legacy scratch tables built without an FK at all, not by disabling constraints on the real schema — fourth review's note 1) that construct the orphan and assert the raise happens before any write (§9) |

## 12. Open, genuinely decisional questions for review

Two of the original four questions are resolved by this revision, not
because the review made the call, but because they turned out to have a
concretely correct answer once checked against real code/math rather than
being genuinely open — noted below for the record, not re-opened.

```text
1. Concurrent-autosave policy (§6.3): accept last-write-wins (recommended,
   matches existing behavior) or add optimistic locking
   (e.g. an updated_at/version check) on RestaurantTable/Zone writes? This
   would be new machinery not present anywhere else in the codebase today.
   STILL OPEN — genuine tradeoff, no code fact resolves it either way.

2. Whether `admin_floors.html` staying separate (§5) is acceptable long-term,
   or whether unifying it into one page (as feat/floor-map did) should be
   scheduled as a distinct, later, explicitly authorized slice. This design
   takes no position beyond "not in this slice." STILL OPEN — a product/
   navigation decision, not a technical one this document can settle.

RESOLVED by this revision (were open questions 2 and 4 in the first version):

  - NULL-position fallback: no longer "table id parity" (only 2 buckets,
    stacks from the 3rd unplaced table onward). §4.4 now specifies each
    table's zero-based rank (list index after sorting a zone's current
    NULL-position tables by id, not `id % N` — that also collides for some
    id sets, caught and fixed during this same revision) at render time,
    spread on a ring/sub-grid sized to the zone's rectangle. Auto-fallback
    (not a forced placement gate) is kept, for the UX reason already given:
    gating would block every other floor-plan action until every table is
    manually placed.

  - Y-axis backfill precision: §4.3 now gives `GRID_ROWS_SEEN` a precise,
    per-floor, pre-loop definition (`max(1, MAX(pos_y)+1)` joined through
    `Zone.floor_id`, since `RestaurantTable.floor_id` is a Python property,
    not a column — verified at `oltp.py:397-399`). The per-table `max()`
    that made the original formula read as if a per-row exception existed
    is removed as algebraically redundant once `GRID_ROWS_SEEN` is computed
    this way. Kept dynamic per floor rather than switched to a fixed
    constant — a constant would reintroduce the uneven-bunching problem the
    per-floor approach exists to avoid.
```

## Confirmation nothing was implemented

```text
git status --short (in this worktree, at time of writing):
   M docs/03_CURRENT_WORK.md
  ?? docs/Evidence/Floor/            (contains this file — new directory)

Both are documentation-only, both are the two files this design slice is
authorized to touch. No file under app/, web/, tests/, or migrate.py was
created or modified. No migration was run. No route, model, or template was
changed. No commit was made in this worktree.
```
