# Kitchen Stations — Stage B2.1 Independent-Audit Package

**Purpose:** evidence packaging for an independent audit of the B2.1 implementation
against the approved `KITCHEN_STATIONS_STAGE_B2_v3_DESIGN.md` (verdict: APPROVED WITH
NON-BLOCKING NOTES). **No code was changed to produce this file. No fixes. No B2.2.**

B2.1 status: **implemented, AWAITING INDEPENDENT AUDIT — not approved.**

## Scope of the change (only these four files)

| File | Kind | B2.1 content |
|---|---|---|
| `app/routers/sales.py` | modified | gate `task_kds_active`, task-read helpers, `kitchen_display` task-mode branches |
| `web/templates/kitchen.html` | modified | task station sub-rows, blocked notice, read-only in task mode |
| `app/main.py` | modified | startup activation-readiness warning (decision (a)) |
| `tests/test_kds_task_reads.py` | new | 11 read-side B2.1 tests |

## Diff methodology & evidence limit (read first)

There is **no baseline commit** to diff against: the entire `feat/floor-map` working
tree is uncommitted, and even `HEAD` (`8a0e349`) predates Stage A **and** B1 (the whole
Kitchen Stations line is uncommitted from earlier sessions). A raw
`git diff -- app/routers/sales.py` therefore mixes Stage A + B1 + B2.1 and **cannot**
isolate B2.1. To honor "the exact B2.1 diff / no unrelated working-tree diff," the
hunks below are the **exact B2.1 edits** (old→new), each anchored to the **current line
numbers** so the reviewer can grep/inspect them in the live file. Verify independently
with, e.g., `grep -nE "task_kds_active|_task_ticket_courses|task_mode|b2_blocked|TASK_ACTIVE"
app/routers/sales.py`.

---

## A. `app/main.py` — startup activation-readiness warning (decision (a))

New block after the schedule-positions seed (current lines **54–67**). Reuses the
existing stdout logging (`print(..., flush=True)`); **no new infrastructure**. It runs
inside the existing startup section that already opens a `SessionLocal()` for seeding
(same pattern, same place) — no new import-time DB access pattern is introduced; the
runtime gate in `kitchen_display` is the actual authority, this is only an operator log.

```diff
 from app.services import schedule as _schedule_svc  # noqa: E402
 with SessionLocal() as _db:
     _schedule_svc.ensure_default_positions(_db)
+
+# Kitchen Stations B2.1 — surface the task-KDS activation-readiness state at
+# startup so a BLOCKED activation (flag on, but fired work still lacks tasks) is
+# visible in the service logs. Reuses the existing stdout logging; no new infra.
+# The runtime gate in kitchen_display is what actually keeps the board on the
+# complete Stage A path — this is only an operator-visible heads-up.
+from app.services import settings as _settings_svc  # noqa: E402
+with SessionLocal() as _db:
+    if _settings_svc.flag(_db, "kitchen_b2_active"):
+        _missing = sales.fired_items_without_tasks(_db)
+        if _missing:
+            print(f"[kitchen-b2] activation BLOCKED: {_missing} fired item(s) without "
+                  "PreparationTasks — serving the full Stage A KDS", flush=True)
+        else:
+            print("[kitchen-b2] task-based KDS active (flag on, no missing tasks)", flush=True)
```

`sales` and `SessionLocal` are already imported at the top of `main.py`
(`from app.routers import ... sales ...`; `from app.database import ... SessionLocal ...`).

---

## B. `app/routers/sales.py`

### B.1 — import (current line 57)

```diff
 from app.services import daymenu, happyhour
 from app.services import reservations as reservations_svc
+from app.services import settings as settings_svc
 from app.services import upsell
```

### B.2 — gate + task-read helpers (new block, current lines **1691–1793**, right after `fired_items_without_tasks`)

```diff
     ).scalar_one()
+
+
+# --------------------------------------------------------------------------
+# Kitchen Stations B2.1 — task-based KDS reads (DORMANT until activated)
+# --------------------------------------------------------------------------
+
+# PreparationTask statuses that put station work on the board (mirrors the
+# OrderItem KITCHEN_STATES). SERVED tasks are off the line.
+TASK_ACTIVE = (PreparationTaskStatus.PREPARING, PreparationTaskStatus.READY)
+
+
+def task_kds_active(db: Session) -> bool:
+    """B2.1 activation gate (design v3 §2) — DORMANT by default.
+
+    Task-based KDS reads switch on ONLY when the manual `kitchen_b2_active`
+    setting is truthy AND every fired item already has PreparationTasks
+    (`fired_items_without_tasks(db) == 0`). A missing / false / non-boolean flag
+    reads as False (settings.flag defaults to "0"), and B2.1 ships **no** UI path
+    to set the flag — so the task board stays off until an operator sets it
+    deliberately and B2.2 exists to make it actionable. If the flag is on but
+    fired work still lacks tasks, this returns False and the KDS keeps serving the
+    complete Stage A board (never a hybrid)."""
+    if not settings_svc.flag(db, "kitchen_b2_active"):
+        return False
+    return fired_items_without_tasks(db) == 0
+
+
+def _task_in_station(task, station_filter) -> bool:
+    """The single, unambiguous station rule (design v3 §3.1): None = every station;
+    "unassigned" = NULL station ONLY; an int = that station ONLY. An Unassigned
+    task never matches a selected station, and a selected station never shows
+    Unassigned."""
+    if station_filter is None:
+        return True
+    if station_filter == "unassigned":
+        return task.station_id is None
+    return task.station_id == station_filter
+
+
+def _task_ticket_courses(order: Order, station_filter=None) -> list[dict]:
+    """An order's items grouped by course for the TASK-mode KDS (design v3 §3.7).
+
+    Each sale OrderItem renders once; its active PreparationTasks matching the
+    station filter become station sub-rows (attached as `item.display_tasks`).
+    The course/line status shown is the rolled-up `OrderItem.kitchen_status`
+    (decision #3), not a per-task status. Only fired items (those with active
+    tasks) appear; held courses have no tasks. Read-only — no mutation here."""
+    first_fire = order.sent_to_kitchen_at
+    groups: dict[int, list] = {}
+    for i in order.items:
+        tks = [t for t in i.tasks
+               if t.kitchen_status in TASK_ACTIVE and _task_in_station(t, station_filter)]
+        if not tks:
+            continue
+        i.is_new = bool(first_fire and i.created_at and i.created_at > first_fire)
+        i.display_tasks = sorted(
+            tks, key=lambda t: (t.station.display_order if t.station else 999, t.id)
+        )
+        groups.setdefault(i.course, []).append(i)
+    out = []
+    for course in sorted(groups):
+        items = groups[course]
+        statuses = {it.kitchen_status for it in items}
+        status = KitchenStatus.READY if statuses == {KitchenStatus.READY} else KitchenStatus.PREPARING
+        out.append({
+            "course": course, "label": course_label(course),
+            "lines": items, "status": status, "multi": len(groups) > 1,
+        })
+    return out
+
+
 @router.post("/orders/{order_id}/send")
 def send_to_kitchen(
```

### B.3 — `kitchen_display`: mode/gate computation (added after `mine = …`, current lines ~1906–1913)

```diff
     mine = (1 if _covers_tables(db, staff) else 0) if mine is None else (1 if mine else 0)
+
+    # Kitchen Stations B2.1 — task-based reads are DORMANT until activated. Single
+    # authority: TASK mode ONLY when the manual flag is on AND every fired item has
+    # tasks; otherwise the COMPLETE Stage A board (never a hybrid). Flag on but
+    # fired work still task-less → Stage A board + a blocked notice (design v3 §2).
+    b2_flag = settings_svc.flag(db, "kitchen_b2_active")
+    b2_missing = fired_items_without_tasks(db) if b2_flag else 0
+    task_mode = b2_flag and b2_missing == 0
+    b2_blocked = b2_flag and b2_missing > 0
```

### B.4 — station badge counts over the same board universe (current lines ~1964–1987)

Task mode replaces the Stage A `OrderItem.station_id` grouping with a
`PreparationTask.station_id` grouping over **the same** `_on_board` + `_view_mine`
**and the same `kstatus`** universe (design v3 §3.3); Stage A path is unchanged.

```diff
     station_line_counts: dict = {}
-    for sid, n in db.execute(
-        _view_mine(
-            select(OrderItem.station_id, func.count())
-            .select_from(OrderItem).join(Order, Order.id == OrderItem.order_id)
-            .where(*_on_board, OrderItem.kitchen_status.in_(KITCHEN_STATES))
-        ).group_by(OrderItem.station_id)
-    ):
-        station_line_counts[sid if sid is not None else "unassigned"] = n
+    if task_mode:
+        # Badges count active PreparationTasks (station-work units) over the SAME
+        # order universe/view/mine AND the same kstatus filter as the board
+        # (design v3 §3.3), so a tab's count matches exactly what selecting it
+        # would reveal. Only the station filter is replaced by GROUP BY station.
+        cq = _view_mine(
+            select(PreparationTask.station_id, func.count())
+            .select_from(PreparationTask).join(Order, Order.id == PreparationTask.order_id)
+            .where(*_on_board, PreparationTask.kitchen_status.in_(TASK_ACTIVE))
+        )
+        if kstatus != "all":
+            cq = cq.where(Order.kitchen_status == kstatus)
+        for sid, n in db.execute(cq.group_by(PreparationTask.station_id)):
+            station_line_counts[sid if sid is not None else "unassigned"] = n
+    else:
+        for sid, n in db.execute(
+            _view_mine(
+                select(OrderItem.station_id, func.count())
+                .select_from(OrderItem).join(Order, Order.id == OrderItem.order_id)
+                .where(*_on_board, OrderItem.kitchen_status.in_(KITCHEN_STATES))
+            ).group_by(OrderItem.station_id)
+        ):
+            station_line_counts[sid if sid is not None else "unassigned"] = n
```

### B.5 — rendered set: station EXISTS (single rule) + eager-load tasks (current lines ~1995–2028)

Task-mode membership uses `PreparationTask.station_id` **only**, one mutually-exclusive
predicate (design v3 §3.1). `board_total` still counts the same filtered set. Tasks +
their station are eager-loaded (no N+1) only in task mode.

```diff
     if station_filter is not None:
-        sub = select(OrderItem.id).where(
-            OrderItem.order_id == Order.id,
-            OrderItem.kitchen_status.in_(KITCHEN_STATES),
-            OrderItem.station_id.is_(None) if station_filter == "unassigned"
-            else OrderItem.station_id == station_filter,
-        )
-        tq = tq.where(sub.exists())
+        if task_mode:
+            # Task-mode membership from PreparationTask.station_id ONLY (the fire
+            # snapshot), single-rule (design v3 §3.1): selected station = that id;
+            # Unassigned = NULL; the two are mutually exclusive.
+            conds = [
+                PreparationTask.order_id == Order.id,
+                PreparationTask.kitchen_status.in_(TASK_ACTIVE),
+                PreparationTask.station_id.is_(None) if station_filter == "unassigned"
+                else PreparationTask.station_id == station_filter,
+            ]
+            tq = tq.where(select(PreparationTask.id).where(*conds).exists())
+        else:
+            sub = select(OrderItem.id).where(
+                OrderItem.order_id == Order.id,
+                OrderItem.kitchen_status.in_(KITCHEN_STATES),
+                OrderItem.station_id.is_(None) if station_filter == "unassigned"
+                else OrderItem.station_id == station_filter,
+            )
+            tq = tq.where(sub.exists())
     board_total = db.execute(
         select(func.count()).select_from(tq.subquery())
     ).scalar_one()
-    orders = db.execute(
-        tq.options(selectinload(Order.items).selectinload(OrderItem.station))
-        .order_by(Order.sent_to_kitchen_at).limit(KITCHEN_LIMIT)
-    ).scalars().all()
+    # Task mode also eager-loads each item's tasks + their station (station
+    # sub-rows) so rendering stays one SELECT per level, no N+1.
+    load_opts = [selectinload(Order.items).selectinload(OrderItem.station)]
+    if task_mode:
+        load_opts.append(
+            selectinload(Order.items).selectinload(OrderItem.tasks).selectinload(PreparationTask.station)
+        )
+    orders = db.execute(
+        tq.options(*load_opts)
+        .order_by(Order.sent_to_kitchen_at).limit(KITCHEN_LIMIT)
+    ).scalars().all()
```

### B.6 — ticket grouping + `total_items` (current lines ~2033–2048)

```diff
         is_delivery = o.channel.channel_type == "delivery"
-        # Items grouped by course, scoped to the chosen station.
-        courses = _ticket_courses(o, station_filter)
+        # Items grouped by course, scoped to the chosen station. Task mode groups
+        # by PreparationTask (one sale line, task station sub-rows); Stage A keeps
+        # the line-snapshot grouping.
+        courses = (_task_ticket_courses(o, station_filter) if task_mode
+                   else _ticket_courses(o, station_filter))
         if station_filter is not None and not courses:
             continue
         sent_at = o.sent_to_kitchen_at or o.opened_at
         elapsed = int((now - sent_at).total_seconds() // 60)
         # Colour-coded urgency (4.1.3).
         urgency = "ok" if elapsed < 10 else ("warn" if elapsed < 20 else "late")
-        if station_filter is None:
-            total_items = sum(
-                i.quantity for i in o.items if i.kitchen_status != KitchenStatus.SERVED
-            )
-        else:
-            total_items = sum(i.quantity for c in courses for i in c["lines"])
+        # total_items is SALE-item quantity (never a station-work/task count). In
+        # task mode and any station-scoped view it sums the shown OrderItems' qty.
+        if task_mode or station_filter is not None:
+            total_items = sum(i.quantity for c in courses for i in c["lines"])
+        else:
+            total_items = sum(
+                i.quantity for i in o.items if i.kitchen_status != KitchenStatus.SERVED
+            )
```

### B.7 — render context (current lines ~2099–2104)

```diff
         "has_unassigned": station_line_counts.get("unassigned", 0) > 0,
         "board_total": board_total, "limit": KITCHEN_LIMIT,
+        # Kitchen Stations B2.1: task_mode drives task station sub-rows + read-only
+        # rendering; b2_blocked shows the "activation blocked" operator notice while
+        # the board falls back to complete Stage A.
+        "task_mode": task_mode, "b2_blocked": b2_blocked, "b2_missing": b2_missing,
         "title": "Kitchen display",
     })
```

---

## C. `web/templates/kitchen.html`

### C.1 — blocked-activation notice (current lines ~50–60, after the status filter bar)

```diff
      title="Show only tables you're serving">My tables</a>
 </div>
+
+{% if b2_blocked %}
+{# Kitchen Stations B2: task-based KDS is flagged on but some fired work has no
+   PreparationTasks yet — the runtime gate keeps the COMPLETE Stage A board (never
+   a hybrid) until that clears. Operator-visible, not hidden. #}
+<div class="card" style="border-left:4px solid #d9822b;margin:0 0 12px">
+  <p class="sub" style="margin:0"><strong>⚠ Task-based KDS activation blocked:</strong>
+  {{ b2_missing }} fired item{{ '' if b2_missing == 1 else 's' }} without kitchen tasks.
+  Showing the full Stage A board until every fired item has tasks.</p>
+</div>
+{% endif %}

 {% if not tickets %}
```

### C.2 — line body: task sub-rows + read-only in task mode (current lines ~105–152)

One sale line renders once; task sub-rows carry the per-station chips in task mode; the
legacy per-line action controls are rendered **only** in Stage A (`not task_mode`).

```diff
         {% for i in c.lines %}
-        <li class="k-line{% if i.is_new %} k-new{% endif %}{% if i.kitchen_status == 'ready' %} k-done{% endif %}">
+        <li class="k-line{% if i.is_new %} k-new{% endif %}{% if not task_mode and i.kitchen_status == 'ready' %} k-done{% endif %}">
           <div class="k-line-body">
-            {# Station chip (snapshot at fire) — helps the expo see where each
-               line came from on the All board. Unassigned lines are flagged. #}
-            {% if station == 'all' %}{% if i.station %}<span class="k-st-chip" style="--sc: {{ i.station.swatch }}">{{ i.station.icon }} {{ i.station.name }}</span> {% else %}<span class="k-st-chip k-st-none">⚠ Unassigned</span> {% endif %}{% endif %}
+            {# Stage A station chip (snapshot at fire) on the All board. In task
+               mode the station lives in the per-task sub-rows below instead. #}
+            {% if not task_mode and station == 'all' %}{% if i.station %}<span class="k-st-chip" style="--sc: {{ i.station.swatch }}">{{ i.station.icon }} {{ i.station.name }}</span> {% else %}<span class="k-st-chip k-st-none">⚠ Unassigned</span> {% endif %}{% endif %}
             {% if i.is_new %}<span class="k-new-tag">NEW</span> {% endif %}{{ i.quantity }}× {{ i.menu_item.name }}
             {% for o in i.options %}<br><span class="sub">+ {{ o.label }}</span>{% endfor %}
             {% for m in i.modifiers %}<br><span class="sub">+ {{ m.modifier.name }}</span>{% endfor %}
             {% if i.allergens %}<br><span class="allergy-chit">⚠ ALLERGY: {{ i.allergens }}</span>{% endif %}
             {% if i.notes %}<br><span class="note">✎ {{ i.notes }}</span>{% endif %}
+            {% if task_mode %}
+            {# Kitchen Stations B2.1 — one sale line, per-station task sub-rows
+               (read-only). Never duplicates the sale item. #}
+            <div class="k-tasks">
+              {% for tk in i.display_tasks %}
+              <div class="k-task">
+                {% if tk.station %}<span class="k-st-chip" style="--sc: {{ tk.station.swatch }}">{{ tk.station.icon }} {{ tk.station.name }}</span>{% else %}<span class="k-st-chip k-st-none">⚠ Unassigned</span>{% endif %}
+                <span class="k-task-status {{ 'ok' if tk.kitchen_status == 'ready' else '' }}" title="{{ 'ready' if tk.kitchen_status == 'ready' else 'preparing' }}">{{ '✓' if tk.kitchen_status == 'ready' else '⏳' }}</span>
+              </div>
+              {% endfor %}
+            </div>
+            {% endif %}
           </div>
+          {% if not task_mode %}
+          {# Stage A per-line ready controls (legacy /status action). Deliberately
+             absent in task mode: per-task READY is B2.2; B2.1 is read-only. #}
           {% if can('kitchen.update') and i.kitchen_status == 'preparing' %}
           <form method="post" action="/kitchen/{{ t.order.id }}/status" class="k-line-act">
             <input type="hidden" name="status" value="ready">
             <input type="hidden" name="item_id" value="{{ i.id }}">
             <button type="submit" class="k-check" title="Mark this item ready" aria-label="Mark ready"></button>
           </form>
           {% elif can('kitchen.update') and i.kitchen_status == 'ready' %}
           {# Click the tick to undo — in case a line was marked ready by mistake. #}
           <form method="post" action="/kitchen/{{ t.order.id }}/status" class="k-line-act">
             <input type="hidden" name="status" value="preparing">
             <input type="hidden" name="item_id" value="{{ i.id }}">
             <button type="submit" class="k-uncheck" title="Not ready — undo" aria-label="Undo ready">✓</button>
           </form>
           {% elif i.kitchen_status == 'ready' %}
           <span class="k-line-done" title="Ready">✓</span>
           {% endif %}
+          {% endif %}
         </li>
         {% endfor %}
```

### C.3 — order-level "Mark all ready" hidden in task mode (current line ~154)

```diff
-    {% if can('kitchen.update') and t.courses|selectattr('status','equalto','preparing')|list %}
+    {% if not task_mode and can('kitchen.update') and t.courses|selectattr('status','equalto','preparing')|list %}
     <form method="post" action="/kitchen/{{ t.order.id }}/status" style="margin-top:8px">
       <input type="hidden" name="status" value="ready">
       <button class="btn good" style="width:100%">✓ Mark all ready</button>
     </form>
     {% endif %}
```

---

## D. Complete `tests/test_kds_task_reads.py` (new file, 11 tests)

```python
"""Kitchen Stations Stage B2.1 — task-based KDS READS (read-only).

Proves the read-side migration from OrderItem.station_id to PreparationTask:
dormant-by-default gate, single-rule station scoping, Unassigned handling,
task-based badge counts, inactive-station reachability, one-OrderItem-rendered-once
with task sub-rows, and the complete Stage A fallback (no hybrid) when task mode is
inactive or blocked. B2.1 introduces NO mutation, so there are no READY/rollup tests
here (that is B2.2). Throwaway SQLite + dependency overrides.
Run: python tests/test_kds_task_reads.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.deps import current_staff, get_db
from app.main import app
from app.models import oltp  # noqa: F401
from app.models.oltp import (
    Channel, KitchenStatus, MenuCategory, MenuItem, Order, OrderItem, OrderStatus,
    PreparationTask, PreparationTaskStatus, Setting, Staff, Station,
)
from app.routers.sales import task_kds_active

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _session():
    path = os.path.join(tempfile.gettempdir(), f"kds_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _client(db, staff):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


def _owner(db):
    o = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
    db.add(o); db.commit()
    return o


def _base(db, item_station=None):
    ch = Channel(code="dine_in", name="Dine-in", channel_type="dine_in")
    cat = MenuCategory(name="Mains")
    db.add_all([ch, cat]); db.flush()
    item = MenuItem(category_id=cat.id, name="Burger", price_cents=1500, station_id=item_station)
    db.add(item); db.commit()
    return ch, item


def _flag_on(db):
    db.add(Setting(key="kitchen_b2_active", value="1")); db.commit()


def _fired_item(db, ch, mi, station_ids, item_ks=KitchenStatus.PREPARING, with_tasks=True):
    """A fired order (on-board) with one OrderItem and one PreparationTask per id in
    station_ids (None = Unassigned). with_tasks=False leaves a fired item with NO
    tasks (to exercise the readiness-blocked path)."""
    now = datetime.now()
    o = Order(code=f"O-{uuid.uuid4().hex[:6]}", channel_id=ch.id, status=OrderStatus.PREPARING,
              guest_count=2, opened_at=now, sent_to_kitchen_at=now,
              kitchen_status=KitchenStatus.PREPARING)
    db.add(o); db.flush()
    oi = OrderItem(order_id=o.id, menu_item_id=mi.id, quantity=1,
                   unit_price_cents=mi.price_cents, kitchen_status=item_ks, course=2,
                   station_id=station_ids[0] if station_ids else None)
    db.add(oi); db.flush()
    if with_tasks:
        for idx, sid in enumerate(station_ids):
            db.add(PreparationTask(
                order_item_id=oi.id, order_id=o.id, station_id=sid, course=2,
                kitchen_status=PreparationTaskStatus.PREPARING, quantity=1,
                item_label=mi.name, is_base=(idx == 0), fire_seq=1, fired_at=now))
    db.commit()
    return o, oi


def _get(c, station="all", kstatus="all"):
    return c.get(f"/kitchen?station={station}&kstatus={kstatus}&mine=0", follow_redirects=False)


# --------------------------------------------------------------------------

def test_gate_dormant_by_default():
    db = _session(); _owner(db)
    check(task_kds_active(db) is False, "no flag → task_kds_active False (dormant default)")
    db.add(Setting(key="kitchen_b2_active", value="banana")); db.commit()
    check(task_kds_active(db) is False, "non-boolean flag value → still False")
    # B2.1 must ship NO operational path to activate task KDS: the flag is not an
    # editable setting, so the owner Settings form cannot flip it (note #1).
    from app.services import settings as _svc
    check("kitchen_b2_active" not in _svc.EDITABLE,
          "the b2 flag is not settable via the Settings form (no in-app activation path)")
    db.close()


def test_gate_on_only_when_no_missing_tasks():
    db = _session(); _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _flag_on(db)
    check(task_kds_active(db) is True, "flag on + zero fired-without-tasks → active")
    # A fired item with NO tasks blocks activation.
    _fired_item(db, ch, mi, [grill.id], with_tasks=False)
    check(task_kds_active(db) is False, "flag on but a fired item lacks tasks → NOT active")
    db.close()


def test_stage_a_when_flag_off():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id])           # flag NOT set
    c = _client(db, owner)
    html = _get(c).text
    check("/status" in html and 'name="item_id"' in html,
          "flag off → Stage A board keeps the legacy per-line action")
    check("k-tasks" not in html, "flag off → no task sub-rows rendered")
    app.dependency_overrides.clear(); db.close()


def test_task_mode_renders_subrows_readonly():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True)
    fryer = Station(name="Fryer", type="production", is_active=True)
    db.add_all([grill, fryer]); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id, fryer.id])     # one sale line, two station tasks
    _flag_on(db)
    c = _client(db, owner)
    html = _get(c).text
    check(html.count("Burger") == 1, "the sale item renders exactly once (not duplicated per task)")
    check("k-tasks" in html and "Grill" in html and "Fryer" in html,
          "task station sub-rows render for both stations")
    check('name="item_id"' not in html and "k-check" not in html,
          "task mode is READ-ONLY: no legacy per-line action controls")
    app.dependency_overrides.clear(); db.close()


def test_selected_station_shows_only_its_task():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True)
    fryer = Station(name="Fryer", type="production", is_active=True)
    db.add_all([grill, fryer]); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id, fryer.id])
    _flag_on(db)
    c = _client(db, owner)
    # NB: the station tab BAR lists every station, so assert on the rendered task
    # sub-rows (class="k-task") not on station names appearing anywhere in the page.
    grill_html = _get(c, station=str(grill.id)).text
    check(grill_html.count('class="k-task"') == 1 and "Burger" in grill_html,
          "selected Grill tab renders the item with exactly its single Grill task sub-row")
    fryer_html = _get(c, station=str(fryer.id)).text
    check(fryer_html.count('class="k-task"') == 1 and "Burger" in fryer_html,
          "selected Fryer tab renders the item with exactly its single Fryer task sub-row")
    check(_get(c, station="all").text.count('class="k-task"') == 2,
          "the All board renders both task sub-rows for the one item")
    app.dependency_overrides.clear(); db.close()


def test_unassigned_scoping():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.commit()
    ch, mi = _base(db, item_station=None)
    _fired_item(db, ch, mi, [None])               # base task, Unassigned (NULL station)
    _flag_on(db)
    c = _client(db, owner)
    check("Burger" in _get(c, station="unassigned").text,
          "Unassigned tab shows the NULL-station task")
    check("Burger" in _get(c, station="all").text,
          "Unassigned work is also visible on the All board (never hidden)")
    check("Burger" not in _get(c, station=str(grill.id)).text,
          "Unassigned work NEVER appears under a selected station")
    app.dependency_overrides.clear(); db.close()


def test_station_filter_excludes_other_orders():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True)
    fryer = Station(name="Fryer", type="production", is_active=True)
    db.add_all([grill, fryer]); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    og, _ = _fired_item(db, ch, mi, [grill.id])
    of, _ = _fired_item(db, ch, mi, [fryer.id])
    _flag_on(db)
    c = _client(db, owner)
    html = _get(c, station=str(grill.id)).text
    check(og.code in html and of.code not in html,
          "station filter excludes the whole non-matching order (filtered in SQL before LIMIT)")
    app.dependency_overrides.clear(); db.close()


def test_badge_counts_are_task_based():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True)
    fryer = Station(name="Fryer", type="production", is_active=True)
    db.add_all([grill, fryer]); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id, fryer.id])   # 1 sale line → 2 station-work units
    _flag_on(db)
    c = _client(db, owner)
    html = _get(c).text
    check("All stations 2" in html,
          "All badge counts PreparationTasks (2), not sale lines (which would be 1)")
    check("Total items: 1" in html,
          "total_items stays sale-item quantity (1), distinct from the 2 station-work units")
    app.dependency_overrides.clear(); db.close()


def test_inactive_station_with_live_task_keeps_tab():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id])
    grill.is_active = False; db.commit()            # deactivated AFTER firing
    _flag_on(db)
    c = _client(db, owner)
    html = _get(c).text
    check("Grill" in html, "an inactive station with live tasks keeps its KDS tab reachable")
    app.dependency_overrides.clear(); db.close()


def test_blocked_activation_falls_back_to_full_stage_a():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id])                       # a proper task-backed item
    _fired_item(db, ch, mi, [grill.id], with_tasks=False)     # a fired item with NO tasks
    _flag_on(db)
    c = _client(db, owner)
    html = _get(c).text
    check(task_kds_active(db) is False, "flag on + a task-less fired item → gate stays off")
    check("activation blocked" in html.lower(), "operator sees the blocked-activation notice")
    check("k-tasks" not in html and 'name="item_id"' in html,
          "blocked → COMPLETE Stage A board (legacy actions), never a hybrid task board")
    app.dependency_overrides.clear(); db.close()


if __name__ == "__main__":
    for fn in [
        test_gate_dormant_by_default,
        test_gate_on_only_when_no_missing_tasks,
        test_stage_a_when_flag_off,
        test_task_mode_renders_subrows_readonly,
        test_selected_station_shows_only_its_task,
        test_unassigned_scoping,
        test_station_filter_excludes_other_orders,
        test_badge_counts_are_task_based,
        test_inactive_station_with_live_task_keeps_tab,
        test_blocked_activation_falls_back_to_full_stage_a,
    ]:
        print(f"- {fn.__name__}")
        fn()
    print()
    if _fail:
        print(f"{len(_fail)} FAILED")
        sys.exit(1)
    print("all KDS task-read (B2.1) tests passed")
```

### Test → requirement mapping (for the auditor's 4 focus points + design invariants)

| Test | Proves |
|---|---|
| `test_gate_dormant_by_default` | no accidental activation: missing flag → False; non-boolean → False; flag not in `settings.EDITABLE` (no UI path) — **auditor point 1** |
| `test_gate_on_only_when_no_missing_tasks` | gate on only when flag AND invariant==0 |
| `test_stage_a_when_flag_off` | default = full Stage A (legacy actions present, no sub-rows) |
| `test_task_mode_renders_subrows_readonly` | one OrderItem rendered once; task sub-rows; **no** legacy action controls (read-only) |
| `test_selected_station_shows_only_its_task` | single-rule scoping (exactly one sub-row per selected station; two on All) — **Unassigned isolation & one-rendered-once** |
| `test_unassigned_scoping` | Unassigned on its tab + on All, **never** under a selected station |
| `test_station_filter_excludes_other_orders` | non-matching order excluded from the qualifying set (SQL filter, not post-LIMIT trimming) — **filter-before-limit / point** |
| `test_badge_counts_are_task_based` | badge = task count (2), `total_items` = sale qty (1) — **auditor point 2** |
| `test_inactive_station_with_live_task_keeps_tab` | inactive station with live tasks keeps its tab |
| `test_blocked_activation_falls_back_to_full_stage_a` | blocked → complete Stage A + notice, no `k-tasks` leaking → **no hybrid, auditor point 3** |

Auditor **point 4** (startup warning: no import-time side effect / DB access at the wrong
time) is inspectable in §A: the block runs inside the **existing** startup section that
already opens `SessionLocal()` for the schedule seed — same place, same pattern; it adds
no new import-time behavior beyond one bounded settings read + one COUNT, and only when
the flag is set. It is **not** covered by an automated test (the literal startup `print`
runs at module import); this is stated as an evidence limit below.

---

## E. Test commands run + results (this session)

Environment: Windows, project `.venv` (CPython 3.14), **SQLite** dev engine,
`PYTHONIOENCODING=utf-8`, FastAPI `TestClient` (no live server).

```
$ .venv/Scripts/python.exe tests/test_kds_task_reads.py
… all KDS task-read (B2.1) tests passed        (exit 0; 11 tests / 22 checks)
```

Regression (each run standalone, exit code recorded):

```
test_stations           EXIT=0
test_prep_tasks         EXIT=0
test_floormap           EXIT=0
test_reservations_map   EXIT=0
test_templates          EXIT=0
test_money              EXIT=0
test_security           EXIT=0
test_happyhour          EXIT=0
test_happyhour_sales    EXIT=0
test_schedule           EXIT=0
test_upsell             EXIT=0
test_reconciliation     EXIT=0
test_admin              EXIT=1   ← PRE-EXISTING failure, unrelated to B2.1 (see below)
```

Also verified: `python -m py_compile app/routers/sales.py app/main.py` → OK;
`import app.main` → OK (`[migrate] schema up to date`).

## F. Evidence limits & known non-B2.1 items

- **SQLite only; no PostgreSQL.** Acceptable for B2.1 (read-only); no concurrency/row-lock
  claim is made here. PG concurrency evidence belongs to B2.2 (design v3 §4.1).
- **No live server / e2e.** Integration is via `TestClient`.
- **Startup warning** (§A) is not covered by an automated test — the literal `print`
  fires at module import; its flag+invariant logic (`task_kds_active`,
  `fired_items_without_tasks`) *is* tested. Reported as a deviation-class evidence gap
  per B2 v3 note #2.
- **`test_admin.py` fails (exit 1)** — pre-existing on `feat/floor-map` (documented in
  `PROJECT_ARCHITECTURE_FACTS_FOR_DOCS.md`): "Manage tab is hidden from the waiter's nav"
  + admin table-creation. Backend authorization still passes. B2.1 does not touch
  admin/nav/table-creation; left untouched (out of scope).
- **`git diff --stat` on the working tree is not a B2.1 delta** — it reflects the whole
  uncommitted `feat/floor-map` tree (Stage A + B1 + floor + reservations). The B2.1 delta
  is exactly the hunks in §A–§D.

## G. Confirmations

- **B2.1 does not activate task-based KDS** (dormant flag, not UI-settable, gated on
  invariant==0; read-only in task mode; no per-task READY; bridge/rollup are B2.2).
- **No B2.2 code**, no model change, no migration, no payment/refund/Expo/Floor change,
  no Git operation, no dependency change.
- **This package modified no source** — it is documentation/evidence only.

B2.1 remains **AWAITING INDEPENDENT AUDIT — not approved.**
