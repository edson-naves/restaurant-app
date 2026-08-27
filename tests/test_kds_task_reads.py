"""Kitchen Stations Stage B2.1 — task-based KDS READS (read-only).

Proves the read-side migration from OrderItem.station_id to PreparationTask:
dormant-and-not-activatable gate, single-rule station scoping, Unassigned handling,
task-based badge counts, inactive-station reachability, one-OrderItem-rendered-once
with task sub-rows, and the complete Stage A fallback (no hybrid) when task mode is
inactive or blocked. B2.1 introduces NO mutation, so there are no READY/rollup tests
here (that is B2.2).

Activation model: task mode requires the CODE-level `sales.B2_2_ACTIVE` guard (False
throughout B2.1) AND the manual `kitchen_b2_active` setting AND readiness==0. In the
shipped B2.1 slice the guard is False, so task mode can never switch on — the read
tests below flip the guard in-process (`_activate` / `_reset_guard`) to simulate B2.2
being present so the read paths can be exercised; `test_b2_1_cannot_activate_before_b2_2`
proves the shipped guard keeps Stage A authoritative. Throwaway SQLite + overrides.
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
import _env  # noqa: F401  — declares the test opt-out before app imports

import app.routers.sales as _sales_mod
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
    """Set the manual operational flag ONLY (no code-level guard). On its own this
    can NOT activate task mode in the shipped B2.1 slice (see the guard test)."""
    db.add(Setting(key="kitchen_b2_active", value="1")); db.commit()


def _activate(db):
    """Turn the manual flag ON. The code-level B2.2 guard now ships True (B2.2 is
    implemented), so the flag + readiness invariant is what remains of the gate."""
    db.add(Setting(key="kitchen_b2_active", value="1")); db.commit()


def _reset_guard():
    # Restore the shipped default (B2.2 present) after a test that flipped it off.
    _sales_mod.B2_2_ACTIVE = True


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
# Activation gate
# --------------------------------------------------------------------------

def test_gate_dormant_by_default():
    db = _session(); _owner(db)
    check(task_kds_active(db) is False, "no flag → task_kds_active False (dormant default)")
    db.add(Setting(key="kitchen_b2_active", value="banana")); db.commit()
    check(task_kds_active(db) is False, "non-boolean flag value → still False")
    # B2.1 ships NO in-app path to set the flag: it is not an editable setting.
    from app.services import settings as _svc
    check("kitchen_b2_active" not in _svc.EDITABLE,
          "the b2 flag is not settable via the Settings form (no in-app activation path)")
    db.close()


def test_guard_off_forces_stage_a():
    # Regression for the B2.1 activation guard. The guard now ships True (B2.2), but
    # if it is ever off, NO flag/readiness combination can activate task mode — the
    # board stays fully Stage A. Flips it off in-process, always restores it.
    _sales_mod.B2_2_ACTIVE = False
    try:
        db = _session(); owner = _owner(db)
        grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.commit()
        ch, mi = _base(db, item_station=grill.id)
        _fired_item(db, ch, mi, [grill.id])   # readiness == 0 (the item has a task)
        _flag_on(db)                          # manual flag ON, guard off
        check(task_kds_active(db) is False,
              "guard off → not activatable even with flag + readiness 0")
        c = _client(db, owner)
        html = _get(c).text
        check("k-tasks" not in html and 'name="item_id"' in html,
              "guard off → the full Stage A KDS (legacy actions, no task sub-rows)")
        app.dependency_overrides.clear(); db.close()
    finally:
        _sales_mod.B2_2_ACTIVE = True


def test_gate_on_only_when_no_missing_tasks():
    # Simulates B2.2 present (guard on) to exercise the flag+readiness gate logic.
    db = _session(); _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _activate(db)
    check(task_kds_active(db) is True, "guard on + flag + zero fired-without-tasks → active")
    _fired_item(db, ch, mi, [grill.id], with_tasks=False)   # a fired item with NO tasks
    check(task_kds_active(db) is False, "flag on but a fired item lacks tasks → NOT active")
    _reset_guard(); db.close()


# --------------------------------------------------------------------------
# Read paths (guard simulated on)
# --------------------------------------------------------------------------

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


def test_task_mode_renders_subrows_with_per_task_ready():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True)
    fryer = Station(name="Fryer", type="production", is_active=True)
    db.add_all([grill, fryer]); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id, fryer.id])     # one sale line, two station tasks
    _activate(db)
    c = _client(db, owner)
    html = _get(c).text
    check(html.count("Burger") == 1, "the sale item renders exactly once (not duplicated per task)")
    check("k-tasks" in html and "Grill" in html and "Fryer" in html,
          "task station sub-rows render for both stations")
    # B2.2: task mode drives per-task READY (POST /kitchen/tasks/<id>/ready) and NOT
    # the legacy per-line item action (no item_id / no order-level "mark all ready").
    check("/kitchen/tasks/" in html and 'name="item_id"' not in html,
          "task mode exposes per-task READY, not the legacy per-line item action")
    _reset_guard(); app.dependency_overrides.clear(); db.close()


def test_task_mode_coarse_unready_control_present():
    # Design v3 §5.3: an operator-facing coarse control that resets readiness at ALL
    # stations of the item, via the existing bridge (POST status=preparing). Shown
    # once the item has a ready task.
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True)
    f = Station(name="Fryer", type="production", is_active=True)
    db.add_all([g, f]); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id, f.id])
    db.refresh(oi, ["tasks"]); oi.tasks[0].kitchen_status = PreparationTaskStatus.READY; db.commit()
    _activate(db)
    c = _client(db, owner)
    html = _get(c).text
    check('name="status" value="preparing"' in html and f'name="item_id" value="{oi.id}"' in html,
          "task mode shows a coarse un-ready control posting PREPARING for the item")
    check("all stations" in html.lower(),
          "the un-ready control clearly says it resets readiness for ALL stations")
    _reset_guard(); app.dependency_overrides.clear(); db.close()


def test_selected_station_shows_only_its_task():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True)
    fryer = Station(name="Fryer", type="production", is_active=True)
    db.add_all([grill, fryer]); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id, fryer.id])
    _activate(db)
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
    _reset_guard(); app.dependency_overrides.clear(); db.close()


def test_unassigned_scoping():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.commit()
    ch, mi = _base(db, item_station=None)
    _fired_item(db, ch, mi, [None])               # base task, Unassigned (NULL station)
    _activate(db)
    c = _client(db, owner)
    check("Burger" in _get(c, station="unassigned").text,
          "Unassigned tab shows the NULL-station task")
    check("Burger" in _get(c, station="all").text,
          "Unassigned work is also visible on the All board (never hidden)")
    check("Burger" not in _get(c, station=str(grill.id)).text,
          "Unassigned work NEVER appears under a selected station")
    _reset_guard(); app.dependency_overrides.clear(); db.close()


def test_station_filter_excludes_other_orders():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True)
    fryer = Station(name="Fryer", type="production", is_active=True)
    db.add_all([grill, fryer]); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    og, _ = _fired_item(db, ch, mi, [grill.id])
    of, _ = _fired_item(db, ch, mi, [fryer.id])
    _activate(db)
    c = _client(db, owner)
    html = _get(c, station=str(grill.id)).text
    check(og.code in html and of.code not in html,
          "station filter excludes the whole non-matching order (filtered in SQL before LIMIT)")
    _reset_guard(); app.dependency_overrides.clear(); db.close()


def test_badge_counts_are_task_based():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True)
    fryer = Station(name="Fryer", type="production", is_active=True)
    db.add_all([grill, fryer]); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id, fryer.id])   # 1 sale line → 2 station-work units
    _activate(db)
    c = _client(db, owner)
    html = _get(c).text
    check("All stations 2" in html,
          "All badge counts PreparationTasks (2), not sale lines (which would be 1)")
    check("Total items: 1" in html,
          "total_items stays sale-item quantity (1), distinct from the 2 station-work units")
    _reset_guard(); app.dependency_overrides.clear(); db.close()


def test_inactive_station_with_live_task_keeps_tab():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id])
    grill.is_active = False; db.commit()            # deactivated AFTER firing
    _activate(db)
    c = _client(db, owner)
    html = _get(c).text
    check("Grill" in html, "an inactive station with live tasks keeps its KDS tab reachable")
    _reset_guard(); app.dependency_overrides.clear(); db.close()


def test_blocked_activation_falls_back_to_full_stage_a():
    # Guard simulated on (B2.2 present) so the readiness block is the thing under test.
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id])                       # a proper task-backed item
    _fired_item(db, ch, mi, [grill.id], with_tasks=False)     # a fired item with NO tasks
    _activate(db)
    c = _client(db, owner)
    html = _get(c).text
    check(task_kds_active(db) is False, "guard on + a task-less fired item → gate stays off")
    check("activation blocked" in html.lower(), "operator sees the blocked-activation notice")
    check("k-tasks" not in html and 'name="item_id"' in html,
          "blocked → COMPLETE Stage A board (legacy actions), never a hybrid task board")
    _reset_guard(); app.dependency_overrides.clear(); db.close()


if __name__ == "__main__":
    try:
        for fn in [
            test_gate_dormant_by_default,
            test_guard_off_forces_stage_a,
            test_gate_on_only_when_no_missing_tasks,
            test_stage_a_when_flag_off,
            test_task_mode_renders_subrows_with_per_task_ready,
            test_task_mode_coarse_unready_control_present,
            test_selected_station_shows_only_its_task,
            test_unassigned_scoping,
            test_station_filter_excludes_other_orders,
            test_badge_counts_are_task_based,
            test_inactive_station_with_live_task_keeps_tab,
            test_blocked_activation_falls_back_to_full_stage_a,
        ]:
            print(f"- {fn.__name__}")
            fn()
    finally:
        _reset_guard()   # never leak the guard flip out of this process
    print()
    if _fail:
        print(f"{len(_fail)} FAILED")
        sys.exit(1)
    print("all KDS task-read (B2.1) tests passed")
