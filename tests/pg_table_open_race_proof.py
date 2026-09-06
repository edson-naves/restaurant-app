"""Real-Postgres concurrency proof: two simultaneous open_order_on_table()
calls on the SAME table.

Not part of Floor/Reservations' own diff â€” open_order_on_table() (app/routers/
sales.py) is pre-existing code from 489f6d2 that both the floor "open table"
action and the new seat_reservation_here() endpoint call. It does a plain
SELECT/UPDATE on table.status, with no SELECT ... FOR UPDATE and no unique
constraint on (table_id, status='open') at the schema level (confirmed by
reading app/models/oltp.py: Order.table_id carries no such constraint, only a
plain index). SQLite's global write lock hides this race entirely, so this
needs a real Postgres to observe.

SKIPs (exit 0) unless PG_TEST_DSN points at a disposable Postgres, matching
tests/test_pg_concurrency.py's convention. Uses the same drop_all/create_all
throwaway-schema helper as the payment concurrency proofs (tests/_pay_fixture),
so it is safe only against a database that convention already treats as
disposable.

Run: PG_TEST_DSN=postgresql+psycopg://user:pass@host:5433/db python tests/pg_table_open_race_proof.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _env  # noqa: F401  â€” declares the test opt-out before app imports
from tests._pay_fixture import Session, fresh_schema, make_engine, pg_dsn

from app.models.oltp import Channel, Order, RestaurantTable, Staff, TableStatus
from app.routers.sales import open_order_on_table

if not pg_dsn():
    print("SKIP: PG_TEST_DSN not set (table-open race proof is Postgres-specific)")
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


def test_two_sessions_racing_the_same_table():
    engine, _ = make_engine()
    fresh_schema(engine)
    Sess = Session(engine)

    seed = Sess()
    staff = Staff(name="Waiter A", role="waiter", pin_code="pbkdf2_sha256$1$x$y")
    ch = Channel(code="dine_in", name="Dine-in", channel_type="dine_in")
    seed.add_all([staff, ch])
    seed.flush()
    table = RestaurantTable(number=1, capacity=4, status=TableStatus.FREE)
    seed.add(table)
    seed.commit()
    staff_id, table_id = staff.id, table.id
    seed.close()

    def open_it(_i):
        db = Sess()
        try:
            t = db.get(RestaurantTable, table_id)
            order = open_order_on_table(db, t, guests=2, waiter_id=staff_id)
            db.commit()
            return order.id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    results = _run_concurrently(open_it, 2)
    ok_ids = [r[1] for r in results if r[0] == "ok"]
    errs = [r[1] for r in results if r[0] == "err"]

    verify = Sess()
    open_orders = verify.query(Order).filter(Order.table_id == table_id).count()
    verify.close()

    check(len(ok_ids) <= 1 or open_orders <= 1,
          f"two concurrent open_order_on_table() calls on the same table do not "
          f"both succeed (got {len(ok_ids)} successes, {open_orders} orders on "
          f"the table, {len(errs)} raised) â€” currently expected to FAIL: no "
          f"row lock and no DB constraint guard this path")


if __name__ == "__main__":
    test_two_sessions_racing_the_same_table()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall table-open race checks passed")
