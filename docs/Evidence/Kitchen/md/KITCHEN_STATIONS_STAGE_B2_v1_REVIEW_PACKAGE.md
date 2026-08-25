# Kitchen Stations — Stage B2 Design Review Package v1 (for external review)

You are reviewing a **design only** (no code has been written). The project is a
restaurant POS (FastAPI + SQLAlchemy 2.0 + Jinja2; dev SQLite, prod Postgres).
Stage A and Stage B1 are already **audit-approved and closed**. This slice, **B2**,
moves the Kitchen Display (KDS) station reads and the per-line READY action from
the legacy `OrderItem.station_id` snapshot to the new `PreparationTask` rows
created at fire time in B1.

## What we want from your review

Judge the B2 design (Part C) against the **locked invariants** (Part A) and the
**actual current code** (Part B). Specifically:

1. Does the B2.1 read design preserve every Stage A/B1 guarantee, especially
   *filter-before-LIMIT*, honest counts, Unassigned visibility, inactive-station
   reachability, and coursing?
2. Is moving station scoping from `OrderItem.station_id` to `PreparationTask` (while
   keeping the **order-level board gate on `Order.kitchen_status`**) sound — any way
   a fired order or line could be silently hidden?
3. Is the B2.2 per-task READY + rollup + Order-row-lock correct under duplicate and
   concurrent READY? Any lost-update or stale-rollup hole?
4. Is the activation-safety approach (Order gate keeps `all` complete + legacy
   fallback render + visible banner from `fired_items_without_tasks`) sufficient, or
   is a hard startup halt warranted?
5. Weigh in on the 5 open architectural decisions at the end.

**Out of scope (do NOT redesign):** the PreparationTask model, fire-batch identity
`(order_item_id, fire_seq, COALESCE(station_id,-1))`, the unique index, backfill,
`rollup_item_kitchen_status`, or B1 concurrency. Those are approved. Also do NOT
propose B3 (Expo/Floor migration) or B4 (modifier-routing UI) work here.

---

# Part A — Locked invariants (must remain true)

- Snapshot-at-fire; fired routing is immutable (never re-read live MenuItem/Modifier
  routing for existing tasks).
- `OrderItem.station_id` is the **legacy Stage A snapshot** — kept, not removed in B2.
- `OrderItem.kitchen_status` stays compatible with the existing system; it is the
  source of truth for order/payment state.
- Status model `pending → preparing → ready → served`. **READY ≠ SERVED.**
  PreparationTasks must **never** own the `served` transition and never set
  `OrderItem`/`Order` to SERVED.
- Payment gate unchanged; coursing unchanged; held courses create no active tasks.
- KDS station work for fired items must come **only** from `PreparationTask.station_id`.
- Unassigned (`station_id IS NULL`) is explicit and never hidden.
- Inactive stations with live fired work stay reachable on the KDS.
- Filters (station + status) applied in SQL **before** `LIMIT 80`; counts computed
  over the same filtered population; oldest-fired-first ordering.
- Expo and Floor remain on the Stage A read path in B2 (B3 migrates them).
- B1 readiness invariant: `fired_items_without_tasks(db)` — if fired work expected to
  have tasks is missing tasks, B2 must not silently hide it.
- One-fire-per-line in current B1 (`fire_seq = 1`).

---

# Part B — Relevant current code (Stage A/B1, `app/routers/sales.py`)

### B.1  KDS read — `kitchen_display` (GET /kitchen), station scoping today reads `OrderItem.station_id`

```python
@router.get("/kitchen")
def kitchen_display(request, view="all", kstatus="all", station="all", mine=None,
                    db=Depends(get_db), staff=Depends(require("kitchen.view"))):
    KITCHEN_STATES = (KitchenStatus.PREPARING, KitchenStatus.READY)
    if kstatus != "all" and kstatus not in KITCHEN_STATES:
        kstatus = "all"

    # station filter -> "unassigned" | int station id | None (= all)
    if station == "unassigned":
        station_filter = "unassigned"
    elif station not in ("all", ""):
        try: station_filter = int(station)
        except ValueError: station_filter, station = None, "all"
    else:
        station_filter = None

    mine = (1 if _covers_tables(db, staff) else 0) if mine is None else (1 if mine else 0)

    # self-heal orders left SERVED while still holding preparing/ready items ...

    _on_board = [
        Order.kitchen_status.in_(KITCHEN_STATES),
        Order.status.not_in((OrderStatus.SERVED, OrderStatus.PAID,
                             OrderStatus.CLOSED, OrderStatus.CANCELLED)),
    ]

    def _view_mine(stmt):
        # channel view + "my tables" applied in SQL, BEFORE the LIMIT
        if view in ("dine_in", "delivery"):
            stmt = stmt.join(Channel, Channel.id == Order.channel_id).where(
                Channel.channel_type == "delivery" if view == "delivery"
                else Channel.channel_type != "delivery")
        if mine:
            stmt = stmt.where(Order.waiter_id == staff.id)
        return stmt

    # status tallies (view/mine applied, NOT status/station filter)
    status_counts = {KitchenStatus.PREPARING: 0, KitchenStatus.READY: 0}
    for st, n in db.execute(_view_mine(
        select(Order.kitchen_status, func.count()).where(*_on_board)
    ).group_by(Order.kitchen_status)):
        status_counts[st] = n

    # per-station badge counts — TODAY from OrderItem.station_id
    station_line_counts = {}
    for sid, n in db.execute(_view_mine(
        select(OrderItem.station_id, func.count())
        .select_from(OrderItem).join(Order, Order.id == OrderItem.order_id)
        .where(*_on_board, OrderItem.kitchen_status.in_(KITCHEN_STATES))
    ).group_by(OrderItem.station_id)):
        station_line_counts[sid if sid is not None else "unassigned"] = n

    # rendered set: view/mine + status + station filter, ALL in SQL, oldest first, capped
    tq = _view_mine(select(Order).where(*_on_board))
    if kstatus != "all":
        tq = tq.where(Order.kitchen_status == kstatus)
    if station_filter is not None:
        sub = select(OrderItem.id).where(
            OrderItem.order_id == Order.id,
            OrderItem.kitchen_status.in_(KITCHEN_STATES),
            OrderItem.station_id.is_(None) if station_filter == "unassigned"
            else OrderItem.station_id == station_filter,
        )
        tq = tq.where(sub.exists())
    board_total = db.execute(select(func.count()).select_from(tq.subquery())).scalar_one()
    orders = db.execute(
        tq.options(selectinload(Order.items).selectinload(OrderItem.station))
        .order_by(Order.sent_to_kitchen_at).limit(KITCHEN_LIMIT)   # KITCHEN_LIMIT = 80
    ).scalars().all()

    # ... build tickets via _ticket_courses(o, station_filter); skip an order that
    #     yields no courses under a station filter; urgency by elapsed minutes ...

    # station tabs: active stations PLUS any inactive station that still has fired
    # lines on the board (from station_line_counts keys)
    tab_ids = {sid for sid in station_line_counts if isinstance(sid, int)}
    stations = db.execute(select(Station).where(
        or_(Station.is_active.is_(True), Station.id.in_(tab_ids))
    ).order_by(Station.display_order, Station.name)).scalars().all()
    # station_tab_counts: {"all": sum, "unassigned": ..., <id>: ...}
```

### B.2  Ticket grouping today — reads the line's station snapshot

```python
def _line_in_station(item, station_filter):
    if station_filter is None: return True                 # all
    if station_filter == "unassigned": return item.station_id is None
    return item.station_id == station_filter

def _ticket_courses(order, station_filter=None):
    first_fire = order.sent_to_kitchen_at
    groups = {}
    for i in order.items:
        if i.kitchen_status in (KitchenStatus.PREPARING, KitchenStatus.READY):
            if not _line_in_station(i, station_filter):
                continue
            i.is_new = bool(first_fire and i.created_at and i.created_at > first_fire)
            groups.setdefault(i.course, []).append(i)
    out = []
    for course in sorted(groups):
        items = groups[course]
        statuses = {i.kitchen_status for i in items}
        status = KitchenStatus.READY if statuses == {KitchenStatus.READY} else KitchenStatus.PREPARING
        out.append({"course": course, "label": course_label(course),
                    "lines": items, "status": status, "multi": len(groups) > 1})
    return out
```

### B.3  Per-line status action today (order-scoped) — `POST /kitchen/{order_id}/status`

```python
@router.post("/kitchen/{order_id}/status")
def kitchen_status(order_id, status=Form(...), course=Form(0), item_id=Form(0),
                   next=Form(""), db=Depends(get_db),
                   staff=Depends(require("kitchen.update"))):
    order = db.get(Order, order_id) or _404
    if status not in (PENDING, PREPARING, READY): raise HTTPException(400, ...)
    targets = [i for i in order.items if i.id == item_id] if item_id \
              else [i for i in order.items if i.course == course] if course \
              else order.items
    for item in targets:
        if item.kitchen_status == KitchenStatus.SERVED:   # served items stay delivered
            continue
        item.kitchen_status = status
    now = datetime.now()
    if status == KitchenStatus.PREPARING:
        order.sent_to_kitchen_at = order.sent_to_kitchen_at or now
    _recompute_kitchen(order, now)      # re-derives Order/table state + ready-to-pay
    db.commit()
    return RedirectResponse(_safe_next_board(next), status_code=303)   # -> /kitchen or /expo
```

### B.4  Approved B1 primitives B2 reuses

```python
def rollup_item_kitchen_status(item):
    """item.kitchen_status derived from its PreparationTasks. READY only when every
    non-served task is READY; else PREPARING. NEVER sets SERVED. No tasks = no-op."""
    tasks = item.tasks
    if not tasks or item.kitchen_status == KitchenStatus.SERVED:
        return item.kitchen_status
    active = [t for t in tasks if t.kitchen_status != PreparationTaskStatus.SERVED]
    if active and all(t.kitchen_status == PreparationTaskStatus.READY for t in active):
        item.kitchen_status = KitchenStatus.READY
    elif active:
        item.kitchen_status = KitchenStatus.PREPARING
    return item.kitchen_status

def fired_items_without_tasks(db):
    """B2 readiness invariant: count fired OrderItems (preparing/ready) with NO task."""
    return db.execute(select(func.count()).select_from(OrderItem).where(
        OrderItem.kitchen_status.in_((KitchenStatus.PREPARING, KitchenStatus.READY)),
        ~OrderItem.tasks.any(),
    )).scalar_one()
```

`PreparationTask` (B1) fields relevant here: `order_item_id`, `order_id`,
`station_id` (nullable FK; NULL = Unassigned), `course`, `kitchen_status`
(PreparationTaskStatus PREPARING/READY/SERVED — nothing sets SERVED in B1),
`quantity`, `item_label`, `is_base`, `fire_seq`, `ready_at`, `served_at`;
relationships `order_item`, `station` (lazy select). `OrderItem.tasks` is a
cascade relationship. The fire path already row-locks the Order
(`SELECT … FOR UPDATE`, no-op on SQLite) and creates one task per distinct target
station.

---

# Part C — The B2 design under review

> The full design doc (`KITCHEN_STATIONS_STAGE_B2_v1_DESIGN.md`) follows verbatim.

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
- **Unassigned** → `pt.station_id IS NULL` (base task of an un-routed item; never
  hidden — always also on `all`).
- **all** → no station predicate.

Fired-work station membership comes **only** from `PreparationTask.station_id`
(the fire snapshot). Never re-read live `MenuItem`/`Modifier` routing.

**Filter-before-limit / ordering / total:** station EXISTS + `kstatus` predicate
applied in SQL *before* `order_by(Order.sent_to_kitchen_at)` (oldest first) and
`.limit(80)`, as today. `board_total` counts the same filtered set (subquery
`COUNT(*)`, no LIMIT).

**Counts (from tasks):** per-station badges move from `GROUP BY OrderItem.station_id`
to one aggregate over active tasks grouped by `pt.station_id` (COALESCE NULL →
"unassigned"), with `_view_mine` applied so a badge matches its tab's population.
`status_counts` stays driven by `Order.kitchen_status`.

**Inactive stations with live tasks:** `tab_ids` from the station keys in the task
counts ∪ active stations — an inactive station with active tasks keeps its tab.

**Coursing:** group by snapshotted `task.course`; held courses have no fired tasks.

**N+1:** eager-load `Order.items → OrderItem.tasks → PreparationTask.station`
(selectinload, one SELECT per level). Tab metadata from the aggregate, not the page.

**No mutation in B2.1.**

## Ticket presentation (multi-station item)

Render the item once, tasks as sub-rows:

```
Burger              (qty from OrderItem, shown once)
├── Grill    ⏳
├── Fryer    ⏳
└── Cold Prep ✓
```

- **Station board:** only that station's task, actionable.
- **all board:** course → `OrderItem` → task chips per station. Quantity/label from
  the `OrderItem`, so counts never inflate. `total_items` stays tied to
  `OrderItem.quantity`, not task count.

## B2.2 — per-task READY + rollup

**Route:** `POST /kitchen/tasks/{task_id}/ready`, `require("kitchen.update")`,
redirect via `_safe_next_board(next)`.

1. `task = db.get(PreparationTask, task_id)`; 404 if missing.
2. **Lock parent Order** `SELECT … FOR UPDATE` (no-op on SQLite) — serializes
   concurrent sibling rollups; same mechanism as fire.
3. **Idempotent:** already READY → no error, fall through to rollup + redirect.
4. Else `task.kitchen_status = READY`, `task.ready_at = ready_at or now`. Never set
   task/OrderItem/Order to SERVED.
5. `rollup_item_kitchen_status(task.order_item)` — approved B1 fn, unchanged.
6. `_recompute_kitchen(order, now)` — Order/table state + ready-to-pay derive exactly
   as the existing line flow; **payment gate unchanged**.
7. `db.commit()`.

**Concurrency:** both txns take `FOR UPDATE` on the Order row → serialized. First:
task A READY, rollup sees B preparing → item PREPARING. Second re-reads committed
state, B READY, all-ready → item READY → Order READY. Correct regardless of order.
SQLite's global write lock gives the same serialization. **Duplicate READY** → no-op,
rollup idempotent, identical state. The row lock (not an IntegrityError dance) is the
guard.

**READY ≠ SERVED / payment:** tasks only reach READY; SERVED stays owned by the
existing serving flow. Payment eligibility unchanged.

## B2 activation safety

Rule: fired work missing tasks must not be silently hidden. Two layers:

1. **Order gate protects `all`:** board qualification stays on `Order.kitchen_status`,
   so every fired order still shows as a card on `all`; a fired line lacking tasks is
   rendered via a **legacy fallback** (`OrderItem.station_id`), visible and attributed.
   Only station-tab scoping leans on tasks.
2. **Explicit surfacing:** compute `fired_items_without_tasks(db)`; if `> 0`, show a
   visible banner (not hide), and keep the idempotent B1 backfill running at startup.

Whether to *also* hard-gate startup (halt when `> 0`) is an open decision below.

## Backward compatibility (unchanged in B2)

Payment gate; serving; READY ≠ SERVED; coursing; held courses; Expo & Floor on Stage A
reads (B3); menu-routing UI; modifier-routing UI (none yet); `OrderItem.station_id`
kept; PreparationTask creation/backfill rules.

## Files expected to change

**B2.1:** `app/routers/sales.py` (`kitchen_display` station EXISTS/counts over tasks,
selectinload tasks→station, tab_ids from task counts, task-based grouping replacing/
extending `_ticket_courses`/`_line_in_station` with legacy kept for fallback);
`web/templates/kitchen.html` (item→task sub-rows, badges from task counts).
**B2.2:** `app/routers/sales.py` (new `POST /kitchen/tasks/{task_id}/ready`);
`web/templates/kitchen.html` (per-task READY button). **No model change, no migration.**

## Lean test matrix

| Risk | Expected behavior | Test type |
|---|---|---|
| Station filtering reads tasks | station tab shows exactly orders with an active task there | route/integration |
| Unassigned | `station_id IS NULL` tasks under Unassigned and on `all`, never hidden | route/integration |
| Multi-station item | one `OrderItem`, tasks as sub-rows; not duplicate items | route/render |
| Filter-before-limit | filter before LIMIT 80; `board_total` matches | query/integration |
| Inactive station w/ live tasks | tab remains, work reachable | route/integration |
| Task READY rollup | task→READY; item READY only when all non-served tasks READY | unit + route |
| READY ≠ SERVED / payment | never SERVED via READY; payment gate unchanged | route/integration |
| Duplicate / concurrent READY | idempotent; two sibling tasks → correct final status | concurrency (two-session) |
| B2 readiness invariant | fired item without tasks surfaced (banner + legacy fallback), never hidden | route/integration |

## Unresolved architectural decisions

1. **Activation strictness:** visible-warn + legacy fallback (recommended) vs. hard
   startup halt when `fired_items_without_tasks(db) > 0`.
2. **`all`-board grouping unit:** group by `OrderItem` with per-station task chips
   (recommended) vs. flat task list.
3. **Course status chip source on `all`:** from tasks or from rolled-up
   `OrderItem.kitchen_status` (recommend the item rollup, so boards agree).
4. **Task SERVED on serve:** leave tasks at READY (recommended) vs. cascade
   `served_at` onto tasks — defer cascade to B3/B5.
5. **Lock granularity:** Order row-lock (recommended, matches fire) vs. OrderItem.

---

# Part D — Answer these

1. Any invariant in Part A that the Part C design does not actually preserve? Cite it.
2. B2.1: with the board gate on `Order.kitchen_status` but station scoping/rendering
   on tasks — construct any case where a fired order/line is silently hidden, or say
   none exists.
3. B2.2: is the Order-row-lock + reuse of `rollup_item_kitchen_status` correct for
   duplicate and concurrent READY? Give a failing interleaving if one exists.
4. Activation safety: is legacy-fallback-render + visible banner enough, or is a hard
   startup halt on `fired_items_without_tasks(db) > 0` required? Recommend one.
5. Pick a side on each of the 5 open architectural decisions and say why.
Keep the response focused on implementation decisions; do not restate the design.
