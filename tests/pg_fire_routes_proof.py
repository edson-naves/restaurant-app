"""Kitchen fire routes — PostgreSQL PROOF (standalone; NOT auto-run).

Covers the defect class the SQLite suites cannot detect by construction: SQLite
treats `FOR UPDATE` as a no-op, so a lock statement Postgres rejects still passes
every SQLite test. `send_to_kitchen` and `fire_item` locked with `select(Order)`,
whose eager relationships become LEFT OUTER JOINs — and Postgres refuses
`FOR UPDATE cannot be applied to the nullable side of an outer join`, so both
routes returned HTTP 500 on the engine production actually runs.

Proves, against REAL PostgreSQL:
  1. POST /orders/{id}/send does not 500 and fires the held lines;
  2. POST /orders/{id}/items/{iid}/fire does not 500 and fires one line;
  3. routing is snapshotted at fire, Unassigned stays NULL;
  4. every Order lock in this module is taken on the scalar id, never the entity;
  5. two concurrent fires of the SAME line decide on POST-LOCK state: exactly one
     wins, the loser is rejected from the revalidated precondition, and no
     duplicate PreparationTask is created.

REQUIREMENTS (fails closed if not met):
  * DATABASE_URL must be a PostgreSQL DSN pointing at a scratch database;
  * that database must already be bootstrapped (python -m app.bootstrap).

RUN:
    DATABASE_URL=postgresql+psycopg://... python tests/pg_fire_routes_proof.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException                       # noqa: E402
from fastapi.testclient import TestClient               # noqa: E402
from sqlalchemy import event, select, text        # noqa: E402

from app.database import SessionLocal, engine           # noqa: E402
from app.main import app                                # noqa: E402
from app.deps import current_staff                      # noqa: E402
from app.database import get_db                         # noqa: E402
from app.models.oltp import (                           # noqa: E402
    KitchenStatus, MenuItem, Order, OrderItem, OrderStatus,
    PreparationTask, Role, RestaurantTable, Staff, Station, TableStatus,
)
from app.routers import sales                           # noqa: E402

ok = True


def check(cond, label, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  -> ' + str(detail)) if detail else ''}")


if engine.dialect.name != "postgresql":
    sys.exit("REFUSING: DATABASE_URL is not PostgreSQL — this proof is meaningless on SQLite")

db = SessionLocal()
print("engine =", engine.dialect.name, "| database =", engine.url.database)

owner = db.execute(select(Staff).where(Staff.role == Role.OWNER)).scalars().first()
app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[current_staff] = lambda: owner
client = TestClient(app)


def _fresh_order(label: str, n_items: int = 2):
    """Seat a free table, open an order, and park n_items on it, all PENDING."""
    tbl = db.execute(
        select(RestaurantTable)
        .where(RestaurantTable.status == TableStatus.FREE)
        .order_by(RestaurantTable.id)
    ).scalars().first()
    assert tbl is not None, "no free table left in the scratch database"
    client.post(f"/tables/{tbl.id}/open", data={"guests": 2}, follow_redirects=False)
    db.expire_all()
    order = db.execute(
        select(Order).where(Order.table_id == tbl.id, Order.status != OrderStatus.PAID)
        .order_by(Order.id.desc())
    ).scalars().first()
    items = db.execute(select(MenuItem).order_by(MenuItem.id).limit(n_items)).scalars().all()
    for mi in items:
        client.post(f"/orders/{order.id}/items",
                    data={"menu_item_id": mi.id, "quantity": 1}, follow_redirects=False)
    db.expire_all()
    print(f"  seeded {label}: order={order.code} items={n_items}")
    return db.get(Order, order.id)


# ---------------------------------------------------------------- lock shape
src = Path(sales.__file__).read_text(encoding="utf-8")
entity_locks = [ln for ln in src.splitlines()
                if "with_for_update" in ln and "select(Order.id)" not in ln]
check(not entity_locks,
      "every Order lock in sales.py is taken on the scalar id, not the entity",
      entity_locks or "0 entity locks")

# ------------------------------------------------------- 1. /send does not 500
order = _fresh_order("send", 2)
routed = db.execute(select(Station).where(Station.type == "production")).scalars().first()
if routed is None:
    client.post("/admin/stations/create",
                data={"name": "PGFire Grill", "type": "production", "color": "#ef4444", "icon": "G"},
                follow_redirects=False)
    db.expire_all()
    routed = db.execute(select(Station)).scalars().first()
first_item = order.items[0]
first_item.menu_item.station_id = routed.id      # route one line, leave the other Unassigned
db.commit()
db.expire_all()

r = client.post(f"/orders/{order.id}/send", follow_redirects=False)
check(r.status_code != 500, "POST /orders/{id}/send does NOT return 500 on PostgreSQL",
      r.status_code)
check(r.status_code == 303, "send redirects (303) as before", r.status_code)
db.expire_all()
order = db.get(Order, order.id)
check(all(i.kitchen_status == KitchenStatus.PREPARING for i in order.items),
      "every held line moved to PREPARING", [i.kitchen_status for i in order.items])
snaps = {i.id: i.station_id for i in order.items}
check(snaps.get(first_item.id) == routed.id, "routed line snapshotted its station at fire",
      snaps.get(first_item.id))
check(any(v is None for v in snaps.values()), "unrouted line snapshotted Unassigned (NULL)",
      snaps)
check(all(len(i.tasks) >= 1 for i in order.items), "each fired line has at least one task",
      [len(i.tasks) for i in order.items])

# idempotency: nothing left to fire
r2 = client.post(f"/orders/{order.id}/send", follow_redirects=False)
check(r2.status_code == 400, "re-firing an already-fired order is rejected (400)", r2.status_code)

# ------------------------------------------------- 2. /fire (single) no 500
order2 = _fresh_order("fire", 2)
# Pin the ids BEFORE firing: the collection is re-loaded after expire_all() and
# its order is not guaranteed, so indexing it afterwards can hand back the wrong
# line and make a passing app look broken (it did, on the first run of this test).
target_id, other_id = sorted(i.id for i in order2.items)
r = client.post(f"/orders/{order2.id}/items/{target_id}/fire", follow_redirects=False)
check(r.status_code != 500, "POST /orders/{id}/items/{iid}/fire does NOT return 500", r.status_code)
check(r.status_code == 303, "single fire redirects (303) as before", r.status_code)
db.expire_all()
target = db.get(OrderItem, target_id)
other = db.get(OrderItem, other_id)
check(target.kitchen_status == KitchenStatus.PREPARING, "the fired line is PREPARING",
      target.kitchen_status)
check(other.kitchen_status == KitchenStatus.PENDING,
      "its course-mate is untouched (coursing preserved)",
      f"target={target_id}:{target.kitchen_status} other={other_id}:{other.kitchen_status}")
n_tasks_before = len(target.tasks)
check(n_tasks_before >= 1, "single fire created its task(s)", n_tasks_before)

r = client.post(f"/orders/{order2.id}/items/{target_id}/fire", follow_redirects=False)
check(r.status_code == 400, "re-firing the same line is rejected (400)", r.status_code)
db.expire_all()
check(len(db.get(OrderItem, target_id).tasks) == n_tasks_before,
      "the rejected re-fire created no extra task", len(db.get(OrderItem, target_id).tasks))

# ------------------------------- 3. concurrent fire of the SAME line, post-lock
# Two independent sessions call the real route function on the same PENDING line.
# Correct behaviour: whoever takes the row lock first fires it; the loser, having
# revalidated AFTER the lock, sees PREPARING and is rejected — it must not fall
# through to create a second task set. Under the old pre-lock validation both
# would have passed the PENDING check before contending.
order3 = _fresh_order("race", 1)
race_item_id = order3.items[0].id
db.commit()

# Barring thread START proves nothing: one session can take the lock, commit and
# finish before the other even issues its SELECT ... FOR UPDATE, and a
# fired/400 pair would then be indistinguishable from sequential execution. So
# the barrier is moved onto the lock statement itself, via before_cursor_execute:
# each session blocks at the instant it is about to emit the Order FOR UPDATE.
# Both then release into the lock together, and whoever wins holds the row for a
# deliberate HOLD window (from the test's after_cursor_execute hook — production
# code is untouched) so the loser demonstrably WAITS on the database.
HOLD = 2.0
lock_gate = threading.Barrier(2, timeout=45)
winner_mu = threading.Lock()
winner = {"tag": None}
results: dict[str, dict] = {}
observed_waiters: list = []


def _is_order_lock(statement: str) -> bool:
    s = " ".join(statement.split()).upper()
    return "FOR UPDATE" in s and 'FROM "ORDER"' in s


def racer(tag):
    info = {"status": None, "detail": None, "error": None,
            "reached_lock": False, "lock_wait": None, "held": False}
    s = SessionLocal()
    try:
        raw = s.connection()          # materialise the connection so we can hook it

        @event.listens_for(raw, "before_cursor_execute")
        def _before(conn, cursor, statement, parameters, context, executemany):
            if _is_order_lock(statement):
                info["reached_lock"] = True
                lock_gate.wait()                       # both are AT the lock now
                info["_t0"] = time.perf_counter()

        @event.listens_for(raw, "after_cursor_execute")
        def _after(conn, cursor, statement, parameters, context, executemany):
            if _is_order_lock(statement):
                info["lock_wait"] = time.perf_counter() - info["_t0"]
                with winner_mu:
                    first = winner["tag"] is None
                    if first:
                        winner["tag"] = tag
                if first:                              # hold the row so the loser waits
                    info["held"] = True
                    time.sleep(HOLD)

        sales.fire_item(order_id=order3.id, item_id=race_item_id, db=s, staff=owner)
        info["status"] = "fired"
    except HTTPException as exc:
        info["status"] = f"rejected {exc.status_code}"
        info["detail"] = exc.detail
    except Exception as exc:                     # noqa: BLE001 — recorded, not hidden
        info["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        info.pop("_t0", None)
        s.close()
    results[tag] = info


def observer():
    """Third session: catch the lock waiter in the catalog while it waits.

    Each sample runs in its own short-lived session and commits, so the poll can
    never sit inside one long-lived transaction while it watches.
    """
    end = time.perf_counter() + HOLD + 10
    while time.perf_counter() < end:
        s = SessionLocal()
        try:
            rows = s.execute(text(
                "SELECT wait_event_type, wait_event, LEFT(query, 60) AS q "
                "FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND query ILIKE '%for update%'"
            )).all()
            if rows:
                observed_waiters.extend(tuple(r) for r in rows)
                return
        except Exception as exc:                 # noqa: BLE001 — recorded, not hidden
            observed_waiters.append(("observer_error", repr(exc), ""))
            return
        finally:
            s.close()
        time.sleep(0.05)


ts = [threading.Thread(target=racer, args=(f"S{i}",)) for i in (1, 2)]
obs = threading.Thread(target=observer)
obs.start()
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=90)
obs.join(timeout=20)

for tag in ("S1", "S2"):
    print(f"  {tag}: {results.get(tag)}")
print(f"  winner={winner['tag']} hold={HOLD}s  catalog waiters={observed_waiters or 'none'}")

still_alive = [t.name for t in ts if t.is_alive()]
check(not still_alive, "both racer threads finished (no result accepted from a live thread)",
      still_alive or "none alive")

both_at_lock = all(results.get(t, {}).get("reached_lock") for t in ("S1", "S2"))
check(both_at_lock, "BOTH sessions reached the Order SELECT ... FOR UPDATE",
      {t: results.get(t, {}).get("reached_lock") for t in ("S1", "S2")})

waits = {t: results.get(t, {}).get("lock_wait") for t in ("S1", "S2")}
loser_tag = next((t for t in ("S1", "S2") if t != winner["tag"]), None)
loser_wait = waits.get(loser_tag)
check(loser_wait is not None and loser_wait >= HOLD * 0.8,
      f"the loser WAITED on the winner's row lock (>= {HOLD * 0.8:.1f}s)",
      f"waits={waits} winner={winner['tag']}")
check(bool(observed_waiters),
      "a lock waiter was visible in pg_stat_activity during the race",
      observed_waiters or "none")

outcomes = sorted((results.get(t, {}).get("status") or "missing") for t in ("S1", "S2"))
errs = [f"{t}: {i['error']}" for t, i in results.items() if i.get("error")]
check(not errs, "neither concurrent fire raised an unexpected error", errs or "none")
check(outcomes == ["fired", "rejected 400"],
      "exactly one fire won; the loser was rejected from POST-LOCK revalidation",
      outcomes)
check(results.get(loser_tag, {}).get("detail") == "That item has already been fired.",
      "the loser's rejection came from the revalidated PENDING precondition",
      results.get(loser_tag, {}).get("detail"))

db.expire_all()
n_tasks = db.execute(
    select(PreparationTask).where(PreparationTask.order_item_id == race_item_id)
).scalars().all()
batches = {(t.order_item_id, t.fire_seq, t.station_id) for t in n_tasks}
check(len(n_tasks) == len(batches),
      "no duplicate PreparationTask batch was created by the race",
      f"{len(n_tasks)} tasks / {len(batches)} distinct batches")
check(db.get(OrderItem, race_item_id).kitchen_status == KitchenStatus.PREPARING,
      "the contended line ends PREPARING exactly once",
      db.get(OrderItem, race_item_id).kitchen_status)

app.dependency_overrides.clear()
print("\nRESULT: " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
