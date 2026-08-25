# Kitchen Stations — v5 (Stage B1 concurrency/idempotency hardening)

**Version:** v5 · **Scope:** the targeted B1 safety fixes only (durable DB-level
idempotency for PreparationTask creation + backfill). **No redesign, no B2.**
KDS / Expo / floor / payment / serving / coursing / snapshot / Unassigned / the
Stage A legacy `OrderItem.station_id` are all unchanged. User-visible behavior is
unchanged. **Pair:** this handoff + `KITCHEN_STATIONS_v5.diff` (full cumulative).

## Files changed vs v4

- `app/models/oltp.py` — durable unique index on the fire batch.
- `app/migrate.py` — create that index on existing DBs; conflict-safe backfill.
- `app/routers/sales.py` — meaningful `fire_seq`; catch `IntegrityError` on the
  fire commit; `fired_items_without_tasks` invariant.
- `tests/test_prep_tasks.py` — 4 new tests (DB rejection, concurrent fire,
  concurrent backfill, B2 invariant).

## 1. Task identity / idempotency rule (the final defense)

**Durable DB-level rule:** a **UNIQUE index on
`(order_item_id, fire_seq, COALESCE(station_id, -1))`** (`uq_prep_task_batch`).

- It is the **final** protection — not the app checks. Two independent
  transactions that both build the same fire batch cannot both commit: the
  second violates the index and is rejected.
- It is **not** `UNIQUE(order_item_id, station_id)`, so a **future re-fire/remake**
  (a new `fire_seq`) may legitimately add another task at the same station.
- `COALESCE(station_id, -1)` extends the rule to the **NULL / Unassigned** base
  task (both SQLite and Postgres treat bare NULLs as distinct — the COALESCE
  fixes that). Same DDL string on both engines; built by `create_all` on fresh
  DBs and `CREATE UNIQUE INDEX IF NOT EXISTS` on existing ones.
- The existing layers remain as fast-paths, not the guarantee: the `Order`
  row-lock (`SELECT … FOR UPDATE`) serializes fires, the "already has tasks"
  guard skips re-work, and the fire routes now **catch `IntegrityError`** and
  treat a losing concurrent fire as a no-op (rollback + normal redirect).

## 2. `fire_seq` semantics

`fire_seq` = the fire-batch number for a line: `1 + max(existing task fire_seq)`.
B1 fires a line exactly once, so it is **1**. A future re-fire computes the next
batch; the DB unique rule allows a later batch to reuse a station while rejecting
a duplicate **within** a batch. Assignment is concurrency-safe because the DB
unique index — not the Python `max(...)+1` — is the arbiter (a racing duplicate
of the same batch is rejected at commit).

## 3. Concurrency-safe backfill

`_backfill_prep_tasks` keeps its `NOT EXISTS` sequential guard **and** now issues
a **conflict-safe insert against `uq_prep_task_batch`**: `INSERT OR IGNORE` on
SQLite, `INSERT … ON CONFLICT DO NOTHING` on Postgres. So two startup processes
racing the backfill can never insert a duplicate base task. It still uses the
historical `OrderItem.station_id`, never re-reads current MenuItem routing,
preserves NULL as Unassigned, never modifies `order_item`, and is additive.

## 4. Concurrency regression tests (new)

- `test_db_rejects_duplicate_within_batch` — the DB rejects a second
  `(line, fire_seq, station)`; a different `fire_seq` is allowed; a duplicate
  NULL/Unassigned in the same batch is rejected (COALESCE).
- `test_concurrent_fire_creates_one_batch` — **two overlapping sessions** both
  build the same batch before either commits; the first commits, the second is
  rejected by the unique index → **exactly one batch** (2 tasks, not 4), line
  status still correct.
- `test_concurrent_backfill_no_duplicate` — two sessions run the backfill; exactly
  one task; legacy Unassigned stays NULL.
- (existing) sequential duplicate fire, backfill idempotency, rollup, coursing,
  payment gate, etc.

## 5. B2 readiness invariant

`fired_items_without_tasks(db)` returns the count of fired OrderItems
(preparing/ready) with **no** PreparationTask. **B2 must not switch the KDS to
task-based reads unless this is 0** for the population expected to have tasks —
otherwise task-based reads would silently hide kitchen work. It's informational
in B1; `test_readiness_invariant_for_b2` proves it is 0 after a fire, 1 for a
legacy task-less fired item, and back to 0 after backfill.

## SQLite vs Postgres note

The **guarantee** (the unique index + conflict-safe insert) is identical on both.
The *contention mechanics* differ: SQLite serializes writers at the DB level (one
writer at a time), so a true simultaneous fire is naturally sequenced and the
loser then either finds nothing pending or is rejected by the index; Postgres
uses the `Order` row-lock to serialize and the index as the final guard. The
tests exercise the strongest guarantee available on SQLite (overlapping sessions
→ the index rejects the duplicate at commit).

## Test results

`tests/test_prep_tasks.py` — **all pass** (15 cases). Regression:
`test_stations.py`, `test_floormap.py`, `test_reservations_map.py` all green;
`app.main` imports; dev DB migrated (index created; 7225 backfilled tasks intact,
no duplication). e2e is a live-server harness (not run here).

## Confirmation: B1 user-visible behavior unchanged

No change to KDS/Expo/floor reads, payment gate, READY ≠ SERVED, serving,
coursing, snapshot-at-fire, Unassigned, modifier routing UI, or the legacy
`OrderItem.station_id`. This patch only hardens PreparationTask creation/backfill
under retries and concurrency.

Stopping after this for re-audit — **not** proceeding to B2.
