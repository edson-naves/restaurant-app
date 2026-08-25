"""Kitchen Stations B2.2 — PostgreSQL concurrency PROOF (standalone; NOT auto-run).

Proves the approved B2 v3 §4/§4.1 concurrency contract for sibling PreparationTask
READY under REAL PostgreSQL row locking. It reproduces the exact lock+refresh+rollup
sequence of the `task_ready` route (app/routers/sales.py) — parent Order
`SELECT ... FOR UPDATE`, post-lock reselect/refresh, `rollup_item_kitchen_status`,
`_recompute_kitchen` — across two independent sessions/transactions, using a
deliberate hold window in session A so session B must CONTEND on the same Order lock.

This is a proof script, not implementation code. It changes no B2.2 code.

REQUIREMENTS (fails closed if not met):
  * DATABASE_URL must be a PostgreSQL DSN, e.g.
      export DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/restaurant_scratch
  * psycopg (v3) installed (already in this venv).
  * A scratch/staging database you are OK creating tables in (create_all is additive).

RUN:
    DATABASE_URL=postgresql+psycopg://... .venv/Scripts/python.exe tests/pg_b2_2_concurrency_proof.py

It will NOT run on SQLite and does NOT simulate FOR UPDATE.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from datetime import datetime

DSN = os.environ.get("DATABASE_URL", "")
if not DSN.startswith(("postgresql", "postgres")):
    print(f"REFUSING TO RUN: DATABASE_URL must be a PostgreSQL DSN (got: {DSN or '<unset>'}).")
    print("This proof is PostgreSQL-only and must not use SQLite.")
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.oltp import (
    Channel, KitchenStatus, MenuCategory, MenuItem, Order, OrderItem, OrderStatus,
    PreparationTask, PreparationTaskStatus, Station,
)
from app.routers.sales import _recompute_kitchen, rollup_item_kitchen_status

HOLD_SECONDS = 2.0                     # how long session A holds the Order lock
engine = create_engine(DSN, future=True)
Base.metadata.create_all(engine)      # additive; creates missing tables only
Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)


def _seed() -> tuple[int, int, list[int]]:
    """One on-board OrderItem with two sibling PreparationTasks in PREPARING."""
    db = Session()
    now = datetime.now()
    ch = db.execute(select(Channel).where(Channel.code == "dine_in")).scalars().first()
    if ch is None:
        ch = Channel(code="dine_in", name="Dine-in", channel_type="dine_in"); db.add(ch)
    cat = MenuCategory(name=f"Mains-{uuid.uuid4().hex[:6]}"); db.add(cat); db.flush()
    mi = MenuItem(category_id=cat.id, name=f"Burger-{uuid.uuid4().hex[:6]}", price_cents=1500)
    db.add(mi); db.flush()
    g = Station(name=f"Grill-{uuid.uuid4().hex[:6]}", type="production", is_active=True)
    f = Station(name=f"Fryer-{uuid.uuid4().hex[:6]}", type="production", is_active=True)
    db.add_all([g, f]); db.flush()
    o = Order(code=f"PG-{uuid.uuid4().hex[:8]}", channel_id=ch.id, status=OrderStatus.PREPARING,
              guest_count=2, opened_at=now, sent_to_kitchen_at=now,
              kitchen_status=KitchenStatus.PREPARING)
    db.add(o); db.flush()
    oi = OrderItem(order_id=o.id, menu_item_id=mi.id, quantity=1, unit_price_cents=1500,
                   kitchen_status=KitchenStatus.PREPARING, course=2, station_id=g.id)
    db.add(oi); db.flush()
    ta = PreparationTask(order_item_id=oi.id, order_id=o.id, station_id=g.id, course=2,
                         kitchen_status=PreparationTaskStatus.PREPARING, quantity=1,
                         item_label=mi.name, is_base=True, fire_seq=1, fired_at=now)
    tb = PreparationTask(order_item_id=oi.id, order_id=o.id, station_id=f.id, course=2,
                         kitchen_status=PreparationTaskStatus.PREPARING, quantity=1,
                         item_label=mi.name, is_base=False, fire_seq=1, fired_at=now)
    db.add_all([ta, tb]); db.commit()
    ids = (o.id, oi.id, [ta.id, tb.id])
    db.close()
    return ids


def _ready_like_route(db, task_id: int, order_id: int, hold: float = 0.0):
    """The task_ready route's exact sequence: lock the Order FOR UPDATE, then reselect
    the task + siblings from committed state, transition, rollup, recompute, commit.
    `hold` sleeps WHILE holding the lock (session A) to force contention on B."""
    # 1. parent Order FOR UPDATE (this blocks a concurrent locker until we commit)
    db.execute(select(Order.id).where(Order.id == order_id).with_for_update()).first()
    if hold:
        time.sleep(hold)
    # 2. post-lock reselect of the task + siblings (fresh committed state)
    task = db.execute(
        select(PreparationTask).where(PreparationTask.id == task_id)
        .execution_options(populate_existing=True)
    ).scalars().first()
    item = task.order_item
    db.refresh(item, ["tasks"])
    # 3. transition (PREPARING -> READY; idempotent READY preserved)
    now = datetime.now()
    if task.kitchen_status == PreparationTaskStatus.PREPARING:
        task.kitchen_status = PreparationTaskStatus.READY
        task.ready_at = task.ready_at or now
    # 4. rollup + recompute + commit
    rollup_item_kitchen_status(item)
    _recompute_kitchen(item.order, now)
    db.commit()


def main() -> int:
    server_version = None
    with engine.connect() as c:
        server_version = c.execute(text("SHOW server_version")).scalar_one()
    print(f"PostgreSQL server_version = {server_version}")

    order_id, oi_id, (task_a, task_b) = _seed()
    print(f"seeded order={order_id} item={oi_id} tasks={task_a},{task_b} (both PREPARING)")

    a_locked = threading.Event()
    results: dict = {}

    def run_a():
        db = Session()
        try:
            db.execute(select(Order.id).where(Order.id == order_id).with_for_update()).first()
            a_locked.set()                      # A now holds the lock
            time.sleep(HOLD_SECONDS)            # hold it so B must wait
            task = db.execute(
                select(PreparationTask).where(PreparationTask.id == task_a)
                .execution_options(populate_existing=True)).scalars().first()
            item = task.order_item
            db.refresh(item, ["tasks"])
            now = datetime.now()
            task.kitchen_status = PreparationTaskStatus.READY
            task.ready_at = task.ready_at or now
            rollup_item_kitchen_status(item)
            _recompute_kitchen(item.order, now)
            db.commit()
            results["a_committed_at"] = time.monotonic()
        except Exception as exc:                # noqa: BLE001
            results["a_error"] = repr(exc)
        finally:
            db.close()

    def run_b():
        a_locked.wait(timeout=10)
        time.sleep(0.3)                         # ensure A holds the lock first
        db = Session()
        try:
            t0 = time.monotonic()
            db.execute(select(Order.id).where(Order.id == order_id).with_for_update()).first()
            t1 = time.monotonic()
            results["b_wait_seconds"] = t1 - t0     # ~ HOLD_SECONDS if B truly waited
            # post-lock refresh: B must now observe A's committed READY on task A
            db.expire_all()
            observed = db.get(PreparationTask, task_a).kitchen_status
            results["b_observed_task_a"] = observed
            task = db.execute(
                select(PreparationTask).where(PreparationTask.id == task_b)
                .execution_options(populate_existing=True)).scalars().first()
            item = task.order_item
            db.refresh(item, ["tasks"])
            now = datetime.now()
            task.kitchen_status = PreparationTaskStatus.READY
            task.ready_at = task.ready_at or now
            rollup_item_kitchen_status(item)
            _recompute_kitchen(item.order, now)
            db.commit()
        except Exception as exc:                # noqa: BLE001
            results["b_error"] = repr(exc)
        finally:
            db.close()

    def monitor():
        # While A holds and B contends, capture B waiting on a lock in the catalog.
        a_locked.wait(timeout=10)
        time.sleep(HOLD_SECONDS * 0.5)          # mid-hold, B should be blocked
        db = Session()
        try:
            rows = db.execute(text(
                "SELECT wait_event_type, wait_event, LEFT(query, 60) AS q "
                "FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND query ILIKE '%for update%'"
            )).all()
            results["lock_waiters"] = [tuple(r) for r in rows]
        except Exception as exc:                # noqa: BLE001
            results["monitor_error"] = repr(exc)
        finally:
            db.close()

    ta = threading.Thread(target=run_a)
    tb = threading.Thread(target=run_b)
    tm = threading.Thread(target=monitor)
    ta.start(); tb.start(); tm.start()
    ta.join(); tb.join(); tm.join()

    # Final persisted state (fresh session)
    v = Session()
    A = v.get(PreparationTask, task_a); B = v.get(PreparationTask, task_b)
    item = v.get(OrderItem, oi_id); order = v.get(Order, order_id)
    state = {
        "task_a": A.kitchen_status, "task_a_ready_at": A.ready_at,
        "task_b": B.kitchen_status, "task_b_ready_at": B.ready_at,
        "order_item": item.kitchen_status, "order_status": order.status,
    }
    v.close()

    print("\n--- evidence ---")
    print("A error:", results.get("a_error"))
    print("B error:", results.get("b_error"))
    print(f"B wait on Order lock: {results.get('b_wait_seconds')!r} s "
          f"(hold was {HOLD_SECONDS}s; ~hold ⇒ B serialized behind A)")
    print("B observed task_a after lock:", results.get("b_observed_task_a"))
    print("catalog lock-waiters mid-hold:", results.get("lock_waiters"))
    print("final state:", state)

    waited = (results.get("b_wait_seconds") or 0) >= HOLD_SECONDS * 0.7
    observed_ready = results.get("b_observed_task_a") == PreparationTaskStatus.READY
    ok = (
        not results.get("a_error") and not results.get("b_error")
        and waited and observed_ready
        and state["task_a"] == PreparationTaskStatus.READY
        and state["task_b"] == PreparationTaskStatus.READY
        and state["task_a_ready_at"] is not None and state["task_b_ready_at"] is not None
        and state["order_item"] == KitchenStatus.READY
        and state["order_status"] != OrderStatus.SERVED
    )
    print("\nRESULT:", "PASS" if ok else "FAIL")
    if not waited:
        print("  ! B did not measurably wait on the Order lock — contention NOT proven.")
    if not observed_ready:
        print("  ! B did not observe A's committed READY after acquiring the lock.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
