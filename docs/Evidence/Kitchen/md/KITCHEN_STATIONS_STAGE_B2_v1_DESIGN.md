# Kitchen Stations — Stage B2 Design v1 (KDS reads/acts on PreparationTask)

**Status:** design only — no code, no diff. **Goal:** move KDS station reads and the
per-line READY action from the legacy `OrderItem.station_id` snapshot to
`PreparationTask`, preserving every approved Stage A / B1 behavior. Split into two
audited slices: **B2.1** (reads) then **B2.2** (per-task READY + rollup). No B3/B4.

Grounding (current code): the KDS is `kitchen_display` (`GET /kitchen`,
`app/routers/sales.py:1798`); station scoping today is an `EXISTS` over
`OrderItem.station_id` (`:1901`) + `_ticket_courses`/`_line_in_station`
(`:1986`–`:2030`); per-line status is `POST /kitchen/{order_id}/status`
(`kitchen_status`, `:2152`). B1 gives us `PreparationTask` (fields incl.
`station_id` nullable, `course`, `kitchen_status` ∈ PREPARING/READY/SERVED,
`ready_at`, `fire_seq`, rel. `station`, `order_item`), `rollup_item_kitchen_status`
(`:1628`), and `fired_items_without_tasks` (`:1677`).

---

## B2.1 — KDS reads PreparationTask

**Order qualification (unchanged gate):** an order is on the board when
`Order.kitchen_status ∈ (PREPARING, READY)` and `Order.status` not in
served/paid/closed/cancelled. That gate stays as-is — so **no fired order can
disappear** because of task rewiring; only *station scoping* and *line rendering*
switch to tasks.

**Station scoping (switched to tasks):** replace the `OrderItem.station_id` EXISTS
with an EXISTS over active `PreparationTask` for that order:

```
EXISTS ( PreparationTask pt JOIN OrderItem oi ON oi.id = pt.order_item_id
         WHERE oi.order_id = Order.id
           AND pt.kitchen_status IN ('preparing','ready')
           AND ( pt.station_id = :sid                    -- a station tab
                 OR pt.station_id IS NULL )              -- 'unassigned' tab
       )
```

- **selected station** → `pt.station_id = :sid`.
- **Unassigned** → `pt.station_id IS NULL` (the base task of an un-routed item;
  never hidden — it always also appears on `all`).
- **all** → no station predicate.

Fired-work station membership comes **only** from `PreparationTask.station_id`
(the fire snapshot). We never re-read live `MenuItem`/`Modifier` routing for
existing tasks.

**Filter-before-limit / ordering / total:** the station EXISTS and the
`kstatus` predicate are applied in SQL *before* `.order_by(Order.sent_to_kitchen_at)`
(oldest-fired first) and `.limit(KITCHEN_LIMIT=80)`, exactly as today. `board_total`
counts the same filtered set (subquery `COUNT(*)`, no LIMIT) so "showing N of M"
stays honest.

**Counts (from tasks):** the per-station badge counts move from
`GROUP BY OrderItem.station_id` to a single aggregate over active tasks:

```
SELECT COALESCE(pt.station_id, 'unassigned'), COUNT(*)
FROM preparation_task pt JOIN order_item oi ... JOIN "order" o ...
WHERE <board gate> AND pt.kitchen_status IN ('preparing','ready')
GROUP BY pt.station_id
```

`all` badge = sum of the group; the same `_view_mine` (channel view + "my tables")
is applied so a badge matches the population its tab reveals. `status_counts`
(Preparing/Ready over the board) stays driven by `Order.kitchen_status` — unchanged.

**Inactive stations with live tasks:** derive `tab_ids` from the station keys
present in the task counts (same pattern as today at `:1959`), union active
stations. An inactive station that still has active tasks keeps its tab and its
work stays reachable — tasks never migrate to Unassigned.

**Coursing:** tasks carry a snapshotted `course`. Group by `task.course`; held
courses have no fired tasks, so they don't appear — preserved.

**N+1 avoidance:** eager-load one bounded chain instead of per-row lazy loads:
`selectinload(Order.items) → selectinload(OrderItem.tasks) → selectinload(PreparationTask.station)`
(one extra SELECT per level, not per order). Station-tab metadata comes from the
single aggregate above, not by scanning the capped page.

**No mutation in B2.1** — reads only.

---

## Ticket presentation (multi-station item)

A single sale line (`OrderItem` "Burger") may own several tasks. We render the
**item once**, with its tasks as sub-rows — never as duplicate items:

```
Burger              (qty from OrderItem, shown once)
├── Grill    ⏳
├── Fryer    ⏳
└── Cold Prep ✓
```

- **Station board** (`station=<id>`/`unassigned`): show only that station's task
  for the item; only that task is actionable (its READY button). Other stations'
  work is not shown/actionable here.
- **all board:** group course → `OrderItem` → its tasks (chips per station). The
  item's quantity/label come from the `OrderItem`, so counts never inflate.

`total_items` stays tied to `OrderItem.quantity` (of items in scope), not a task
count, so a 3-station burger is still one burger.

---

## B2.2 — per-task READY + rollup

**Route:** `POST /kitchen/tasks/{task_id}/ready`, `require("kitchen.update")`,
redirect via the existing `_safe_next_board(next)` allowlist (→ `/kitchen` or
`/expo`).

**Logic:**
1. `task = db.get(PreparationTask, task_id)`; 404 if missing.
2. **Lock the parent Order** `SELECT … FROM "order" WHERE id=:oid FOR UPDATE`
   (no-op on SQLite) — serializes concurrent rollups on sibling tasks, same
   mechanism the fire path already uses.
3. **Idempotent:** if `task.kitchen_status == READY`, do not error — fall through
   to rollup + redirect (no-op).
4. Else set `task.kitchen_status = READY`, `task.ready_at = task.ready_at or now`.
   **Never** set a task to SERVED; **never** set `OrderItem`/`Order` to SERVED.
5. `rollup_item_kitchen_status(task.order_item)` — the approved B1 function,
   unchanged: item → READY only when every non-served task is READY, else
   PREPARING; never SERVED; no-tasks = no-op.
6. `_recompute_kitchen(order, now)` — re-derive Order/table state from item
   statuses exactly as the existing line-level flow does, so Order-level READY,
   ready-to-pay, and the **payment gate are byte-for-byte unchanged**.
7. `db.commit()`.

**Concurrency (two different tasks READY at once):** both transactions take
`FOR UPDATE` on the same Order row → serialized. First commits (task A READY;
rollup sees B still preparing → item PREPARING). Second re-reads committed state
(A READY), sets B READY; now all-ready → item READY, `_recompute_kitchen` →
Order READY. Final rollup is correct regardless of arrival order. On SQLite the
global write lock gives the same serialization; the second txn reads the first's
committed result. **Duplicate READY** on one task → second call sees READY, no-op,
rollup idempotent → identical final state. (No unique-constraint risk here; the
row lock, not an IntegrityError dance, is the guard.)

**READY ≠ SERVED / payment:** tasks only ever reach READY; SERVED remains owned by
the existing serving flow (`serve_items`/`_recompute_kitchen`). Nothing about
payment eligibility changes.

---

## B2 activation safety (the readiness invariant)

Rule: **if fired work that should have tasks is missing them, B2 must not silently
hide it.** Two layers, smallest-safe:

1. **The Order gate already protects `all`.** Because board qualification stays on
   `Order.kitchen_status` (not tasks), every fired order still appears as a card on
   `all`. A fired line lacking tasks is rendered via a **legacy fallback** (its
   `OrderItem.station_id`) so it is visible and attributed, never dropped. Only
   *station-tab scoping* leans on tasks.
2. **Explicit surfacing.** At KDS read, compute `fired_items_without_tasks(db)`
   (already exists). If `> 0`, show a visible banner ("N fired items without
   kitchen tasks — legacy fallback") rather than hiding them, and keep running the
   idempotent B1 backfill at startup (already wired) to drive it to 0.

This keeps the guarantee without a hard production halt. Whether to *also* hard-gate
startup (halt when `> 0`, like the index invariant) is left as an open decision
below.

---

## Backward compatibility (unchanged in B2)

Payment gate; serving semantics; READY ≠ SERVED; coursing; held courses create no
active tasks; Expo & Floor stay on the Stage A read path (B3); menu-routing UI;
modifier-routing UI (none yet); `OrderItem.station_id` legacy field **kept**;
PreparationTask creation/backfill rules. B2 changes only the KDS read + a new
per-task READY action.

---

## Files expected to change

**B2.1**
- `app/routers/sales.py` — `kitchen_display`: station EXISTS over `PreparationTask`;
  station counts aggregate over tasks; `selectinload` tasks→station; `tab_ids` from
  task counts. New task-based grouping to replace/extend `_ticket_courses` /
  `_line_in_station` (legacy kept for the fallback render).
- `web/templates/kitchen.html` — item→task sub-rows; badges from task counts.

**B2.2**
- `app/routers/sales.py` — new `POST /kitchen/tasks/{task_id}/ready`.
- `web/templates/kitchen.html` — per-task READY button on the station board.

**No model change, no migration** — `PreparationTask` already has `kitchen_status`
and `ready_at`.

---

## Lean test matrix

| Risk | Expected behavior | Test type |
|---|---|---|
| Station filtering reads tasks | station tab shows exactly orders with an active task at that station | route/integration |
| Unassigned | `station_id IS NULL` tasks appear under Unassigned and on `all`, never hidden | route/integration |
| Multi-station item | one `OrderItem`, tasks as sub-rows; not shown as duplicate items | route/render |
| Filter-before-limit | station/status filter applied before LIMIT 80; `board_total` matches | query/integration |
| Inactive station w/ live tasks | tab remains, work reachable | route/integration |
| Task READY rollup | task→READY; item READY only when all non-served tasks READY | unit (`rollup`) + route |
| READY ≠ SERVED / payment | task/item never SERVED via READY; payment gate unchanged | route/integration |
| Duplicate / concurrent READY | idempotent; two sibling tasks → correct final item/order status | concurrency (two-session) |
| B2 readiness invariant | fired item without tasks is surfaced (banner + legacy fallback), never hidden | route/integration |

(No re-run of Stage A/B1 suites unless B2 can regress them.)

---

## Unresolved architectural decisions

1. **Activation strictness:** visible-warn + legacy fallback (recommended) vs. a
   hard startup halt when `fired_items_without_tasks(db) > 0`. Need sign-off.
2. **`all`-board grouping unit:** group by `OrderItem` with per-station task chips
   (recommended) vs. a flat task list. Confirm the desired look.
3. **Course status chip source on `all`:** derive the course/line status from tasks
   or from the rolled-up `OrderItem.kitchen_status`? (Recommend the item rollup, so
   `all` and station boards agree.)
4. **Task SERVED on serve:** when an item is served via the existing flow, leave its
   tasks at READY (recommended for B2; they're already off the board via Order/item
   status) or cascade `served_at` onto tasks for later reporting — defer the
   cascade to B3/B5.
5. **Lock granularity:** Order row-lock (recommended, matches fire) vs. OrderItem.
