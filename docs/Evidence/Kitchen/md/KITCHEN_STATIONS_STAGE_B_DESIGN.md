# Kitchen Stations — Stage B design (PreparationTask)

**Status:** design only — no code, no diff. For architectural review before any
implementation. Stage A is approved & closed; nothing here changes Stage A
behavior until a slice is explicitly authorized.

## 0. Goal & the core shift

Stage A model: **one `OrderItem` → one station** (`OrderItem.station_id`,
snapshotted at fire). Stage B introduces the ability for **one `OrderItem` to
generate work at several stations at once**, driven mainly by modifiers
(e.g. a burger to Grill, its fries add-on to Fryer, its special sauce to Cold
Prep) — **without duplicating the sale line**.

The new unit of kitchen work is the **`PreparationTask`**. The sale line
(`OrderItem`) is unchanged as the unit of *billing*; `PreparationTask` becomes
the unit of *preparation*. The KDS, Expo and floor read tasks; billing/payment
keep reading `OrderItem.kitchen_status`.

### Guarantees preserved (non-negotiable)
`OrderItem.kitchen_status` stays the source of truth for the order/payment flow
(it becomes a **derived rollup** of the item's tasks — persisted onto the same
column, so every existing reader is unchanged) · READY ≠ SERVED · payment gate
unchanged · coursing unchanged · Stage A single-station routing stays
backward-compatible · fired work is snapshotted and immutable under later
menu/modifier re-routing · Unassigned work never disappears · KDS/Expo/floor keep
working during migration · no websockets/SSE — the 15s poll stays.

---

## 1. `PreparationTask` data model

New table `preparation_task`. One row per **(fired OrderItem × target station)**.

| Field | Type | Notes |
|---|---|---|
| `id` | PK | |
| `order_item_id` | FK → `order_item.id` (CASCADE) | the sale line it belongs to |
| `order_id` | FK → `order.id` | denormalized for cheap board queries / grouping |
| `station_id` | FK → `station.id`, **nullable** | **snapshot at fire**; NULL = Unassigned |
| `course` | int | snapshot of `OrderItem.course` at fire (coursing) |
| `kitchen_status` | str | task lifecycle (below); default `preparing` (created at fire) |
| `quantity` | int | snapshot of the line quantity |
| `item_label` | str | snapshot of the item name (receipt-stable, survives menu edits) |
| `is_base` | bool | true = the item's own station task; false = a modifier-only task |
| `fired_at` | datetime | when created (drives KDS elapsed/urgency) |
| `ready_at` | datetime \| null | when marked ready (metrics: fired→ready) |
| `served_at` | datetime \| null | when delivered |
| `nonce` | str \| null | fire idempotency token (see §11) |
| `created_at` / `updated_at` | datetime | |

**Modifier detail per task** — a task must show *which* modifiers it is
responsible for (e.g. the Fryer task says "Fries"; the Grill task says "Burger ·
extra cheese"). Two clean options (decision for review, §15):

- **(preferred) `preparation_task_modifier`** child rows: `(id,
  preparation_task_id, source_kind ['modifier'|'option'|'base'],
  source_id nullable, label snapshot, station_id snapshot)`. Normalized, no
  arrays; each selected modifier/option that routes to this task contributes one
  row with a snapshotted label. Avoids putting arrays/CSV on `OrderItem`.
- (alt) a snapshot `labels` **JSON/text column** on the task. Simpler, but weaker
  for future per-modifier metrics. **Recommendation: the child table.**

**Relationships**
- `OrderItem.tasks` → `list[PreparationTask]` (cascade delete, `lazy="select"`;
  KDS/Expo/floor `selectinload` it — never eager globally, per the Stage A N+1
  lesson).
- `PreparationTask.station` → `Station` (`lazy="select"`, selectinloaded by the
  board queries).
- `PreparationTask.order_item` / `.order`.

**Indexes**
- `(order_id)` and `(order_item_id)` for grouping.
- `(station_id, kitchen_status)` for station-board queries.
- `(kitchen_status)` for board scoping.
- **`UNIQUE(order_item_id, station_id)`** — one task per item per station; makes
  fire idempotent and blocks duplicates under concurrency (see §11). *(SQLite
  treats multiple NULLs as distinct, so an item can only ever have one
  base/Unassigned task anyway because there is exactly one base task per item;
  the app guards that. On Postgres, `NULLS NOT DISTINCT` or a partial unique
  index can enforce it if we want DB-level strictness — a §15 decision.)*

### Task status lifecycle
Default (backward-compatible with the `OrderItem` lifecycle):

```
preparing → ready → served
```

Created at fire as `preparing` (already sent). Optional richer states are
**deferred** and additive, not required for B1: `accepted` / `started` (before
preparing), and side states `held` / `cancelled` / `voided` / `recalled` /
`remake`. B1 ships the minimal three; richer states land only when a KDS
accept/start flow is approved. This keeps the rollup (§4) trivial and safe.

### Independent vs derived
Task status is the **granular truth**; `OrderItem.kitchen_status` is a
**derived rollup** persisted onto the existing column (§4). This is the crux
decision: we do **not** make payment read tasks directly — payment keeps reading
`OrderItem.kitchen_status` / `Order.kitchen_status` exactly as today, so the gate
cannot regress.

---

## 2. Snapshot semantics — exactly when tasks are created

- **Tasks are created at FIRE time**, never at add time (matches Stage A). Before
  fire, `OrderItem.kitchen_status = pending` and it has **zero tasks**.
- Firing an item (`/orders/{id}/send` for a course, `/items/{id}/fire` for a
  line) resolves the routing **at that instant** and writes the tasks with
  snapshotted `station_id`, `course`, `item_label`, and modifier labels.
- **Routing resolution at fire** for one item:
  1. `base_station = OrderItem.menu_item.station_id` (may be NULL = Unassigned).
  2. For each selected modifier (`OrderItemModifier → Modifier.station_id`) and
     each selected option (`OrderItemOption → ModifierOption.station_id`): if it
     has an explicit station **different** from `base_station`, it contributes to
     a task at *that* station; otherwise it folds into the base task.
  3. Task set = `{ base_station }` (always, even if NULL) `∪ { distinct explicit
     modifier stations }`. One task per distinct station.
- **Immutability:** once written, a task's `station_id` and labels never change.
  Editing `MenuItem.station_id`, `Modifier.station_id` or `ModifierOption.station_id`
  afterwards affects only **future** fires. (Same rule as Stage A's
  `OrderItem.station_id` snapshot.)
- **`OrderItem.station_id` is retained** through B1–B4 as the legacy single-station
  snapshot = the **base task's** station (the "primary" station), keeping every
  Stage A reader valid during migration. Its fate is decided in B5 (§12/§15).

---

## 3. Multi-station worked example

Order line: **1× Burger** (`OrderItem`, `menu_item.station_id = Grill`), with
selected modifiers **+ Fries** (`Modifier.station_id = Fryer`) and **+ Special
sauce** (`ModifierOption.station_id = Cold Prep`), plus **+ extra cheese**
(`station_id = NULL` → folds into base).

At fire, **one sale line** produces **three tasks** (no duplicated billing):

```
OrderItem #501  Burger  ×1   course 2   kitchen_status → (rollup)
├─ PreparationTask  station=Grill      is_base=true   labels: [Burger, extra cheese]
├─ PreparationTask  station=Fryer      is_base=false  labels: [Fries]
└─ PreparationTask  station=Cold Prep  is_base=false  labels: [Special sauce]
```

The Grill screen sees "Burger · extra cheese"; the Fryer screen sees "Fries (for
Table X's Burger)"; Cold Prep sees "Special sauce". The bill still shows one
Burger line with its modifier price deltas — unchanged.

If the Burger's own station were Unassigned, the base task would carry
`station_id = NULL` and appear in the Unassigned bucket; the Fryer/Cold tasks are
unaffected.

---

## 4. `OrderItem.kitchen_status` rollup (compatibility)

The parent line's status is **derived from its tasks** and written to the same
`kitchen_status` column, so `_recompute_kitchen` and the payment gate keep
working unchanged. Per-item rule:

- **No tasks** (not fired) → `pending`.
- **All tasks `served`** → item `served`.
- **All not-served tasks `ready`** (and ≥1 exists) → item `ready`.
- **Otherwise** (any task still `preparing`) → item `preparing`.

**"When does the item become READY?"** — only when **every** required task for
that fired item is `ready` (or already `served`). A burger is not ready to run
until Grill, Fryer *and* Cold Prep are all up. This is the key behavioral win and
it strengthens, without changing, the existing gate.

- **Order rollup** (`Order.kitchen_status`) and **served/payment** semantics are
  **unchanged** — `_recompute_kitchen` still derives the order from its items;
  we only change *how each item's* status is computed (tasks instead of the item
  flipping directly). READY ≠ SERVED is untouched: serving delivers `ready` work,
  tasks flip to `served`, and served tasks leave the board (as served items do
  today).
- Marking an item/course served (waiter action) cascades to its tasks
  (`ready → served`); a re-fire creates new `preparing` tasks, exactly mirroring
  today's re-fire behavior.

---

## 5. Coursing

- Each task snapshots `course` from its `OrderItem` at fire.
- **A held later course generates no tasks** — tasks exist only for *fired*
  items, and coursing already controls what gets fired. Dessert (course 3) held
  back produces zero station work until it's fired; it cannot block course 2.
- KDS/Expo group by `course` using the task's snapshotted `course` (identical to
  Stage A's per-line course, just sourced from the task). A course is "ready"
  when all its tasks (across stations) are ready — see Expo (§7).

---

## 6. KDS — querying `PreparationTask`

The station boards stop scanning `OrderItem.station_id` and query
`preparation_task` directly.

- **Production station tab** (`station=<id>`): tasks where
  `station_id = <id>` AND `kitchen_status IN (preparing, ready)` AND the parent
  order is on-board.
- **Unassigned tab**: tasks with `station_id IS NULL`. Same "never hidden" rule
  as Stage A — the All board shows them and the tab surfaces them.
- **Inactive station with live work**: the station-tab set = active stations ∪
  stations referenced by any on-board task (even inactive) — identical to the
  Stage A rule, now keyed on tasks. Snapshots keep the tab reachable.
- **Order/ticket grouping**: filter tasks in SQL (join `order`), then group in
  Python by `order_id` → course → the station's tasks, so a ticket shows one card
  per order with only that station's lines. `board_total` counts the filtered set
  (same "showing N of M" pattern).
- **Limits/performance**: keep the **KDS LIMIT 80 / Expo LIMIT 60** *orders*
  (not tasks) — filter+limit orders in SQL first, then load their tasks.
- **Avoiding N+1**: one query for the on-board orders (filtered/limited), one
  `selectinload`/`IN` query for their tasks + `selectinload(PreparationTask.station)`.
  No per-row lazy loads. Aggregate counts (per-station tab badges) via a single
  `GROUP BY station_id` over the filtered task set — the Stage A approach, on
  tasks.

The KDS marks a **task** ready/preparing (not the whole item); the item rollup
(§4) then re-derives and `_recompute_kitchen` updates the order — so the existing
"mark ready" endpoint gains a `task_id` target alongside the current
`item_id`/`course`/order targets.

---

## 7. Expo aggregation

Hierarchy: **Order → Course → Station → PreparationTasks**.

- **Station (within a course) is ready** when *all* its tasks in that course are
  `ready`.
- **Course is ready to serve** when *every* station present in that course is
  ready (i.e. all the course's tasks are ready). Until then, "Waiting on
  {stations not ready}".
- Held later courses have no tasks, so they never appear as "waiting" and never
  block the current course (§5).
- Same LIMIT 60 orders; same selectinload discipline. This is the Stage A Expo
  generalized from "lines grouped by station" to "tasks grouped by station",
  which is strictly more correct for multi-station items.

---

## 8. Floor plan strip

The occupied-table strip aggregates the order's **tasks** by station (instead of
the item's single snapshot):

- For each distinct station among the order's non-served tasks: a chip, `ready`
  only when all that station's tasks are ready.
- **The card's aggregate status is unchanged** — it still comes from
  `Order.kitchen_status` / the derived `disp_status`. The strip is display-only
  detail, exactly as in Stage A; only its data source changes (tasks, not
  `OrderItem.station_id`). Unassigned tasks show the ⚠ chip.

---

## 9. Modifier routing configuration

Add a **nullable `station_id` FK** to the modifier *definitions*:

- `Modifier.station_id` (legacy flat modifiers).
- `ModifierOption.station_id` (grouped options).

**No arrays, no comma-separated ids** anywhere. Each modifier/option routes to at
most **one** production station. The *multiplicity* (one item → many stations)
emerges from the **set** of the item's + its modifiers' single-station routes at
fire — never from a multi-valued column.

- Admin UI: a "Station" dropdown (active production stations only, reusing the
  Stage A `_routed_station` validation) on the modifier and modifier-option
  editors. NULL = "prepare with the item" (folds into the base task).
- Bulk routing for modifiers can reuse the Stage A CSV/XLSX importer pattern in a
  later slice if needed (not required for B4).

---

## 10. Backward compatibility during migration

Two code paths must coexist while B1→B3 roll out:

- **New fires** create tasks.
- **Legacy fired items** (fired under Stage A, before B1) have
  `OrderItem.station_id` but **no tasks**.

**Recommended:** the B1 migration **backfills exactly one `PreparationTask` per
currently-fired `OrderItem`** from its `station_id` snapshot (is_base=true,
status = the item's current kitchen_status mapped to task status, `fired_at =
sent_to_kitchen_at`). After backfill, the entire board is task-based — a single
code path, no dual-read shim. The backfill is idempotent (guarded by the UNIQUE
constraint / "item already has tasks" check).

Until B2 flips the KDS to read tasks, the Stage A KDS keeps reading
`OrderItem.station_id`, which the rollup keeps accurate — so B1 is invisible to
users.

---

## 11. Migration (additive, SQLite + Postgres)

- `create_all` makes `preparation_task` (+ `preparation_task_modifier` if we take
  the child-table option).
- `ADDED_COLUMNS` (idempotent, guarded, both dialects): `modifier.station_id`,
  `modifier_option.station_id` (nullable FK). `order_item.station_id` **stays**.
- The B1 backfill runs once (guarded), from `order_item` where
  `kitchen_status IN (preparing, ready)`.
- Cross-dialect: nullable INTEGER FK columns and a new table are the same safe
  additive pattern used throughout `migrate.py`. The `UNIQUE(order_item_id,
  station_id)` constraint is created with the table (fresh DBs) — for the DB-level
  NULL-distinct nuance see §1/§15; the app-level guard is the primary safety net.

---

## 12. Failure & concurrency

- **Duplicate fire requests**: task creation is idempotent — a fire only creates
  tasks for items transitioning `pending → preparing`, and the
  `UNIQUE(order_item_id, station_id)` constraint makes a re-attempt a no-op.
  Reuse the Stage A one-time **nonce** pattern on the fire endpoint (store on
  `PreparationTask.nonce`) so a double-submit can't double-create.
- **Partial task creation**: all tasks for one fire + the item status flip happen
  in **one transaction**. A failure rolls the whole thing back — no half-fired
  item, no orphan tasks.
- **Transaction rollback**: same — atomic per fire.
- **Modifier changed after fire**: tasks snapshot station + labels → immutable;
  later config edits touch only future fires.
- **Station deactivated after fire**: task keeps its `station_id`; KDS shows the
  inactive-station-with-live-work tab (Stage A rule). Only future routing is
  affected.
- **Concurrent task updates**: marking a task `ready` is idempotent (set-to-value,
  not increment); last-writer-wins with `updated_at`. The item rollup is
  recomputed from the current task set after each update, so concurrent readies
  converge to the correct item status.
- **Retries / idempotency**: nonce on fire + UNIQUE(order_item, station) + the
  "only fire pending" guard together make fires safely retryable.

---

## 13. Testing strategy (before implementation)

Unit / integration cases to lock behavior:

**Creation & snapshot**
- Fire a single-station item → 1 base task; `station_id` snapshot correct.
- Fire an item with modifiers at 2 other stations → 3 tasks, one base + two
  modifier tasks; billing line count unchanged (still 1 `OrderItem`).
- Modifier with NULL station folds into the base task (no extra task).
- Re-route `MenuItem`/`Modifier`/`Option` after fire → existing tasks unchanged.
- Base item Unassigned + a routed modifier → base task NULL (Unassigned) + the
  modifier task; Unassigned reachable.

**Rollup / lifecycle / payment**
- Item ready only when **all** its tasks ready; not before.
- Order not payable while any task preparing (gate unchanged).
- READY ≠ SERVED: serving flips ready tasks → served; re-fire creates new tasks.
- Legacy fired item (no tasks) after B1 backfill behaves identically.

**Coursing**
- Held course 3 creates no tasks; course 2 readiness independent.

**KDS / Expo / floor**
- Station tab shows only its tasks; Unassigned tab shows NULL-station tasks.
- Inactive station with live tasks stays on the tab list.
- Expo "waiting on X" until all stations in the course are ready.
- Floor strip aggregates tasks; card aggregate status unchanged.
- LIMIT/counts correct; no N+1 (assert query counts).

**Concurrency / idempotency**
- Duplicate fire (same nonce) → tasks created once.
- Concurrent "mark ready" on two tasks of one item → item becomes ready exactly
  when both are ready.
- Deactivate station mid-service → fired tasks still visible & snapshot intact.

---

## 14. Rollout plan (sliced — recommended)

Do **not** ship all of Stage B at once. Each slice leaves the app fully working
and is separately reviewable/approvable.

- **B1 — model + creation/snapshot + backfill.** Add `preparation_task` (+ child
  table), modifier `station_id` columns, task creation at fire, the rollup into
  `OrderItem.kitchen_status`, and the legacy backfill. KDS/Expo/floor still read
  Stage A data (unchanged UX). Ships invisibly; fully tested.
- **B2 — KDS reads tasks.** Flip the station boards to query tasks; add per-task
  mark-ready. Remove the item-only KDS path.
- **B3 — Expo + floor aggregation on tasks.** Order→Course→Station→Tasks.
- **B4 — modifier multi-routing config.** Admin UI for `Modifier.station_id` /
  `ModifierOption.station_id`; now one item genuinely fans out to N stations.
- **B5 — cleanup / legacy-field decision.** Decide `OrderItem.station_id`: keep as
  the derived "primary station" (base task) for reporting, or drop. Optional DB
  unique-constraint hardening; optional richer task states (accept/start).

Recommendation: authorize **B1 only** after this design is approved; review B1
before B2.

---

## 15. Risks / decisions requiring approval

1. **`OrderItem.kitchen_status` becomes a derived rollup of tasks** (persisted to
   the same column). Payment/floor read it unchanged, but this is the one place
   Stage B touches the critical path — must be reviewed as payment-adjacent.
2. **Modifier detail storage**: child table `preparation_task_modifier` (preferred,
   normalized, no arrays) vs. a snapshot JSON column. Affects future per-modifier
   metrics.
3. **`UNIQUE(order_item_id, station_id)`** and the NULL-distinct nuance
   (SQLite vs Postgres) for the base/Unassigned task — app-guard only, or add a
   DB-level partial/`NULLS NOT DISTINCT` constraint.
4. **B1 backfill of live fired items into tasks** — a one-time data migration
   over open orders; needs the same "dev DB only until approved for prod" care.
5. **Fate of `OrderItem.station_id`** at B5 — keep as legacy primary vs drop.
6. **Task lifecycle richness** — ship minimal `preparing→ready→served` now, defer
   `accepted/started/held/recalled/remake` (they're additive; deferring keeps the
   rollup and payment path simple).
7. **Modifier routing surfaces** — both legacy `Modifier` and grouped
   `ModifierOption` get `station_id`; confirm both are in scope for B4.
8. Still **out of scope** (Stage A backlog, not part of B): DB-level unique
   station names, duplicate item-ids in one import, CSV formula-injection.

No production code was modified and no diff was generated. Awaiting architectural
review of this document before authorizing B1.
