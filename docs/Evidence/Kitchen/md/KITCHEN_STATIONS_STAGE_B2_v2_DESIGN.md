# Kitchen Stations — Stage B2 Design v2 (KDS reads/acts on PreparationTask)

**Status:** design only — no code, no diff. Revises v1 per audit. Same goal: move
KDS station reads + the per-line READY action from legacy `OrderItem.station_id` to
`PreparationTask`, preserving all approved Stage A/B1 behavior. **No B1 redesign.**

**What changed vs v1 (the 9 audit items):** (1) a state-consistency **bridge** so the
legacy `/kitchen/{order_id}/status` action (used by Expo too) can never diverge a
task-backed item from its tasks; (2) an explicit **deployment boundary** — B2.1 is an
audit checkpoint, not a deployable task-KDS; (3) a **hard readiness gate**, no
hybrid routing fallback; (4) **post-lock refresh** in the READY route; (5) explicit
**PREPARING→READY** task transition; (6) the five architecture decisions recorded;
(7) `kstatus` stays **order-level**; (8) **task-count vs item-count** terminology;
(9) lean B2.1/B2.2 test boundaries.

Grounding: `kitchen_display` (`GET /kitchen`, `sales.py:1798`), station EXISTS over
`OrderItem.station_id` (`:1901`), `_ticket_courses`/`_line_in_station`
(`:1986`), legacy prep-state action `kitchen_status` (`POST /kitchen/{order_id}/status`,
`:2152`) — **also the target of Expo's "Mark course ready"** (`expo.html:60`), Expo
board `expo_display` (`:2073`), B1 `rollup_item_kitchen_status` (`:1628`),
`fired_items_without_tasks` (`:1677`).

---

## 0. Core principle (single authority for task-backed items)

Once an `OrderItem` has PreparationTasks, **kitchen prep state is owned by the tasks**;
`OrderItem.kitchen_status` becomes a **rollup result**, never independently written by
a competing kitchen path. `PreparationTask` state and the `OrderItem` rollup must
never contradict each other. Legacy items with **no** tasks keep the Stage A behavior
as a fallback until migration completes.

---

## 1. Revised rollout & deployment boundary

```
B2 Design v2 → approve
 → B2.1 implementation (task query / grouping / counts only)  → audit
 → B2.2 implementation (task READY + compatibility bridge + rollup) → audit
 → verify fired_items_without_tasks(db) == 0
 → ACTIVATE task-based KDS
```

**B2.1 is an implementation/audit checkpoint, NOT an independently deployable
task-based KDS.** Until B2.2 is approved, production KDS keeps running the Stage A
path. Rationale (BLOCKER 2): if B2.1 shipped alone, a Grill board could show only the
Grill task while its READY button still hit the OrderItem-level route and mark the
whole Burger ready with the Fryer task still preparing — defeating multi-station
tasks. So the code slices are separate and separately audited; **activation is a
single event after B2.2**. No B3/B4 in this work.

---

## 2. Activation gate — hard, no hybrid (BLOCKER 3)

There is **one** routing authority at a time — never a board mixing task station
membership with `OrderItem.station_id`-attributed lines.

```
KDS mode = TASK   if  b2_active AND fired_items_without_tasks(db) == 0
         = STAGE_A otherwise      (full legacy board — "B2 not active")
```

- **`b2_active`** is an explicit operational flag (app setting), turned on only after
  B2.2 is approved and the invariant is confirmed 0.
- If `b2_active` is on but `fired_items_without_tasks(db) > 0`, the KDS **does not**
  serve a hybrid board. It serves the **complete Stage A board** and shows a prominent
  operator notice: *"B2 activation blocked: N fired items without kitchen tasks."*
  Full Stage A is acceptable **only as the "B2 is off" state** — a single authority.
- This removes v1's legacy-fallback-inside-the-task-board idea, which conflicted with
  the task-only-station-routing invariant and left a real visibility hole
  (`/kitchen?station=Grill` uses `EXISTS PreparationTask`, so a task-less Grill line
  never qualifies even though its `OrderItem.station_id=Grill`).

Open sub-decision (§9-a): whether the blocked state is *also* a startup readiness
failure or only the runtime gate above. Recommendation: runtime gate (don't block the
whole app for a KDS-scoped condition), plus a logged startup warning.

---

## 3. B2.1 — KDS reads PreparationTask (task mode)

**Order qualification (unchanged gate):** on the board when
`Order.kitchen_status ∈ (PREPARING, READY)` and `Order.status` not in
served/paid/closed/cancelled. Only *station scoping* and *line rendering* move to
tasks; the order-level gate is unchanged, so no fired order vanishes.

**Station scoping (tasks):** replace the OrderItem EXISTS with an EXISTS over active
tasks:

```
EXISTS ( preparation_task pt JOIN order_item oi ON oi.id = pt.order_item_id
         WHERE oi.order_id = "order".id
           AND pt.kitchen_status IN ('preparing','ready')
           AND ( pt.station_id = :sid  OR  pt.station_id IS NULL ) )
```

selected station → `pt.station_id = :sid`; **Unassigned** → `IS NULL`; **all** → no
predicate. Fired-work membership comes **only** from `PreparationTask.station_id`
(the fire snapshot); never re-read live MenuItem/Modifier routing.

**`kstatus` stays ORDER-level (explicit, BLOCKER-adjacent clarification):** the
top-level Preparing/Ready filter continues to mean `Order.kitchen_status`, and
`status_counts` continues to come from `Order.kitchen_status`. B2 does **not** repoint
`kstatus` at `PreparationTask.kitchen_status`. Station membership + chips become
task-based; a future per-station task-status filter is a separate change, not B2.

**Filter-before-LIMIT / ordering / total:** station EXISTS + `kstatus` applied in SQL
before `order_by(Order.sent_to_kitchen_at)` (oldest first) and `.limit(80)`.
`board_total` counts the same filtered set (subquery COUNT, no LIMIT).

**Counts & terminology (SHOULD-FIX):** per-station **badges count active
PreparationTasks** (a task is the unit of station work), grouped by
`COALESCE(pt.station_id → 'unassigned')`, with `_view_mine` applied. A multi-station
Burger legitimately contributes Grill +1 and Fryer +1. To avoid implying extra
sales, the UI keeps two distinct quantities:
- **`total_items`** — sale-item quantity (`OrderItem.quantity`), the ticket header /
  expo tray count. Unchanged meaning.
- **station work count** — the task badges. Labeled as station work, never "items".

**Inactive stations with live tasks:** `tab_ids` from the station keys in the task
counts ∪ active stations — an inactive station with active tasks keeps its tab.

**Coursing:** group by snapshotted `task.course`; held courses have no fired tasks.

**N+1:** eager-load `Order.items → OrderItem.tasks → PreparationTask.station`
(selectinload; one SELECT per level). Tab metadata from the aggregate, not the page.

**No mutation in B2.1.**

### Ticket presentation (decision #2: OrderItem + task sub-rows)

Render the sale line once, tasks as sub-rows/chips — never as duplicate items:

```
Burger ×1
├── Grill      READY
├── Fryer      PREPARING
└── Cold Prep  READY
```

- **Station board:** show only that station's task for the item; only that task is
  actionable (its READY button — B2.2).
- **all board:** course → `OrderItem` → per-station task chips.

**Course / line status shown on `all` (decision #3): from the rolled-up
`OrderItem.kitchen_status`.** Tasks determine *station* work; the OrderItem rollup is
the compatibility status the rest of the app (order/payment) uses, so `all` and
station boards agree with order state.

---

## 4. B2.2 — per-task READY (task mode)

**Route:** `POST /kitchen/tasks/{task_id}/ready`, `require("kitchen.update")`,
redirect via `_safe_next_board(next)`.

**Sequence (post-lock refresh — SHOULD-FIX):**
1. `db.get(PreparationTask, task_id)` — only to learn its `order_id`; 404 if missing.
2. **Lock parent Order** `SELECT … FROM "order" WHERE id=:oid FOR UPDATE` (no-op on
   SQLite) — decision #5, matches fire.
3. **Refresh from post-lock committed state:** re-select the task **and its sibling
   `OrderItem.tasks`** with `populate_existing=True` (or `db.refresh(...,
   ['kitchen_status','ready_at'])` + refresh the item's `tasks`), so the transition
   and rollup act on **current** rows, not pre-lock identity-map objects. The design
   asserts: the rollup operates on post-lock state.
4. **Explicit transition (SHOULD-FIX):**
   - `PREPARING → READY` (set `ready_at = now`).
   - `READY → READY` — idempotent no-op; **preserve the original `ready_at`** (do not
     overwrite). Fall through to rollup + redirect.
   - `SERVED → READY` (or anything else) — **rejected** (409/400). The contract is
     *not* "anything not READY becomes READY".
5. Never set task/OrderItem/Order to SERVED.
6. `rollup_item_kitchen_status(task.order_item)` — approved B1 fn, unchanged
   (item READY only when every non-served task READY; else PREPARING; never SERVED).
7. `_recompute_kitchen(order, now)` — Order/table state + ready-to-pay derive exactly
   as the existing line flow; **payment gate byte-for-byte unchanged**.
8. `db.commit()`.

**Concurrency:** both txns take `FOR UPDATE` on the Order row → serialized; each
refreshes after the lock (step 3), so the second sees the first's committed task
state. First: A READY, rollup sees B preparing → item PREPARING. Second: B READY, all
ready → item READY → Order READY. Correct regardless of arrival order. **Duplicate
READY:** step 4 preserves `ready_at`, truly idempotent. SQLite's global write lock
gives the same serialization.

---

## 5. Compatibility bridge for legacy prep-state writes (BLOCKER 1)

The shared legacy action `kitchen_status` (`POST /kitchen/{order_id}/status`) — used
by the KDS **and Expo's "Mark course ready"** — currently writes
`OrderItem.kitchen_status` directly. Once B2 is active this can diverge a task-backed
item from its tasks (Expo marks Burger READY while Grill/Fryer tasks are PREPARING).

**Bridge (in that same route):** after loading the order and **locking it FOR UPDATE +
refreshing**, split the targets:

- **Task-backed OrderItem** (`item.tasks` non-empty): do **not** write
  `item.kitchen_status`. Translate the request through its **active** (non-served)
  tasks, then rollup:
  - request **READY** → set the item's active tasks to READY (`ready_at=now`) →
    `rollup_item_kitchen_status(item)` → item becomes READY.
  - request **PREPARING** → reset the item's active tasks to PREPARING (clear
    `ready_at`) → rollup → item PREPARING. (Supports an Expo/KDS "un-ready" override.)
  - request **PENDING** → **not supported for task-backed items in B2** (un-firing
    tasked work needs the re-fire/remake model deferred past B2). Leave the item
    unchanged; surface a clear rejection. UI never offers PENDING for these lines.
- **Legacy OrderItem with no tasks:** unchanged Stage A behavior (direct
  `item.kitchen_status = status`) as the fallback until migration completes.

Then `_recompute_kitchen(order, now)` and commit as today. This keeps Expo **visually
Stage A** (no rendering change until B3) while making its underlying writes
task-consistent. Same invariant everywhere: **tasks and the OrderItem rollup never
contradict.**

Note: this bridge only matters once B2 is active (task-backed items exist on the
board). Because activation is gated on `fired_items_without_tasks == 0`, every fired
item on an active B2 board is task-backed and flows through the task branch.

---

## 6. Decisions on the five open questions (recorded)

1. **Activation strictness → hard gate** (§2). No hybrid task/legacy station routing.
2. **`all` grouping → OrderItem with task sub-rows/chips** (§3). Not flattened.
3. **Course/status source on `all` → rolled-up `OrderItem.kitchen_status`** (§3).
4. **Task SERVED on serve → leave tasks READY** in B2; no task-SERVED propagation;
   the OrderItem/Order serving lifecycle already removes work from the board; defer
   task served/reporting to B3/B5.
5. **Lock granularity → parent Order row lock**, with post-lock refresh (§4, §5).

---

## 7. Backward compatibility (unchanged in B2)

Payment gate; serving semantics; READY ≠ SERVED; coursing; held courses create no
active tasks; **Expo & Floor stay on the Stage A read path** (B3) — only Expo's
*write* goes through the bridge; menu-routing UI; modifier-routing UI (none yet);
`OrderItem.station_id` legacy field kept; PreparationTask creation/backfill rules;
`kstatus` order-level semantics.

---

## 8. Files expected to change

**B2.1 (read-only):** `app/routers/sales.py` — `kitchen_display` task station EXISTS,
task-based counts, selectinload tasks→station, `tab_ids` from task counts, task-based
grouping (new helper; legacy `_ticket_courses`/`_line_in_station` retained for the
Stage-A/`b2_active`-off path); KDS-mode selector (`task_kds_active(db)`).
`web/templates/kitchen.html` — item→task sub-rows, badges labeled as station work.

**B2.2:** `app/routers/sales.py` — new `POST /kitchen/tasks/{task_id}/ready`
(post-lock refresh + explicit transition); **`kitchen_status` bridge** (§5) with Order
lock + refresh; the `b2_active` flag surface. `web/templates/kitchen.html` — per-task
READY button.

**No model change, no migration** — `PreparationTask` already has `kitchen_status`
and `ready_at`. The `b2_active` flag reuses the existing settings mechanism.

---

## 9. Lean test boundaries (implementation-time; reuse Stage A/B1 regression)

**B2.1**
- station query uses the PreparationTask snapshot;
- Unassigned (`station_id IS NULL`) visible on its tab and on `all`;
- filter applied before LIMIT; `board_total` matches;
- inactive station with live tasks keeps its tab;
- multi-station item renders once (sub-rows), quantities not inflated;
- **activation blocked** when `fired_items_without_tasks > 0` → full Stage A board +
  notice, never a hybrid.

**B2.2**
- one task READY does **not** ready a sibling task;
- all required tasks READY → OrderItem READY (rollup);
- duplicate READY preserves original `ready_at` (idempotent);
- concurrent sibling READY → correct final rollup (post-lock refresh);
- **legacy `kitchen_status`/Expo path cannot diverge** a task-backed item (READY and
  PREPARING route through tasks; PENDING rejected for task-backed);
- tasks never cause SERVED;
- payment behavior unchanged;
- explicit transition: `SERVED→READY` rejected.

---

## Remaining open decisions (need your call)

- **(a) Activation failure mode:** runtime gate only (recommended) vs. also a startup
  readiness failure when `b2_active` and invariant > 0.
- **(b) `b2_active` toggle mechanism:** a manual app setting flipped after verifying
  invariant==0 (recommended) vs. an automatic "activate when invariant first hits 0"
  latch. Manual is safer and auditable.
- **(c) Legacy PREPARING override on task-backed items:** reset *all* the item's
  active tasks to PREPARING (recommended, matches the coarse legacy button) — confirm
  this is the desired coarse semantic, given B2.2 also offers precise per-task control.
