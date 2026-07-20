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

# Layout: move the test table, confirm it persists and collisions are refused.
r = client.post("/admin/tables/layout", data={"layout": f"{t.id}:3:4"})
t = fresh(RestaurantTable, number=TEST_TABLE_NO)
check(r.status_code == 200 and (t.pos_x, t.pos_y) == (3, 4),
      "layout saves grid position", f"({t.pos_x},{t.pos_y})")

other = db.execute(
    select(RestaurantTable).where(RestaurantTable.id != t.id).limit(1)
).scalars().first()
r = client.post("/admin/tables/layout", data={"layout": f"{t.id}:1:1,{other.id}:1:1"})
check(r.status_code == 400, "two tables on one square refused", str(r.status_code))
t = fresh(RestaurantTable, number=TEST_TABLE_NO)
check((t.pos_x, t.pos_y) == (3, 4), "refused layout left positions untouched")

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
for model, where in (
    (RestaurantTable, {"number": TEST_TABLE_NO}),
    (Staff, {"name": TEST_STAFF}),
    (MenuItem, {"name": TEST_ITEM}),
):
    row = fresh(model, **where)
    if row is not None:
        db.delete(row)
        removed += 1
db.commit()
check(removed == 3, "test fixtures removed", f"{removed}/3")

print("\nRESULT:", "admin module OK" if ok else "ADMIN FAILURES")
sys.exit(0 if ok else 1)
