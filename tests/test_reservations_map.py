"""Reservation floor-plan picker + table holds (feat/floor-reservations-cut).

Covers: booking with tables picked on the map (the reservation_table join), the
live floor plan showing a held table as "reserved", the hold window (not-yet /
active / auto-drop as no-show), seating a booking straight from the map, the
reservation page rendering the picker, and whether the availability engine
(calculate_availability) actually prevents a double-booking of the same table.
Throwaway SQLite + dependency overrides.
Run: python tests/test_reservations_map.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
    Order,
    Reservation,
    ReservationStatus,
    RestaurantTable,
    Staff,
    TableStatus,
    Zone,
)
from app.services import reservations as reservations_svc

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _session():
    path = os.path.join(tempfile.gettempdir(), f"resmap_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _seed(db, n_tables=2):
    owner = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
    floor = Floor(name=f"F{uuid.uuid4().hex[:4]}")
    ch = Channel(code="dine_in", name="Dine-in", channel_type="dine_in")
    db.add_all([owner, floor, ch])
    db.flush()
    zone = Zone(name="Main", floor_id=floor.id, pos_x=100, pos_y=100, width=500, height=400)
    db.add(zone)
    db.flush()
    tables = []
    for i in range(1, n_tables + 1):
        t = RestaurantTable(number=i, zone_id=zone.id, zone="Main", capacity=4,
                            pos_x=150 + i * 80, pos_y=200, shape="round")
        db.add(t)
        tables.append(t)
    db.commit()
    return owner, zone, tables


def _client(db, staff):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


def _booking(db, tables, at, party=4, status=ReservationStatus.WAITING):
    r = Reservation(kind="reservation", guest_name="Silva", party_size=party,
                    at=at, status=status)
    r.tables = tables
    db.add(r)
    db.commit()
    return r


def test_booking_holds_picked_tables():
    db = _session()
    owner, zone, tables = _seed(db, 2)
    c = _client(db, owner)
    c.post("/reservations", data={
        "guest_name": "Silva", "party_size": 6, "date": "2030-01-01", "time": "19:00",
        "table_ids": [tables[0].id, tables[1].id],
    }, follow_redirects=False)
    res = db.query(Reservation).filter(Reservation.guest_name == "Silva").one()
    check({t.id for t in res.tables} == {tables[0].id, tables[1].id},
          "a booking holds every table picked on the map (join written)")
    app.dependency_overrides.clear()
    db.close()


def test_held_table_reads_reserved_on_floor():
    db = _session()
    owner, zone, tables = _seed(db, 2)
    _booking(db, [tables[0]], at=datetime.now(), party=3)   # window open now
    c = _client(db, owner)
    body = c.get("/").text
    check('class="tbl reserved"' in body, "a held free table reads as reserved on the live floor")
    check("📅" in body, "the reserved calendar icon is shown")
    check("Silva" in body, "the booking's guest name is on the reserved card")
    check("seat-here" in body, "the reserved card offers a one-tap seat from the map")
    app.dependency_overrides.clear()
    db.close()


def test_hold_not_started_leaves_table_free():
    db = _session()
    owner, zone, tables = _seed(db, 1)
    _booking(db, [tables[0]], at=datetime.now() + timedelta(hours=3))   # well before window
    c = _client(db, owner)
    holds = reservations_svc.active_holds(db)
    check(tables[0].id not in holds, "a booking outside its hold window does not block the table")
    app.dependency_overrides.clear()
    db.close()


def test_no_show_auto_drops_and_releases():
    db = _session()
    owner, zone, tables = _seed(db, 1)
    r = _booking(db, [tables[0]], at=datetime.now() - timedelta(hours=2))
    r.created_at = r.at - timedelta(hours=1)
    db.commit()
    holds = reservations_svc.active_holds(db)
    check(tables[0].id not in holds, "a past-window booking no longer holds its table")
    db.refresh(r)
    check(r.status == ReservationStatus.NO_SHOW, "the un-seated advance booking auto-drops to no-show")
    db.close()


def test_same_day_past_booking_is_kept():
    db = _session()
    owner, zone, tables = _seed(db, 1)
    r = _booking(db, [tables[0]], at=datetime.now() - timedelta(minutes=40))
    holds = reservations_svc.active_holds(db)
    db.refresh(r)
    check(r.status == ReservationStatus.WAITING, "a just-made same-day booking is not auto-no-showed")
    check(tables[0].id in holds, "and it still holds its table, ready to be seated")
    db.close()


def test_seat_here_opens_orders_on_all_tables():
    db = _session()
    owner, zone, tables = _seed(db, 2)
    r = _booking(db, [tables[0], tables[1]], at=datetime.now(), party=5)
    c = _client(db, owner)
    resp = c.post(f"/reservations/{r.id}/seat-here", follow_redirects=False)
    check(resp.status_code == 303 and resp.headers["location"].startswith("/?floor="),
          "seating a booking stays on the floor plan")
    db.refresh(r)
    db.refresh(tables[0]); db.refresh(tables[1])
    check(r.status == ReservationStatus.SEATED, "the booking is marked seated")
    check(tables[0].status == TableStatus.OCCUPIED and tables[1].status == TableStatus.OCCUPIED,
          "every held table is now occupied")
    orders = db.query(Order).filter(Order.table_id.in_([tables[0].id, tables[1].id])).all()
    check(len(orders) == 2, "an order is opened on each held table")
    check(sorted(o.guest_count for o in orders) == [2, 3], "the party of 5 splits 3 + 2 across the tables")
    app.dependency_overrides.clear()
    db.close()


def test_reservation_page_renders_picker():
    db = _session()
    owner, zone, tables = _seed(db, 2)
    _booking(db, [tables[0]], at=datetime.now() + timedelta(days=1))
    c = _client(db, owner)
    body = c.get("/reservations").text
    check('id="reservation-picker"' in body, "the reservation page renders the floor-plan picker")
    check('class="restbl"' in body, "tables are click-to-select cards on the picker")
    check(f'data-table-id="{tables[0].id}"' in body, "each pickable table carries its id")
    check('title="Already has a booking"' in body,
          "a table already carrying a pending booking is marked booked")
    app.dependency_overrides.clear()
    db.close()


def test_double_booking_the_same_table_same_time():
    """The declared overlap engine (calculate_availability) exists in
    app/services/reservations.py, but add_reservation() never calls it — it
    writes whatever table_ids the form posts, unconditionally. This test
    proves whether that gap is real at the HTTP boundary, not just by reading
    the source: book the same table for two overlapping times and check
    whether the second booking is rejected or silently double-holds it.
    """
    db = _session()
    owner, zone, tables = _seed(db, 1)
    c = _client(db, owner)
    c.post("/reservations", data={
        "guest_name": "Party A", "party_size": 2, "date": "2030-01-01", "time": "19:00",
        "table_ids": [tables[0].id],
    }, follow_redirects=False)
    resp = c.post("/reservations", data={
        "guest_name": "Party B", "party_size": 2, "date": "2030-01-01", "time": "19:15",
        "table_ids": [tables[0].id],
    }, follow_redirects=False)
    both_waiting = db.query(Reservation).filter(
        Reservation.status == ReservationStatus.WAITING,
        Reservation.guest_name.in_(["Party A", "Party B"]),
    ).count()
    check(resp.status_code >= 400 or both_waiting < 2,
          "booking the same table for an overlapping time is rejected "
          "(calculate_availability is enforced at the booking endpoint)")
    app.dependency_overrides.clear()
    db.close()


if __name__ == "__main__":
    for fn in (test_booking_holds_picked_tables,
               test_held_table_reads_reserved_on_floor,
               test_hold_not_started_leaves_table_free,
               test_no_show_auto_drops_and_releases,
               test_same_day_past_booking_is_kept,
               test_seat_here_opens_orders_on_all_tables,
               test_reservation_page_renders_picker,
               test_double_booking_the_same_table_same_time):
        print(f"- {fn.__name__}")
        fn()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall reservation-map tests passed")
