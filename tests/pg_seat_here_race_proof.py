"""Real-Postgres concurrency proof: seat_reservation_here() racing a
concurrent walk-in open_order_on_table() on the SAME held table.

This is the scenario the identity-map bug actually broke: reservation.tables
loaded RestaurantTable into the session's identity map before the lock, so
db.get() after the lock returned the stale (pre-race) status and seat-here
proceeded onto an already-occupied table. seat_reservation_here() now reads
the held ids off the reservation_table association table (never touching the
ORM-mapped RestaurantTable before the lock) and re-reads with
populate_existing=True after locking. SQLite's global write lock hides this
race entirely, so it needs a real Postgres.

SKIPs (exit 0) unless PG_TEST_DSN points at a disposable Postgres, matching
tests/test_pg_concurrency.py's convention.

Run: PG_TEST_DSN=postgresql+psycopg://user:pass@host:5433/db python tests/pg_seat_here_race_proof.py
"""
import os
import sys
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _env  # noqa: F401  — declares the test opt-out before app imports
from tests._pay_fixture import Session, fresh_schema, make_engine, pg_dsn

from app.models.oltp import Channel, Order, Reservation, ReservationStatus, RestaurantTable, Staff
from app.routers.reservations import seat_reservation_here
from app.routers.sales import open_order_on_table

if not pg_dsn():
    print("SKIP: PG_TEST_DSN not set (seat-here race proof is Postgres-specific)")
    sys.exit(0)

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def test_seat_here_races_a_concurrent_walkin():
    engine, _ = make_engine()
    fresh_schema(engine)
    Sess = Session(engine)

    seed = Sess()
    staff = Staff(name="Host", role="owner", pin_code="pbkdf2_sha256$1$x$y")
    channel = Channel(code="dine_in", name="Dine-in", channel_type="dine_in")
    seed.add_all([staff, channel])
    seed.flush()
    table = RestaurantTable(number=1, capacity=4)
    seed.add(table)
    seed.flush()
    reservation = Reservation(kind="reservation", guest_name="Silva", party_size=2,
                              at=datetime.now(), status=ReservationStatus.WAITING)
    reservation.tables = [table]
    seed.add(reservation)
    seed.commit()
    staff_id, table_id, res_id = staff.id, table.id, reservation.id
    seed.close()

    barrier = threading.Barrier(2)
    out = [None, None]

    def do_seat_here():
        barrier.wait()
        db = Sess()
        try:
            staff_row = db.get(Staff, staff_id)
            seat_reservation_here(res_id=res_id, db=db, staff=staff_row)
            out[0] = ("ok", None)
        except Exception as exc:  # noqa: BLE001
            out[0] = ("err", exc)
        finally:
            db.close()

    def do_walkin_open():
        barrier.wait()
        db = Sess()
        try:
            t = db.get(RestaurantTable, table_id)
            order = open_order_on_table(db, t, guests=2, waiter_id=staff_id)
            db.commit()
            out[1] = ("ok", order.id)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            out[1] = ("err", exc)
        finally:
            db.close()

    threads = [threading.Thread(target=do_seat_here), threading.Thread(target=do_walkin_open)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    verify = Sess()
    orders_on_table = verify.query(Order).filter(Order.table_id == table_id).count()
    verify.close()

    seat_result, walkin_result = out
    oks = [r for r in out if r[0] == "ok"]
    check(len(oks) == 1,
          f"exactly one of {{seat-here, concurrent walk-in}} wins the table, "
          f"the other is rejected (seat-here={seat_result}, walkin={walkin_result})")
    check(orders_on_table == 1, f"exactly one order lands on the table (got {orders_on_table})")


if __name__ == "__main__":
    test_seat_here_races_a_concurrent_walkin()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall seat-here race checks passed")
