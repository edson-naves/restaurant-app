"""Real-Postgres concurrency proof: two overlapping bookings racing the same
table through add_reservation().

Companion to pg_table_open_race_proof.py. Where that one proves the
pre-existing, still-unfixed open_order_on_table() race, this one proves the
NEW fix: add_reservation() now locks the requested tables (SELECT ... FOR
UPDATE) before running calculate_availability(), so a second concurrent
request for the same table/time blocks until the first commits, then sees the
first reservation and is correctly rejected. SQLite's global write lock would
hide this race entirely, so it needs a real Postgres.

SKIPs (exit 0) unless PG_TEST_DSN points at a disposable Postgres, matching
tests/test_pg_concurrency.py's convention.

Run: PG_TEST_DSN=postgresql+psycopg://user:pass@host:5433/db python tests/pg_reservation_overlap_race_proof.py
"""
import os
import sys
import threading
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _env  # noqa: F401  — declares the test opt-out before app imports
from tests._pay_fixture import Session, fresh_schema, make_engine, pg_dsn

from app.models.oltp import Reservation, ReservationStatus, RestaurantTable, Staff
from app.routers.reservations import add_reservation

if not pg_dsn():
    print("SKIP: PG_TEST_DSN not set (reservation overlap race proof is Postgres-specific)")
    sys.exit(0)

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _run_concurrently(fn, n):
    out = [None] * n
    barrier = threading.Barrier(n)

    def wrap(i):
        barrier.wait()
        try:
            out[i] = ("ok", fn(i))
        except Exception as exc:  # noqa: BLE001
            out[i] = ("err", exc)

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_two_overlapping_bookings_race_the_same_table():
    engine, _ = make_engine()
    fresh_schema(engine)
    Sess = Session(engine)

    seed = Sess()
    staff = Staff(name="Host", role="owner", pin_code="pbkdf2_sha256$1$x$y")
    seed.add(staff)
    seed.flush()
    table = RestaurantTable(number=1, capacity=4)
    seed.add(table)
    seed.commit()
    staff_id, table_id = staff.id, table.id
    seed.close()

    when = datetime(2030, 1, 1, 19, 0).isoformat()

    def book(i):
        db = Sess()
        try:
            return add_reservation(
                guest_name=f"Party {i}", party_size=2, date="", time="",
                at=when, phone="", notes="", table_pref="",
                table_ids=[table_id], db=db, staff=staff,
            )
        finally:
            db.close()

    results = _run_concurrently(book, 2)
    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]

    verify = Sess()
    waiting = verify.query(Reservation).filter(
        Reservation.status == ReservationStatus.WAITING
    ).count()
    verify.close()

    check(len(oks) == 1 and len(errs) == 1,
          f"exactly one of two concurrent overlapping bookings on the same "
          f"table succeeds, the other is rejected (got {len(oks)} ok, "
          f"{len(errs)} rejected)")
    check(waiting == 1, f"exactly one WAITING reservation persisted (got {waiting})")


if __name__ == "__main__":
    test_two_overlapping_bookings_race_the_same_table()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall reservation-overlap race checks passed")
