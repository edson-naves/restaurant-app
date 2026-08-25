# Kitchen Stations — v4 (Stage B1: PreparationTask model + creation/snapshot + backfill)

**Version:** v4 · **Scope:** Stage **B1 only**. Introduces `PreparationTask`
*underneath* the current system. **KDS / Expo / floor are NOT changed** and do
not read tasks yet (that is B2/B3). No user-visible behavior changes.
**Pair:** this handoff + `KITCHEN_STATIONS_v4.diff` (full cumulative feature state
at v4). B1-specific changes are marked "Stage B1" in the code.

## Files changed in B1

- `app/models/oltp.py` — `PreparationTask`, `PreparationTaskModifier`,
  `PreparationTaskStatus`; nullable `Modifier.station_id` / `ModifierOption.station_id`;
  `OrderItem.tasks` relationship. `OrderItem.station_id` kept unchanged (legacy/primary).
- `app/migrate.py` — additive `modifier.station_id` / `modifier_option.station_id`;
  idempotent `_backfill_prep_tasks` (SQLite + Postgres).
- `app/routers/sales.py` — task creation at fire (`_create_tasks_for_item`),
  `rollup_item_kitchen_status`, and an order row-lock in the two fire routes.
- `tests/test_prep_tasks.py` — 11 B1 test cases.

## Schema (additive; SQLite + Postgres)

- New tables via `create_all`: `preparation_task`
  (order_item_id, order_id, station_id NULL, course, kitchen_status, quantity,
  item_label, is_base, fire_seq, fired_at, ready_at, served_at, timestamps) and
  `preparation_task_modifier` (task_id, source_kind, source_id, label, station_id).
- New columns via guarded `ADD COLUMN`: `modifier.station_id`,
  `modifier_option.station_id` (nullable FK → station).
- **No `UNIQUE(order_item_id, station_id)`** (per instruction #3) — a future
  re-fire/remake may legitimately add same-`(item, station)` tasks.
- `OrderItem.station_id` **retained unchanged** as the Stage A legacy/primary
  snapshot. No Stage A column removed or repurposed.

## Idempotency / task identity (chosen rule)

Tasks are created **only** while an item transitions `pending → preparing` during
a fire, and **only** for items currently `pending`. Two guards make repeated or
concurrent fires safe **without** a uniqueness constraint:

1. **Transactional serialization** — each fire route takes a row lock on the
   `Order` (`SELECT … FOR UPDATE`; a no-op on SQLite, which already serializes
   writers). A concurrent/second fire waits, then re-reads the now-`preparing`
   items and creates nothing.
2. **Creation guard** — `_create_tasks_for_item` returns immediately if the line
   already has tasks.

A repeated fire request hits "nothing to fire" (400) and creates no tasks. This
leaves the door open for legitimate future same-station tasks (re-fire/remake).

## Task creation & snapshot (at fire)

At fire, one task per **distinct target station**:
`{ base = MenuItem.station_id } ∪ { explicit Modifier.station_id / ModifierOption.station_id }`.
Modifiers with no station (or the item's station) fold into the base task. The
sale line is **not** duplicated. Each task snapshots `station_id`, `course`,
`quantity`, `item_label`, `fired_at`; modifier/option details are snapshotted as
`preparation_task_modifier` rows (labels + station). Editing MenuItem/Modifier/
ModifierOption routing afterwards never moves existing tasks (verified by test).
An Unassigned item produces a base task with `station_id = NULL` (explicit).

## Rollup semantics (`rollup_item_kitchen_status`)

`OrderItem.kitchen_status` is derived from its tasks: **ready only when every
non-served task is ready; otherwise preparing**. It **never sets `served`**
(served stays owned by the existing serving flow) and a line with **no tasks is
left untouched** (legacy safe). In B1 the rollup drives only the `fire → preparing`
transition; **B2** makes it the live authority once the KDS marks individual
tasks. READY ≠ SERVED and the payment gate are unchanged (verified).

## Backfill

`_backfill_prep_tasks` creates **one base task per already-fired `OrderItem`**
(`preparing`/`ready`) that has none, from its existing `OrderItem.station_id`
snapshot (`is_base`, `fired_at = order.sent_to_kitchen_at`). Idempotent
(`NOT EXISTS` guard), never touches `order_item`, never re-reads current MenuItem
routing, preserves Unassigned as NULL, safe to re-run. Ran on the dev DB:
**7225 tasks backfilled = 7225 fired items** (no duplication).

## Transactionality & concurrency covered

Duplicate fire (no-op 400) · retry after failure (only pending fires) · partial
creation / rollback (tasks + status flip in one transaction) · concurrent fires
(order row lock) · modifier/menu re-routing after fire (snapshot immutable) ·
station deactivated after fire (task keeps its snapshot).

## Tests (all pass)

`tests/test_prep_tasks.py` (11 cases): single-station · multi-station snapshot ·
Unassigned · modifier-detail snapshot · re-route-after-fire immutability ·
all-tasks-ready→ready · one-not-ready→not-ready · rollup-never-served ·
legacy-no-tasks-safe · held-course-no-task · duplicate-fire idempotent · backfill
idempotent · payment-gate unchanged.
Regression: `test_stations.py`, `test_floormap.py`, `test_reservations_map.py`
all green; `app.main` imports; dev DB migrates + backfills.

## Explicitly NOT done in B1 (later slices)

KDS task-based reads / per-task ready actions (**B2**) · Expo/floor task
aggregation (**B3**) · modifier routing UI (**B4**) · removal of
`OrderItem.station_id` · richer task states · Stage B cleanup. Stage A backlog
(DB unique station name, duplicate item-ids in one import, CSV formula-injection)
still deferred.

Stopping after B1 for audit — not proceeding to B2.
