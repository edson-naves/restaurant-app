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
from sqlalchemy import create_engine, event, select, text
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


# --------------------------------------------------------------------------
# Per-task READY route
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Legacy /kitchen/{order}/status compatibility bridge
# --------------------------------------------------------------------------

def test_bridge_ready_routes_through_tasks_and_preserves_ready_at():
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True)
    f = Station(name="Fryer", type="production", is_active=True)
    db.add_all([g, f]); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id, f.id])
    # Grill already ready with an OLD ready_at; the bridge must not overwrite it.
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
        _ready(c, t.id)                       # item READY, both tasks ready
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
    _ready(c, _tasks(db, oi.id)[0].id)        # ready first
    r = _bridge(c, o.id, "pending", item_id=oi.id)
    check(r.status_code == 409, "PENDING on a task-backed item is rejected (409)")
    check(_tasks(db, oi.id)[0].kitchen_status == "ready",
          "no mutation on rejection — the task is untouched")
    check(db.get(OrderItem, oi.id).kitchen_status == KitchenStatus.READY, "item unchanged")
    app.dependency_overrides.clear(); db.close()


def test_bridge_mixed_request_pending_is_all_or_nothing():
    # A whole-order PENDING that touches a task-backed item must reject the WHOLE
    # request before any mutation (no partial update).
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True); db.add(g); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id])           # task-backed
    # a second, legacy no-task fired item on the SAME order
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


def test_bridge_ready_rejected_when_no_active_task():
    # Design v3 §5.1: a task-backed target with no active task (all SERVED) has no
    # valid READY transition — the bridge must reject the WHOLE request, no mutation.
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True); db.add(g); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id])
    for t in _tasks(db, oi.id):
        t.kitchen_status = PreparationTaskStatus.SERVED
    legacy = OrderItem(order_id=o.id, menu_item_id=mi.id, quantity=1,
                       unit_price_cents=mi.price_cents, kitchen_status=KitchenStatus.PREPARING, course=2)
    db.add(legacy); db.commit()
    c = _client(db, owner)
    r = _bridge(c, o.id, "ready")                     # whole order
    check(r.status_code == 409, "READY rejected when a task-backed target has no active task")
    check(_tasks(db, oi.id)[0].kitchen_status == "served", "the served task is untouched")
    check(db.get(OrderItem, legacy.id).kitchen_status == KitchenStatus.PREPARING,
          "the legacy sibling is NOT changed (all-or-nothing)")
    app.dependency_overrides.clear(); db.close()


def test_bridge_ready_rejected_on_unsupported_task_state():
    # Phase 1 must enforce the per-task contract: a non-served task in an unsupported
    # state (here PENDING) makes the whole READY request invalid — reject all-or-
    # nothing, never silently ignore it in Phase 2. The task CHECK constraint forbids
    # 'pending', so we inject it with PRAGMA ignore_check_constraints to simulate an
    # unexpected/corrupt row.
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True)
    f = Station(name="Fryer", type="production", is_active=True)
    db.add_all([g, f]); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id, f.id])
    ts = _tasks(db, oi.id)
    db.execute(text("PRAGMA ignore_check_constraints=ON"))
    db.execute(text("UPDATE preparation_task SET kitchen_status='pending' WHERE id=:i"),
               {"i": ts[1].id})
    db.commit()
    db.execute(text("PRAGMA ignore_check_constraints=OFF"))
    db.expire_all()
    legacy = OrderItem(order_id=o.id, menu_item_id=mi.id, quantity=1,
                       unit_price_cents=mi.price_cents, kitchen_status=KitchenStatus.PREPARING, course=2)
    db.add(legacy); db.commit()
    c = _client(db, owner)
    r = _bridge(c, o.id, "ready")                     # whole order
    check(r.status_code == 409,
          "READY rejected when a task-backed target has an unsupported task state")
    fresh = {t.station_id: t for t in _tasks(db, oi.id)}
    check(fresh[g.id].kitchen_status == "preparing" and fresh[f.id].kitchen_status == "pending",
          "no mutation on rejection — both tasks untouched")
    check(db.get(OrderItem, legacy.id).kitchen_status == KitchenStatus.PREPARING,
          "the legacy sibling is NOT changed (all-or-nothing)")
    app.dependency_overrides.clear(); db.close()


def test_bridge_legacy_no_task_item_still_direct():
    # Regression: an item with no PreparationTasks keeps the Stage A direct write.
    db = _session(); owner = _owner(db)
    ch, mi = _base(db, item_station=None)
    o, oi = _fired_item(db, ch, mi, [], with_tasks=False)   # fired, no tasks
    c = _client(db, owner)
    r = _bridge(c, o.id, "ready", item_id=oi.id)
    check(r.status_code == 303, "legacy no-task bridge redirects (303)")
    check(db.get(OrderItem, oi.id).kitchen_status == KitchenStatus.READY,
          "a no-task item is set directly (Stage A behaviour preserved)")
    app.dependency_overrides.clear(); db.close()


# --------------------------------------------------------------------------
# Functional convergence (NOT a PostgreSQL concurrency proof — SQLite serialises)
# --------------------------------------------------------------------------

def test_two_sessions_ready_siblings_converge():
    """Two independent sessions ready the two sibling tasks and commit; the final
    rollup is READY. SQLite serialises writers and FOR UPDATE is a no-op here, so
    this proves functional convergence ONLY — it is NOT PostgreSQL row-lock
    evidence (that requires a real PostgreSQL run; reported as pending)."""
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
        test_bridge_ready_rejected_when_no_active_task,
        test_bridge_ready_rejected_on_unsupported_task_state,
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
