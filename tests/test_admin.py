"""Setup & administration module (app/routers/admin.py).

Covers the rules that protect service history and the floor: nothing is
deleted, a table or waiter in the middle of service cannot be retired, the last
owner cannot lock themselves out, and prices typed as decimals land in the
database as exact cents.

Runs in-process against the ASGI app — no server needed:
    python tests/test_admin.py

It writes to the real database, so every row it creates is removed at the end.
The fixtures it makes (table 9001, "QA Probe") never touch an order, so they
are safe to hard-delete; production rows are not.
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.oltp import (  # noqa: E402
    MenuCategory,
    MenuItem,
    Order,
    OrderStatus,
    RestaurantTable,
    Role,
    Staff,
)

TEST_TABLE_NO = 9001
TEST_STAFF = "QA Probe"
TEST_ITEM = "QA Probe Plate"

ok = True


def check(cond, label, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  -> ' + detail) if detail else ''}")


db = SessionLocal()
owner = db.execute(select(Staff).where(Staff.role == Role.OWNER)).scalars().first()
waiter = db.execute(select(Staff).where(Staff.role == Role.WAITER)).scalars().first()
assert owner is not None, "no owner in the database — run: python -m app.seed"

# TestClient drives the ASGI app directly. httpx's own ASGITransport is
# async-only, so it cannot be used from a plain script like this one.
client = TestClient(app, follow_redirects=True)


def as_owner():
    client.cookies.set("staff_id", str(owner.id))


def fresh(model, **where):
    """Re-read a row, bypassing the identity map this session already holds."""
    db.expire_all()
    return db.execute(select(model).filter_by(**where)).scalars().first()


# ------------------------------------------------------------------ migration
print("--- schema ---")
check(
    hasattr(RestaurantTable, "is_active")
    and fresh(RestaurantTable, number=1) is not None,
    "restaurant_table.is_active exists on the live database",
)

# ------------------------------------------------------------------- rendering
print("\n--- pages render ---")
as_owner()
for path in ["/admin", "/admin/tables", "/admin/staff", "/admin/menu"]:
    r = client.get(path)
    check(r.status_code == 200, f"GET {path}", f"{r.status_code}, {len(r.text)}b")

# ------------------------------------------------------------------ permission
print("\n--- access control (section 3) ---")
if waiter is not None:
    client.cookies.set("staff_id", str(waiter.id))
    for path in ["/admin/tables", "/admin/staff", "/admin/menu"]:
        r = client.get(path)
        check(r.status_code == 403, f"waiter is refused {path}", str(r.status_code))
    r = client.post("/admin/tables/create", data={"number": 9999, "capacity": 2})
    check(r.status_code == 403, "waiter cannot create a table", str(r.status_code))
    check(
        fresh(RestaurantTable, number=9999) is None,
        "refused create wrote nothing",
    )
    r = client.get("/")
    check(
        "/admin/tables" not in r.text,
        "Manage tab is hidden from the waiter's nav",
    )
as_owner()

# ----------------------------------------------------------------------- tables
print("\n--- tables (4.1.1) ---")
r = client.post("/admin/tables/create", data={
    "number": TEST_TABLE_NO, "zone": "QA", "capacity": 4,
})
t = fresh(RestaurantTable, number=TEST_TABLE_NO)
check(r.status_code == 200 and t is not None, "create table", f"id={t.id if t else None}")
check(t is not None and t.is_active and t.status == "free", "new table is free and active")

r = client.post("/admin/tables/create", data={
    "number": TEST_TABLE_NO, "zone": "QA", "capacity": 4,
})
check(r.status_code == 400, "duplicate table number refused", str(r.status_code))

r = client.post("/admin/tables/create", data={"number": 9002, "capacity": 99})
check(r.status_code == 400, "capacity 99 refused", str(r.status_code))
check(fresh(RestaurantTable, number=9002) is None, "refused create wrote nothing")

r = client.post(f"/admin/tables/{t.id}/edit", data={
    "number": TEST_TABLE_NO, "zone": "Patio QA", "capacity": 6,
})
t = fresh(RestaurantTable, number=TEST_TABLE_NO)
check(t.zone == "Patio QA" and t.capacity == 6, "edit zone and capacity",
      f"{t.zone} / {t.capacity}")

# A new table must never be dropped onto an occupied square. Positions cannot
# be derived from the table count: the seeded floor is 8 wide, the editor grid
# is 10, so counting would collide.
def occupied_cells(exclude_id=None):
    db.expire_all()
    return {
        (x.pos_x, x.pos_y)
        for x in db.execute(
            select(RestaurantTable).where(RestaurantTable.is_active.is_(True))
        ).scalars().all()
        if x.id != exclude_id
    }


check((t.pos_x, t.pos_y) not in occupied_cells(exclude_id=t.id),
      "a newly created table lands on a free square", f"({t.pos_x},{t.pos_y})")

# Layout: move the test table to a square nothing else holds.
busy_cells = occupied_cells(exclude_id=t.id)
free_cell = next(
    (cx, cy) for cy in range(20) for cx in range(10) if (cx, cy) not in busy_cells
)
r = client.post("/admin/tables/layout", data={"layout": f"{t.id}:{free_cell[0]}:{free_cell[1]}"})
t = fresh(RestaurantTable, number=TEST_TABLE_NO)
check(r.status_code == 200 and (t.pos_x, t.pos_y) == free_cell,
      "layout saves grid position", f"({t.pos_x},{t.pos_y})")

other = db.execute(
    select(RestaurantTable).where(RestaurantTable.id != t.id).limit(1)
).scalars().first()
r = client.post("/admin/tables/layout", data={"layout": f"{t.id}:1:1,{other.id}:1:1"})
check(r.status_code == 400, "two tables on one square refused", str(r.status_code))
t = fresh(RestaurantTable, number=TEST_TABLE_NO)
check((t.pos_x, t.pos_y) == free_cell, "refused layout left positions untouched")

# The payload need not mention every table, so a partial one must not be able
# to park a table on a square someone else already holds.
squatted = next(iter(occupied_cells(exclude_id=t.id)))
r = client.post("/admin/tables/layout", data={
    "layout": f"{t.id}:{squatted[0]}:{squatted[1]}"
})
check(r.status_code == 400, "cannot move onto a table absent from the payload",
      str(r.status_code))
t = fresh(RestaurantTable, number=TEST_TABLE_NO)
check((t.pos_x, t.pos_y) == free_cell, "that refusal left the position untouched")

r = client.post("/admin/tables/layout", data={"layout": f"{t.id}:99:0"})
check(r.status_code == 400, "off-grid column refused", str(r.status_code))
r = client.post("/admin/tables/layout", data={"layout": "garbage"})
check(r.status_code == 400, "malformed layout refused", str(r.status_code))

# An occupied table must not be retirable — that is the rule protecting a live
# bill from vanishing off the floor plan mid-service.
busy = db.execute(
    select(Order).where(Order.status.in_((
        OrderStatus.OPEN, OrderStatus.PREPARING, OrderStatus.READY,
        OrderStatus.PARTIALLY_PAID,
    )), Order.table_id.is_not(None))
).scalars().first()
if busy is not None:
    r = client.post(f"/admin/tables/{busy.table_id}/active", data={"active": 0})
    check(r.status_code == 400, "table with a live order cannot be retired",
          str(r.status_code))
    check(fresh(RestaurantTable, id=busy.table_id).is_active,
          "that table is still active")
else:
    print("SKIP  no live order in the database to test the retire guard")

r = client.post(f"/admin/tables/{t.id}/active", data={"active": 0})
t = fresh(RestaurantTable, number=TEST_TABLE_NO)
check(not t.is_active, "free table retires")
check(fresh(RestaurantTable, number=TEST_TABLE_NO) is not None,
      "retired table still exists in the database (no delete)")

r = client.get("/")
check(f"Table {TEST_TABLE_NO}" not in r.text, "retired table is off the floor plan")
r = client.post(f"/admin/tables/{t.id}/active", data={"active": 1})
r = client.get("/")
check(f"Table {TEST_TABLE_NO}" in r.text, "restored table is back on the floor plan")

# ----------------------------------------------------------- batch create
print("\n--- add several tables at once ---")
before = db.execute(select(RestaurantTable)).scalars().all()
highest = max(t.number for t in before)

r = client.post("/admin/tables/create-batch", data={
    "count": 4, "capacity": 2, "zone": "Batch QA",
})
batch = db.execute(
    select(RestaurantTable).where(RestaurantTable.zone == "Batch QA")
    .order_by(RestaurantTable.number)
).scalars().all()
check(r.status_code == 200 and len(batch) == 4, "4 tables with 2 seats created",
      f"{len(batch)} made")
check(all(t.capacity == 2 for t in batch), "every one has 2 seats",
      str({t.capacity for t in batch}))
check([t.number for t in batch] == [highest + i for i in range(1, 5)],
      "numbering continues from the highest in use",
      str([t.number for t in batch]))
check(all(t.is_active and t.status == "free" for t in batch),
      "all are active and free")

# Grid cells must not collide, or the layout editor would stack them.
cells = [(t.pos_x, t.pos_y) for t in batch]
check(len(set(cells)) == 4, "each lands on its own grid square", str(cells))
occupied = {
    (t.pos_x, t.pos_y)
    for t in db.execute(
        select(RestaurantTable).where(RestaurantTable.is_active.is_(True))
    ).scalars().all()
}
all_active = db.execute(
    select(RestaurantTable).where(RestaurantTable.is_active.is_(True))
).scalars().all()
check(len(occupied) == len(all_active),
      "no two active tables share a square after the batch",
      f"{len(occupied)} cells / {len(all_active)} tables")

# The banner rides on the redirect's query string, so it is only on the
# response to the POST — a fresh GET of /admin/tables carries no params.
check(f"{highest + 1}–{highest + 4}" in r.text,
      "the page reports which numbers it created",
      f"expected {highest + 1}-{highest + 4}")

for bad in ({"count": 0, "capacity": 2}, {"count": 51, "capacity": 2},
            {"count": 2, "capacity": 0}, {"count": 2, "capacity": 21}):
    r = client.post("/admin/tables/create-batch", data=bad)
    check(r.status_code == 400, f"batch {bad} refused", str(r.status_code))
after = db.execute(select(RestaurantTable)).scalars().all()
check(len(after) == len(before) + 4, "refused batches created nothing",
      f"{len(after)} total")

# A retired table still owns its number; the batch must not hand it out again.
retired_no = batch[-1].number
client.post(f"/admin/tables/{batch[-1].id}/active", data={"active": 0})
r = client.post("/admin/tables/create-batch", data={
    "count": 2, "capacity": 4, "zone": "Batch QA2",
})
reused = db.execute(
    select(RestaurantTable).where(RestaurantTable.zone == "Batch QA2")
).scalars().all()
check(all(t.number != retired_no for t in reused),
      "a retired table's number is not reused",
      f"retired {retired_no}, made {[t.number for t in reused]}")

# ------------------------------------------------------------ bulk operations
print("\n--- bulk table edits ---")
# Three throwaway tables to batch against.
bulk_ids = []
for n in (9101, 9102, 9103):
    client.post("/admin/tables/create", data={"number": n, "zone": "QA", "capacity": 2})
    row = fresh(RestaurantTable, number=n)
    if row is not None:
        bulk_ids.append(row.id)
check(len(bulk_ids) == 3, "three bulk fixtures created", f"{len(bulk_ids)}/3")

r = client.post("/admin/tables/bulk", data={
    "action": "zone", "table_ids": bulk_ids, "zone": "Terrace QA",
})
zones = [fresh(RestaurantTable, id=i).zone for i in bulk_ids]
check(r.status_code == 200 and zones == ["Terrace QA"] * 3,
      "bulk set zone applies to every selected table", str(set(zones)))

r = client.post("/admin/tables/bulk", data={
    "action": "capacity", "table_ids": bulk_ids, "capacity": 6,
})
caps = [fresh(RestaurantTable, id=i).capacity for i in bulk_ids]
check(caps == [6, 6, 6], "bulk set seats applies to every selected table", str(set(caps)))

r = client.post("/admin/tables/bulk", data={
    "action": "capacity", "table_ids": bulk_ids, "capacity": 99,
})
caps = [fresh(RestaurantTable, id=i).capacity for i in bulk_ids]
check(r.status_code == 400, "bulk capacity 99 refused", str(r.status_code))
check(caps == [6, 6, 6], "refused bulk wrote nothing (all-or-nothing on input errors)")

r = client.post("/admin/tables/bulk", data={"action": "retire", "table_ids": []})
check(r.status_code == 400, "bulk with nothing selected refused", str(r.status_code))
r = client.post("/admin/tables/bulk", data={"action": "explode", "table_ids": bulk_ids})
check(r.status_code == 400, "unknown bulk action refused", str(r.status_code))

# Partial application: mix free tables with one that is mid-service. The free
# ones must retire and the busy one must be reported, not silently dropped.
if busy is not None:
    mixed = bulk_ids + [busy.table_id]
    r = client.post("/admin/tables/bulk", data={"action": "retire", "table_ids": mixed})
    states = [fresh(RestaurantTable, id=i).is_active for i in bulk_ids]
    busy_table = fresh(RestaurantTable, id=busy.table_id)
    check(states == [False, False, False], "free tables in a mixed batch retire")
    check(busy_table.is_active, "the in-service table in that batch is untouched")
    check(str(busy_table.number) in str(r.url),
          "skipped table is named back in the URL", str(r.url))
    check("done=3" in str(r.url), "count of applied changes reported", str(r.url))
    check("still in service" in r.text, "the page explains what was skipped")
else:
    r = client.post("/admin/tables/bulk", data={"action": "retire", "table_ids": bulk_ids})
    check(all(not fresh(RestaurantTable, id=i).is_active for i in bulk_ids),
          "bulk retire applies")
    print("SKIP  no live order to test mixed-batch partial application")

r = client.post("/admin/tables/bulk", data={"action": "restore", "table_ids": bulk_ids})
check(all(fresh(RestaurantTable, id=i).is_active for i in bulk_ids), "bulk restore applies")
check(all(fresh(RestaurantTable, id=i) is not None for i in bulk_ids),
      "bulk retire never deleted a row")

r = client.get("/admin/tables")
check(r.status_code == 200 and 'name="table_ids"' in r.text,
      "table list renders selection checkboxes")

# -------------------------------------------------------------------- staff
print("\n--- staff (section 3) ---")
r = client.post("/admin/staff/create", data={
    "name": TEST_STAFF, "role": Role.WAITER, "pin_code": "4321",
})
p = fresh(Staff, name=TEST_STAFF)
check(p is not None and p.role == Role.WAITER, "create staff", f"id={p.id if p else None}")

r = client.post("/admin/staff/create", data={
    "name": "Bad Pin", "role": Role.WAITER, "pin_code": "12",
})
check(r.status_code == 400, "short PIN refused", str(r.status_code))
r = client.post("/admin/staff/create", data={
    "name": "Bad Role", "role": "chief_taster", "pin_code": "1234",
})
check(r.status_code == 400, "unknown role refused", str(r.status_code))
check(fresh(Staff, name="Bad Role") is None, "refused create wrote nothing")

r = client.post(f"/admin/staff/{p.id}/edit", data={
    "name": TEST_STAFF, "role": Role.KITCHEN, "pin_code": "",
})
p = fresh(Staff, name=TEST_STAFF)
check(p.role == Role.KITCHEN and p.pin_code == "4321",
      "edit role, blank PIN leaves it unchanged")

owners = db.execute(
    select(Staff).where(Staff.role == Role.OWNER, Staff.is_active.is_(True))
).scalars().all()
if len(owners) == 1:
    r = client.post(f"/admin/staff/{owner.id}/active", data={"active": 0})
    check(r.status_code == 400, "last owner cannot deactivate themselves",
          str(r.status_code))
    r = client.post(f"/admin/staff/{owner.id}/edit", data={
        "name": owner.name, "role": Role.WAITER, "pin_code": "",
    })
    check(r.status_code == 400, "last owner cannot demote themselves",
          str(r.status_code))
    check(fresh(Staff, id=owner.id).role == Role.OWNER, "owner role intact")
else:
    print(f"SKIP  {len(owners)} owners active; last-owner guard needs exactly 1")

# A waiter holding open orders cannot be switched off underneath them.
if waiter is not None:
    live = db.execute(
        select(Order).where(
            Order.waiter_id == waiter.id,
            Order.status.in_((OrderStatus.OPEN, OrderStatus.PREPARING,
                              OrderStatus.READY, OrderStatus.PARTIALLY_PAID)),
        )
    ).scalars().first()
    if live is not None:
        r = client.post(f"/admin/staff/{waiter.id}/active", data={"active": 0})
        check(r.status_code == 400, "waiter with open orders cannot be deactivated",
              str(r.status_code))
        check(fresh(Staff, id=waiter.id).is_active, "that waiter is still active")
    else:
        print("SKIP  no open order held by a waiter")

r = client.post(f"/admin/staff/{p.id}/active", data={"active": 0})
check(not fresh(Staff, name=TEST_STAFF).is_active, "staff deactivates")
check(fresh(Staff, name=TEST_STAFF) is not None, "deactivated staff row survives")

# --------------------------------------------------------------------- menu
print("\n--- menu (4.1.2) ---")
cat = db.execute(select(MenuCategory).limit(1)).scalars().first()
r = client.post("/admin/menu/items/create", data={
    "category_id": cat.id, "name": TEST_ITEM, "price": "19.99",
    "description": "probe", "is_shareable": 1,
})
item = fresh(MenuItem, name=TEST_ITEM)
check(item is not None, "create menu item")
check(item is not None and item.price_cents == 1999,
      "19.99 stores as exactly 1999 cents", str(item.price_cents if item else None))
check(item is not None and item.is_shareable, "shareable checkbox is honoured")

r = client.post(f"/admin/menu/items/{item.id}/edit", data={
    "category_id": cat.id, "name": TEST_ITEM, "price": "$1,250", "description": "",
})
item = fresh(MenuItem, name=TEST_ITEM)
check(item.price_cents == 125000, "'$1,250' parses to 125000 cents",
      str(item.price_cents))
check(not item.is_shareable, "unchecked shareable box clears the flag")

for bad in ["12.345", "abc", "", "1.2.3"]:
    r = client.post(f"/admin/menu/items/{item.id}/edit", data={
        "category_id": cat.id, "name": TEST_ITEM, "price": bad,
    })
    # 400 from _cents(), or 422 when FastAPI's own form validation rejects it
    # first — either way the write does not happen.
    check(r.status_code in (400, 422), f"price {bad!r} refused", str(r.status_code))
check(fresh(MenuItem, name=TEST_ITEM).price_cents == 125000,
      "refused edits left the price untouched")

r = client.post(f"/admin/menu/items/{item.id}/active", data={"active": 0})
check(not fresh(MenuItem, name=TEST_ITEM).is_active, "item comes off the menu")
check(fresh(MenuItem, name=TEST_ITEM) is not None, "off-menu item row survives")

r = client.post("/admin/menu/categories/create", data={"name": cat.name})
check(r.status_code == 400, "duplicate category name refused", str(r.status_code))

# ------------------------------------------------------------------- cleanup
print("\n--- cleanup ---")
removed = 0
targets = [
    (RestaurantTable, {"number": TEST_TABLE_NO}),
    (Staff, {"name": TEST_STAFF}),
    (MenuItem, {"name": TEST_ITEM}),
] + [(RestaurantTable, {"number": n}) for n in (9101, 9102, 9103)]
for model, where in targets:
    row = fresh(model, **where)
    if row is not None:
        db.delete(row)
        removed += 1

# The batch-created tables are found by zone, not by a known number.
db.expire_all()
batched = db.execute(
    select(RestaurantTable).where(RestaurantTable.zone.in_(("Batch QA", "Batch QA2")))
).scalars().all()
for row in batched:
    db.delete(row)
db.commit()
check(removed == len(targets), "test fixtures removed", f"{removed}/{len(targets)}")
check(len(batched) == 6, "batch fixtures removed", f"{len(batched)}/6")
check(
    all(fresh(RestaurantTable, number=n) is None for n in (9101, 9102, 9103)),
    "no bulk fixtures left behind",
)

print("\nRESULT:", "admin module OK" if ok else "ADMIN FAILURES")
sys.exit(0 if ok else 1)
