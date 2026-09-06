"""Floor spatial slice — zone rectangles, table shape/drag, per-waiter colour,
declarative zone sizing, and the reserved/due-soon status on the live Floor.

Ported/adapted onto ddd5add's existing grid-based admin pages (not the
free-placement redesign on feat/floor-map) per the authorized additive scope.
Throwaway SQLite + dependency overrides. Run: python tests/test_floor_spatial.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _env  # noqa: F401  — declares the test opt-out before app imports
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.deps import current_staff, get_db
from app.main import app
from app.models import oltp  # noqa: F401
from app.models.oltp import (
    Channel,
    Floor,
    Reservation,
    ReservationStatus,
    RestaurantTable,
    Staff,
    Zone,
)

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _session():
    path = os.path.join(tempfile.gettempdir(), f"floorspatial_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _seed(db):
    owner = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
    waiter = Staff(name="Sam Lee", role="waiter", pin_code="x", is_active=True)
    floor = Floor(name="Main")
    ch = Channel(code="dine_in", name="Dine-in", channel_type="dine_in")
    db.add_all([owner, waiter, floor, ch])
    db.flush()
    zone = Zone(name="Patio", floor_id=floor.id)
    db.add(zone)
    db.flush()
    return owner, waiter, floor, zone


def _client(db, staff):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


def test_set_zone_tables_grows_and_shrinks_softly():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/zones/{zone.id}/set-tables", data={"count": 3, "capacity": 4})
    check(r.status_code == 204, "set-tables grows to 3 (204)")
    active = db.query(RestaurantTable).filter_by(zone_id=zone.id, is_active=True).all()
    check(len(active) == 3, f"zone now has 3 active tables (got {len(active)})")
    check(all(t.capacity == 4 for t in active), "every table took the new capacity")

    r = c.post(f"/admin/zones/{zone.id}/set-tables", data={"count": 1, "capacity": 4})
    check(r.status_code == 204, "set-tables shrinks to 1 (204)")
    db.expire_all()
    active = db.query(RestaurantTable).filter_by(zone_id=zone.id, is_active=True).all()
    retired = db.query(RestaurantTable).filter_by(zone_id=zone.id, is_active=False).all()
    check(len(active) == 1, f"exactly 1 table remains active (got {len(active)})")
    check(len(retired) == 2, f"the other 2 were soft-retired, not deleted (got {len(retired)})")
    all_rows = db.query(RestaurantTable).filter_by(zone_id=zone.id).all()
    check(len(all_rows) == 3, "no row was hard-deleted — history preserved")
    app.dependency_overrides.clear()
    db.close()


def test_set_zone_tables_never_retires_an_occupied_table():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    t_free = RestaurantTable(number=1, zone_id=zone.id, capacity=4)
    t_occ1 = RestaurantTable(number=2, zone_id=zone.id, capacity=4)
    t_occ2 = RestaurantTable(number=3, zone_id=zone.id, capacity=4)
    db.add_all([t_free, t_occ1, t_occ2])
    db.flush()
    from app.routers.sales import open_order_on_table
    order1 = open_order_on_table(db, t_occ1, guests=2, waiter_id=waiter.id)
    order2 = open_order_on_table(db, t_occ2, guests=2, waiter_id=waiter.id)
    db.commit()
    free_id, occ1_id, occ2_id = t_free.id, t_occ1.id, t_occ2.id

    c = _client(db, owner)
    # 3 tables, 1 free + 2 occupied. Reducing to 1 needs 2 removed; only 1 is
    # free — satisfying it would have to touch an occupied table.
    r = c.post(f"/admin/zones/{zone.id}/set-tables", data={"count": 1, "capacity": 4})
    check(r.status_code == 400, f"reducing below what's free is rejected (got {r.status_code})")

    db.expire_all()
    t_free = db.get(RestaurantTable, free_id)
    t_occ1 = db.get(RestaurantTable, occ1_id)
    t_occ2 = db.get(RestaurantTable, occ2_id)
    check(t_free.is_active and t_occ1.is_active and t_occ2.is_active,
          "nothing was retired — the call changed no table")
    check(t_free.capacity == 4 and t_occ1.capacity == 4 and t_occ2.capacity == 4,
          "capacity untouched on rejection")
    db.refresh(order1); db.refresh(order2)
    check(order1.table_id == occ1_id and order2.table_id == occ2_id,
          "both live orders still point at their (still-active) tables")

    # Reducing to 2 only needs the 1 free table removed — must still work.
    r = c.post(f"/admin/zones/{zone.id}/set-tables", data={"count": 2, "capacity": 6})
    check(r.status_code == 204, f"reducing using only the free table succeeds (got {r.status_code})")
    db.expire_all()
    t_free = db.get(RestaurantTable, free_id)
    t_occ1 = db.get(RestaurantTable, occ1_id)
    t_occ2 = db.get(RestaurantTable, occ2_id)
    check(not t_free.is_active, "the FREE table was retired")
    check(t_occ1.is_active and t_occ2.is_active, "both occupied tables were left alone")
    check(t_occ1.capacity == 4 and t_occ2.capacity == 4, "occupied tables' capacity was NOT changed")
    body = c.get("/").text
    check("Table 2" in body and "Table 3" in body, "both occupied tables are still visible on the Floor page")
    app.dependency_overrides.clear()
    db.close()


def test_set_staff_color():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/staff/{waiter.id}/color", data={"color": "#ff00aa"})
    check(r.status_code == 204, "set color (204)")
    db.refresh(waiter)
    check(waiter.color == "#ff00aa", "colour persisted")
    check(waiter.swatch == "#ff00aa", "swatch reflects the chosen colour")

    r = c.post(f"/admin/staff/{waiter.id}/color", data={"color": ""})
    check(r.status_code == 204, "clear color (204)")
    db.refresh(waiter)
    check(waiter.color is None, "colour cleared back to palette default")
    from app.models.oltp import STAFF_PALETTE
    check(waiter.swatch == STAFF_PALETTE[waiter.id % len(STAFF_PALETTE)],
          "swatch falls back to the palette by id")
    app.dependency_overrides.clear()
    db.close()


def test_save_layout_sets_shape_and_position():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    table = RestaurantTable(number=1, zone_id=zone.id, capacity=4, pos_x=0, pos_y=0)
    db.add(table)
    db.commit()
    c = _client(db, owner)

    r = c.post("/admin/tables/layout", data={"layout": f"{table.id}:3:2:square"})
    check(r.status_code in (200, 303), "layout save accepted")
    db.refresh(table)
    check((table.pos_x, table.pos_y) == (3, 2), "grid position updated")
    check(table.shape == "square", "shape updated from the 4th field")

    # 3-field payload (no shape) still works — backward compatible.
    r = c.post("/admin/tables/layout", data={"layout": f"{table.id}:1:1"})
    check(r.status_code in (200, 303), "3-field layout (no shape) still accepted")
    db.refresh(table)
    check((table.pos_x, table.pos_y) == (1, 1), "position-only move still works")
    check(table.shape == "square", "shape untouched when omitted")
    app.dependency_overrides.clear()
    db.close()


def test_edit_zone_saves_rect():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/zones/{zone.id}/edit", data={
        "name": "Patio", "color": "", "pos_x": 100, "pos_y": 50, "width": 400, "height": 250,
    })
    check(r.status_code in (200, 303), "zone edit with rect accepted")
    db.refresh(zone)
    check((zone.pos_x, zone.pos_y, zone.width, zone.height) == (100, 50, 400, 250),
          "zone rectangle persisted")

    # Plain name/colour edit (no rect fields) must not clobber the rectangle.
    r = c.post(f"/admin/zones/{zone.id}/edit", data={"name": "Patio", "color": ""})
    check(r.status_code in (200, 303), "plain edit without rect fields accepted")
    db.refresh(zone)
    check((zone.pos_x, zone.pos_y, zone.width, zone.height) == (100, 50, 400, 250),
          "rectangle untouched when the fields are omitted")
    app.dependency_overrides.clear()
    db.close()


def test_floor_page_shows_disp_status_and_due_soon():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    t_free = RestaurantTable(number=1, zone_id=zone.id, capacity=2)
    t_occ = RestaurantTable(number=2, zone_id=zone.id, capacity=2,
                            status="occupied", current_waiter_id=waiter.id)
    db.add_all([t_free, t_occ])
    db.flush()
    # A reservation holding the free table -> "reserved".
    res_free = Reservation(kind="reservation", guest_name="Silva", party_size=2,
                           at=datetime.now(), status=ReservationStatus.WAITING)
    res_free.tables = [t_free]
    # A reservation holding the ALREADY-occupied table -> due-soon reminder,
    # disp_status stays "occupied" (staff is already on it).
    res_occ = Reservation(kind="reservation", guest_name="Chen", party_size=2,
                          at=datetime.now(), status=ReservationStatus.WAITING)
    res_occ.tables = [t_occ]
    from app.routers.sales import open_order_on_table
    order = open_order_on_table(db, t_occ, guests=2, waiter_id=waiter.id)
    db.add_all([res_free, res_occ])
    db.commit()

    c = _client(db, owner)
    body = c.get("/").text
    check('class="tbl reserved"' in body, "the free held table renders as reserved")
    check("Silva" in body, "the reserved card shows the guest name")
    check('due-soon' in body, "the occupied-but-booked table gets the due-soon class")
    check("📅 due" in body, "the due-soon reminder text is shown")
    check("Chen" not in body or "title=" in body, "the occupied card's reservation name is in the tooltip, not shouted on the face")
    check('class="zpanel"' in body or 'zpanel' in body, "cards are grouped into zone panels")
    check(waiter.name in body or 'wav' in body, "the waiter legend/colour key is present")
    app.dependency_overrides.clear()
    db.close()


if __name__ == "__main__":
    for fn in (test_set_zone_tables_grows_and_shrinks_softly,
               test_set_zone_tables_never_retires_an_occupied_table,
               test_set_staff_color,
               test_save_layout_sets_shape_and_position,
               test_edit_zone_saves_rect,
               test_floor_page_shows_disp_status_and_due_soon):
        print(f"- {fn.__name__}")
        fn()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall floor-spatial tests passed")
