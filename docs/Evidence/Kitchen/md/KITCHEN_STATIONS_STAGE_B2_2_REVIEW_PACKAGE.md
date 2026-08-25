# Kitchen Stations — Stage B2.2 Independent-Audit Package

**Purpose:** evidence for an independent audit of the B2.2 implementation against the
approved `KITCHEN_STATIONS_STAGE_B2_v3_DESIGN.md` (§4 per-task READY, §5 legacy-write
bridge). **No code was changed to produce this file. No B3/B4.**

B2.2 status: **implemented, AWAITING INDEPENDENT AUDIT — not approved.**
**PostgreSQL concurrency evidence is PENDING (required for acceptance) — see §G.**

## What to review against
- Design: `KITCHEN_STATIONS_STAGE_B2_v3_DESIGN.md` §4 (task READY + post-lock refresh),
  §5 (two-phase bridge, `ready_at` preservation, PENDING rejection, coarse §5.3).
- Current work / authorization: `docs/03_CURRENT_WORK.md`.
- Prior slices already approved: Stage A, B1, B2.1 (read-side).

## Scope of the change (these files)

| File | Kind | B2.2 content |
|---|---|---|
| `app/routers/sales.py` | modified | `B2_2_ACTIVE=True`; new `POST /kitchen/tasks/{id}/ready`; two-phase bridge in `kitchen_status` |
| `web/templates/kitchen.html` | modified | per-task READY control in task mode |
| `tests/test_kds_task_ready.py` | new | 11 B2.2 mutation/bridge tests |
| `tests/test_kds_task_reads.py` | modified | guard now ships True; harness + one test adjusted |

## Diff methodology (same as B2.1 package)
No baseline commit exists (the whole `feat/floor-map` tree is uncommitted; `HEAD`
predates Stage A/B1), so a raw `git diff` mixes Stage A + B1 + B2.1 + B2.2 and cannot
isolate B2.2. The hunks below are the **exact B2.2 edits** (old→new), anchored to
current line numbers; grep-verify in the live file, e.g.
`grep -nE "def task_ready|B2_2_ACTIVE = True|PHASE 1|PHASE 2" app/routers/sales.py`.

---

## A. `app/routers/sales.py`

### A.1 — enable the code-level guard (line 1707)

```diff
-B2_2_ACTIVE = False
+B2_2_ACTIVE = True
```
(comment updated to say B2.2 is the slice that provides the write path; activation
still ALSO requires the manual flag + `fired_items_without_tasks == 0`.)

### A.2 — new per-task READY route (`POST /kitchen/tasks/{task_id}/ready`, line 2370)

```python
@router.post("/kitchen/tasks/{task_id}/ready")
def task_ready(task_id: int, next: str = Form(""), db: Session = Depends(get_db),
               staff: Staff = Depends(require("kitchen.update"))):
    task = db.get(PreparationTask, task_id)
    if task is None:
        raise HTTPException(404, "Preparation task not found")
    order_id = task.order_id
    # Lock the parent order, then discard the pre-lock view and reselect fresh.
    if db.execute(select(Order.id).where(Order.id == order_id).with_for_update()).first() is None:
        raise HTTPException(404, "Order not found")
    task = db.execute(
        select(PreparationTask).where(PreparationTask.id == task_id)
        .execution_options(populate_existing=True)
    ).scalars().first()
    if task is None:
        raise HTTPException(409, "Preparation task no longer exists.")
    item = task.order_item
    db.refresh(item, ["tasks"])             # fresh sibling set for the rollup

    now = datetime.now()
    if task.kitchen_status == PreparationTaskStatus.READY:
        pass                                # idempotent — keep the original ready_at
    elif task.kitchen_status == PreparationTaskStatus.PREPARING:
        task.kitchen_status = PreparationTaskStatus.READY
        task.ready_at = task.ready_at or now
    else:                                   # SERVED or anything unexpected
        raise HTTPException(409, "This task cannot be marked ready from its current state.")

    rollup_item_kitchen_status(item)        # item READY only when ALL non-served tasks READY
    _recompute_kitchen(item.order, now)
    db.commit()
    return RedirectResponse(_safe_next_board(next), status_code=303)
```

Design mapping: §4 step 1 (`db.get` to learn the order) → step 2 (Order `FOR UPDATE`)
→ step 3 (reselect task with `populate_existing` + `db.refresh(item,["tasks"])`; the
transition/rollup use only this post-lock state) → step 4 (explicit
PREPARING→READY / idempotent READY→READY preserving `ready_at` / reject others) →
steps 5–8 (never SERVED; `rollup_item_kitchen_status`; `_recompute_kitchen`; commit).

### A.3 — two-phase legacy bridge in `kitchen_status` (line 2288) — old → new

```diff
-    order = db.get(Order, order_id)
-    if order is None:
-        raise HTTPException(404, "Order not found")
-    if status not in (KitchenStatus.PENDING, KitchenStatus.PREPARING, KitchenStatus.READY):
-        raise HTTPException(400, "Invalid kitchen status.")
-
-    if item_id:
-        targets = [i for i in order.items if i.id == item_id]
-    elif course:
-        targets = [i for i in order.items if i.course == course]
-    else:
-        targets = order.items
-    for item in targets:
-        if item.kitchen_status == KitchenStatus.SERVED:
-            continue
-        item.kitchen_status = status
-
-    now = datetime.now()
+    if status not in (KitchenStatus.PENDING, KitchenStatus.PREPARING, KitchenStatus.READY):
+        raise HTTPException(400, "Invalid kitchen status.")
+    order = db.get(Order, order_id)
+    if order is None:
+        raise HTTPException(404, "Order not found")
+    # Serialize against concurrent task READYs / kitchen writes on this order, then
+    # work only from post-lock refreshed state (never pre-lock objects).
+    db.execute(select(Order.id).where(Order.id == order_id).with_for_update()).first()
+    db.refresh(order, ["items"])
+
+    if item_id:
+        targets = [i for i in order.items if i.id == item_id]
+    elif course:
+        targets = [i for i in order.items if i.course == course]
+    else:
+        targets = list(order.items)
+    for i in targets:
+        db.refresh(i, ["tasks"])            # fresh sibling task state per target
+
+    now = datetime.now()
+    # PHASE 1 — validate every target BEFORE any mutation (no partial commit).
+    for item in targets:
+        if item.kitchen_status == KitchenStatus.SERVED:
+            continue
+        if item.tasks and status == KitchenStatus.PENDING:
+            db.rollback()
+            raise HTTPException(409, "A fired kitchen item cannot be set back to pending.")
+
+    # PHASE 2 — apply. Task-backed items route through their tasks + rollup; legacy
+    # (no-task) items keep the Stage A direct write.
+    for item in targets:
+        if item.kitchen_status == KitchenStatus.SERVED:
+            continue
+        if item.tasks:
+            active = [t for t in item.tasks if t.kitchen_status != PreparationTaskStatus.SERVED]
+            if status == KitchenStatus.READY:
+                for t in active:
+                    if t.kitchen_status == PreparationTaskStatus.PREPARING:
+                        t.kitchen_status = PreparationTaskStatus.READY
+                        t.ready_at = t.ready_at or now      # preserve an existing ready_at
+                rollup_item_kitchen_status(item)
+            elif status == KitchenStatus.PREPARING:
+                # Coarse "un-ready this item" (design v3 §5.3) — DESTRUCTIVE: clears
+                # READY at EVERY station of the item.
+                for t in active:
+                    t.kitchen_status = PreparationTaskStatus.PREPARING
+                    t.ready_at = None
+                rollup_item_kitchen_status(item)
+        else:
+            item.kitchen_status = status
+
     if status == KitchenStatus.PREPARING:
         order.sent_to_kitchen_at = order.sent_to_kitchen_at or now
     _recompute_kitchen(order, now)
     db.commit()
     return RedirectResponse(_safe_next_board(next), status_code=303)
```

---

## B. `web/templates/kitchen.html` — per-task READY control (line 120) — old → new

```diff
               <div class="k-task">
                 {% if tk.station %}<span class="k-st-chip" style="--sc: {{ tk.station.swatch }}">{{ tk.station.icon }} {{ tk.station.name }}</span>{% else %}<span class="k-st-chip k-st-none">⚠ Unassigned</span>{% endif %}
-                <span class="k-task-status {{ 'ok' if tk.kitchen_status == 'ready' else '' }}" title="...">{{ '✓' if tk.kitchen_status == 'ready' else '⏳' }}</span>
+                {% if can('kitchen.update') and tk.kitchen_status == 'preparing' %}
+                {# Kitchen Stations B2.2 — mark THIS station's task ready. The item
+                   rolls up to Ready only when every station's task is ready. #}
+                <form method="post" action="/kitchen/tasks/{{ tk.id }}/ready" class="k-line-act">
+                  <button type="submit" class="k-check" title="Mark {{ tk.station.name if tk.station else 'Unassigned' }} ready" aria-label="Mark task ready"></button>
+                </form>
+                {% else %}
+                <span class="k-task-status {{ 'ok' if tk.kitchen_status == 'ready' else '' }}" title="...">{{ '✓' if tk.kitchen_status == 'ready' else '⏳' }}</span>
+                {% endif %}
               </div>
```

The per-task form posts to `/kitchen/tasks/<id>/ready` and carries **no** `item_id` —
so task mode never exposes the legacy per-line item action.

---

## C. Test changes

### C.1 — `tests/test_kds_task_reads.py` (B2.1 read tests), guard now ships True

```diff
 def _activate(db):
-    """Flag ON + flip the code-level B2.2 guard ... Every caller MUST pair it with _reset_guard()."""
-    db.add(Setting(key="kitchen_b2_active", value="1")); db.commit()
-    _sales_mod.B2_2_ACTIVE = True
+    """Turn the manual flag ON. The code-level B2.2 guard now ships True ..."""
+    db.add(Setting(key="kitchen_b2_active", value="1")); db.commit()

 def _reset_guard():
-    _sales_mod.B2_2_ACTIVE = False
+    _sales_mod.B2_2_ACTIVE = True   # restore shipped default (B2.2 present)
```
- `test_b2_1_cannot_activate_before_b2_2` → **`test_guard_off_forces_stage_a`**: now flips
  the guard OFF in-process (in a `try/finally` that restores True) and proves that with
  the guard off, no flag/readiness activates task mode — the board stays full Stage A.
- `test_task_mode_renders_subrows_readonly` → **`test_task_mode_renders_subrows_with_per_task_ready`**:
  asserts task mode exposes `/kitchen/tasks/` (per-task READY) and **not** the legacy
  per-line item action (`name="item_id"` absent).

### C.2 — `tests/test_kds_task_ready.py` (new, complete)

```python
"""Kitchen Stations Stage B2.2 — per-task READY + legacy-write compatibility bridge.

Covers the mutation slice: `POST /kitchen/tasks/{id}/ready` (transitions, idempotency,
rollup, READY != SERVED), and the two-phase legacy `/kitchen/{order}/status` bridge
(READY/PREPARING through tasks, PENDING rejected for task-backed, all-or-nothing).
Throwaway SQLite + dependency overrides.

Concurrency note: SQLite makes `SELECT ... FOR UPDATE` a no-op and serialises writers,
so it CANNOT demonstrate PostgreSQL row-lock / post-lock-refresh semantics. The
two-session test here proves functional convergence only and is explicitly NOT
PostgreSQL evidence (see the B2.2 review package / handoff).
Run: python tests/test_kds_task_ready.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.deps import current_staff, get_db
from app.main import app
from app.models import oltp  # noqa: F401
from app.models.oltp import (
    Channel, KitchenStatus, MenuCategory, MenuItem, Order, OrderItem, OrderStatus,
    PreparationTask, PreparationTaskStatus, Staff, Station,
)
from app.routers.sales import rollup_item_kitchen_status

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _engine():
    path = os.path.join(tempfile.gettempdir(), f"kdsr_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return engine


def _session():
    return sessionmaker(bind=_engine(), future=True, expire_on_commit=False)()


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


def _fired_item(db, ch, mi, station_ids, item_ks=KitchenStatus.PREPARING, with_tasks=True):
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


def _tasks(db, oi_id):
    return db.execute(
        select(PreparationTask).where(PreparationTask.order_item_id == oi_id)
        .order_by(PreparationTask.id)
    ).scalars().all()


def _ready(c, task_id):
    return c.post(f"/kitchen/tasks/{task_id}/ready", data={"next": "/kitchen"},
                  follow_redirects=False)


def _bridge(c, order_id, status, item_id=0, course=0):
    return c.post(f"/kitchen/{order_id}/status",
                  data={"status": status, "item_id": item_id, "course": course, "next": "/kitchen"},
                  follow_redirects=False)


# --- Per-task READY route ---

def test_one_task_ready_does_not_ready_sibling():
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True)
    f = Station(name="Fryer", type="production", is_active=True)
    db.add_all([g, f]); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id, f.id])
    grill_task = _tasks(db, oi.id)[0]
    c = _client(db, owner)
    r = _ready(c, grill_task.id)
    check(r.status_code == 303, "per-task READY redirects (303)")
    ts = {t.station_id: t for t in _tasks(db, oi.id)}
    check(ts[g.id].kitchen_status == "ready" and ts[g.id].ready_at is not None,
          "the readied task is READY with a ready_at")
    check(ts[f.id].kitchen_status == "preparing", "the sibling task stays PREPARING")
    check(db.get(OrderItem, oi.id).kitchen_status == KitchenStatus.PREPARING,
          "OrderItem rolls up to PREPARING while any task is not ready")
    app.dependency_overrides.clear(); db.close()


def test_all_tasks_ready_rolls_item_up_to_ready():
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True)
    f = Station(name="Fryer", type="production", is_active=True)
    db.add_all([g, f]); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id, f.id])
    c = _client(db, owner)
    for t in _tasks(db, oi.id):
        _ready(c, t.id)
    item = db.get(OrderItem, oi.id)
    check(item.kitchen_status == KitchenStatus.READY,
          "OrderItem rolls up to READY only when every task is READY")
    check(item.kitchen_status != KitchenStatus.SERVED, "READY is not SERVED")
    check(all(t.kitchen_status == "ready" for t in _tasks(db, oi.id)), "both tasks READY")
    check(db.get(Order, o.id).status != OrderStatus.SERVED,
          "the order is not SERVED by kitchen readiness (READY != SERVED)")
    app.dependency_overrides.clear(); db.close()


def test_duplicate_ready_is_idempotent_and_preserves_ready_at():
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True); db.add(g); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id])
    t = _tasks(db, oi.id)[0]
    c = _client(db, owner)
    _ready(c, t.id)
    first = db.get(PreparationTask, t.id).ready_at
    r = _ready(c, t.id)                       # ready again
    check(r.status_code == 303, "second READY is accepted (idempotent, 303)")
    check(db.get(PreparationTask, t.id).ready_at == first,
          "a repeat READY preserves the original ready_at (no overwrite)")
    check(db.get(OrderItem, oi.id).kitchen_status == KitchenStatus.READY, "item still READY")
    app.dependency_overrides.clear(); db.close()


def test_served_task_ready_rejected_no_change():
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True); db.add(g); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id])
    t = _tasks(db, oi.id)[0]
    t.kitchen_status = PreparationTaskStatus.SERVED; db.commit()
    c = _client(db, owner)
    r = _ready(c, t.id)
    check(r.status_code == 409, "readying a SERVED task is rejected (409)")
    check(db.get(PreparationTask, t.id).kitchen_status == "served", "the SERVED task is unchanged")
    app.dependency_overrides.clear(); db.close()


def test_ready_unknown_task_404():
    db = _session(); owner = _owner(db)
    c = _client(db, owner)
    check(_ready(c, 999999).status_code == 404, "unknown task id → 404")
    app.dependency_overrides.clear(); db.close()


# --- Legacy /kitchen/{order}/status bridge ---

def test_bridge_ready_routes_through_tasks_and_preserves_ready_at():
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True)
    f = Station(name="Fryer", type="production", is_active=True)
    db.add_all([g, f]); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id, f.id])
    grill = _tasks(db, oi.id)[0]
    old = datetime.now() - timedelta(hours=1)
    grill.kitchen_status = PreparationTaskStatus.READY; grill.ready_at = old; db.commit()
    c = _client(db, owner)
    r = _bridge(c, o.id, "ready", item_id=oi.id)
    check(r.status_code == 303, "bridge READY redirects (303)")
    ts = {t.station_id: t for t in _tasks(db, oi.id)}
    check(ts[g.id].ready_at == old, "an already-READY task keeps its original ready_at")
    check(ts[f.id].kitchen_status == "ready" and ts[f.id].ready_at is not None,
          "the still-preparing task is moved to READY with a ready_at")
    check(db.get(OrderItem, oi.id).kitchen_status == KitchenStatus.READY,
          "the item is NOT written directly — it rolls up to READY from its tasks")
    app.dependency_overrides.clear(); db.close()


def test_bridge_preparing_resets_tasks_and_clears_ready_at():
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True)
    f = Station(name="Fryer", type="production", is_active=True)
    db.add_all([g, f]); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id, f.id])
    c = _client(db, owner)
    for t in _tasks(db, oi.id):
        _ready(c, t.id)
    r = _bridge(c, o.id, "preparing", item_id=oi.id)   # coarse un-ready (§5.3)
    check(r.status_code == 303, "bridge PREPARING redirects (303)")
    check(all(t.kitchen_status == "preparing" and t.ready_at is None for t in _tasks(db, oi.id)),
          "coarse override resets ALL active tasks to PREPARING and clears ready_at")
    check(db.get(OrderItem, oi.id).kitchen_status == KitchenStatus.PREPARING,
          "the item rolls back to PREPARING")
    app.dependency_overrides.clear(); db.close()


def test_bridge_pending_rejected_for_task_backed_no_mutation():
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True); db.add(g); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id])
    c = _client(db, owner)
    _ready(c, _tasks(db, oi.id)[0].id)
    r = _bridge(c, o.id, "pending", item_id=oi.id)
    check(r.status_code == 409, "PENDING on a task-backed item is rejected (409)")
    check(_tasks(db, oi.id)[0].kitchen_status == "ready",
          "no mutation on rejection — the task is untouched")
    check(db.get(OrderItem, oi.id).kitchen_status == KitchenStatus.READY, "item unchanged")
    app.dependency_overrides.clear(); db.close()


def test_bridge_mixed_request_pending_is_all_or_nothing():
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True); db.add(g); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id])           # task-backed
    now = datetime.now()
    legacy = OrderItem(order_id=o.id, menu_item_id=mi.id, quantity=1,
                       unit_price_cents=mi.price_cents, kitchen_status=KitchenStatus.READY, course=2)
    db.add(legacy); db.commit()
    c = _client(db, owner)
    r = _bridge(c, o.id, "pending")                   # whole order
    check(r.status_code == 409, "whole-order PENDING rejected because a task-backed target exists")
    check(_tasks(db, oi.id)[0].kitchen_status == "preparing",
          "task-backed item untouched (no partial mutation)")
    check(db.get(OrderItem, legacy.id).kitchen_status == KitchenStatus.READY,
          "the legacy sibling is NOT changed either (all-or-nothing)")
    app.dependency_overrides.clear(); db.close()


def test_bridge_legacy_no_task_item_still_direct():
    db = _session(); owner = _owner(db)
    ch, mi = _base(db, item_station=None)
    o, oi = _fired_item(db, ch, mi, [], with_tasks=False)   # fired, no tasks
    c = _client(db, owner)
    r = _bridge(c, o.id, "ready", item_id=oi.id)
    check(r.status_code == 303, "legacy no-task bridge redirects (303)")
    check(db.get(OrderItem, oi.id).kitchen_status == KitchenStatus.READY,
          "a no-task item is set directly (Stage A behaviour preserved)")
    app.dependency_overrides.clear(); db.close()


# --- Functional convergence (NOT a PostgreSQL concurrency proof) ---

def test_two_sessions_ready_siblings_converge():
    """Two independent sessions ready the two sibling tasks and commit; the final
    rollup is READY. SQLite serialises writers and FOR UPDATE is a no-op here, so
    this proves functional convergence ONLY — NOT PostgreSQL row-lock evidence."""
    engine = _engine()
    Sess = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    setup = Sess(); owner = _owner(setup)
    g = Station(name="Grill", type="production", is_active=True)
    f = Station(name="Fryer", type="production", is_active=True)
    setup.add_all([g, f]); setup.commit()
    ch, mi = _base(setup, item_station=g.id)
    o, oi = _fired_item(setup, ch, mi, [g.id, f.id])
    tids = [t.id for t in _tasks(setup, oi.id)]

    a, b = Sess(), Sess()
    app.dependency_overrides[current_staff] = lambda: owner
    app.dependency_overrides[get_db] = lambda: a
    ra = TestClient(app).post(f"/kitchen/tasks/{tids[0]}/ready",
                              data={"next": "/kitchen"}, follow_redirects=False)
    app.dependency_overrides[get_db] = lambda: b
    rb = TestClient(app).post(f"/kitchen/tasks/{tids[1]}/ready",
                              data={"next": "/kitchen"}, follow_redirects=False)
    app.dependency_overrides.clear()
    check(ra.status_code == 303 and rb.status_code == 303, "both session READYs accepted")

    verify = Sess()
    check(verify.get(OrderItem, oi.id).kitchen_status == KitchenStatus.READY,
          "after both sibling tasks are readied, the item converges to READY")
    setup.close(); a.close(); b.close(); verify.close()


if __name__ == "__main__":
    for fn in [
        test_one_task_ready_does_not_ready_sibling,
        test_all_tasks_ready_rolls_item_up_to_ready,
        test_duplicate_ready_is_idempotent_and_preserves_ready_at,
        test_served_task_ready_rejected_no_change,
        test_ready_unknown_task_404,
        test_bridge_ready_routes_through_tasks_and_preserves_ready_at,
        test_bridge_preparing_resets_tasks_and_clears_ready_at,
        test_bridge_pending_rejected_for_task_backed_no_mutation,
        test_bridge_mixed_request_pending_is_all_or_nothing,
        test_bridge_legacy_no_task_item_still_direct,
        test_two_sessions_ready_siblings_converge,
    ]:
        print(f"- {fn.__name__}")
        fn()
    print()
    if _fail:
        print(f"{len(_fail)} FAILED")
        sys.exit(1)
    print("all KDS task-ready (B2.2) tests passed")
```

### Test → requirement mapping

| Test | Proves (design v3) |
|---|---|
| `test_one_task_ready_does_not_ready_sibling` | §4: per-task READY; sibling untouched; rollup PREPARING |
| `test_all_tasks_ready_rolls_item_up_to_ready` | §4/§ADR-007: item READY only when all tasks READY; **READY != SERVED** |
| `test_duplicate_ready_is_idempotent_and_preserves_ready_at` | §4 step 4: idempotent READY, `ready_at` preserved |
| `test_served_task_ready_rejected_no_change` | §4 step 4: SERVED→READY rejected, no change |
| `test_ready_unknown_task_404` | missing task → 404 |
| `test_bridge_ready_routes_through_tasks_and_preserves_ready_at` | §5.2: bridge READY via tasks; already-READY `ready_at` preserved; item not written directly |
| `test_bridge_preparing_resets_tasks_and_clears_ready_at` | §5.3: coarse destructive un-ready resets all tasks + clears `ready_at` |
| `test_bridge_pending_rejected_for_task_backed_no_mutation` | §5.1/§5.2: PENDING rejected for task-backed; no mutation |
| `test_bridge_mixed_request_pending_is_all_or_nothing` | §5.1: validate-all-before-mutate; legacy sibling untouched too |
| `test_bridge_legacy_no_task_item_still_direct` | §5.2: no-task item keeps Stage A direct write |
| `test_two_sessions_ready_siblings_converge` | functional convergence only (NOT PG evidence) |

---

## D. Test commands + results

Environment: Windows, `.venv` (CPython 3.14), **SQLite**, `PYTHONIOENCODING=utf-8`,
FastAPI `TestClient` (no live server).

```
$ .venv/Scripts/python.exe tests/test_kds_task_ready.py   → all KDS task-ready (B2.2) tests passed   (exit 0; 11 tests)
$ .venv/Scripts/python.exe tests/test_kds_task_reads.py   → all KDS task-read (B2.1) tests passed     (exit 0)
```
Regression (each standalone, exit 0): `test_stations, test_prep_tasks, test_floormap,
test_reservations_map, test_templates, test_money, test_security, test_happyhour,
test_happyhour_sales, test_schedule, test_upsell, test_reconciliation`.
`test_admin` → exit 1 (**pre-existing**, "Manage tab is hidden from the waiter's nav";
unrelated to B2.2; backend authz passes). `import app.main` OK (`B2_2_ACTIVE=True`).

## E. Concurrency / refresh behavior
`task_ready`: `db.get` only to learn `order_id` → Order `FOR UPDATE` → **reselect the
task with `populate_existing=True` + `db.refresh(item,["tasks"])`** → transition + rollup
use only post-lock state. `kitchen_status` bridge: Order `FOR UPDATE` +
`db.refresh(order,["items"])` + per-target `db.refresh(item,["tasks"])` before the
two-phase validate/apply. On **SQLite** `FOR UPDATE` is a no-op and writers serialise —
correct functionally but NOT a row-lock demonstration.

## F. Legacy bridge behavior
Two-phase under the Order lock. **Phase 1** validates every target; a task-backed target
with `PENDING` → `rollback` + **409**, no mutation (all-or-nothing). **Phase 2**:
task-backed `READY` → mark active tasks READY, preserving an existing `ready_at`, then
rollup; task-backed `PREPARING` → **destructive coarse override (§5.3)** resetting all
active tasks + clearing `ready_at`, then rollup; no-task item → Stage A direct write.
`OrderItem.kitchen_status` is never written directly for task-backed items.

## G. PostgreSQL evidence status
**No live/integration PostgreSQL was executed. Evidence PENDING.** `DATABASE_URL` is
unset (dev = SQLite); `psycopg` is not in the dev environment. The two-session test is
functional convergence only and is **explicitly not** PostgreSQL row-lock evidence
(SQLite serialises writers; `FOR UPDATE` no-op). Per design v3 §4.1, B2.2 acceptance
requires a real PostgreSQL two-session concurrency run — **to be executed in a Postgres
environment before approval.** No SQLite-equals-Postgres claim is made.

## H. Payment-gate confirmation
Unchanged. Neither the per-task route nor the bridge ever sets `SERVED`; items only
reach `READY` (rollup never yields SERVED), and payment still requires SERVED items
(`_require_served` in `app/routers/pay.py`, untouched). `READY != SERVED` preserved;
tasks do not own SERVED.

## I. Deviations / evidence limits
- **No dedicated task-mode "un-ready" button.** Authorization item 9 = per-task READY
  only; the PREPARING coarse override (§5.3) is implemented + tested in the bridge and
  reachable via the legacy/Expo path, but the task-mode KDS shows no undo button. Minor
  UX gap, within scope.
- **PostgreSQL concurrency evidence pending** (§G) — the one acceptance gate not yet met.
- Startup warning's literal emission remains inspection-only (carryover from B2.1).
- `test_admin` failure is pre-existing and out of scope.

## J. git status --short
```
 M app/main.py                 (B2.1 startup block; no B2.2 edit)
 M app/routers/sales.py        (guard flip + task_ready + kitchen_status bridge)
 M web/templates/kitchen.html  (per-task READY control)
?? tests/test_kds_task_ready.py
?? tests/test_kds_task_reads.py
```
(Other modified/untracked files in the tree are unrelated prior `feat/floor-map` work,
not B2.2. A raw `git diff --stat` reflects the whole uncommitted tree, not the B2.2 delta.)

B2.2 remains **AWAITING INDEPENDENT AUDIT — not approved.** No B3/B4 started.
