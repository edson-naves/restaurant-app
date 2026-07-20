"""Setup & administration — the reference data behind sections 3, 4.1.1 and 4.1.2.

Tables, staff and the menu are the fixed inputs the operational screens read:
the floor plan a waiter taps, the roles the permission matrix in deps.py
resolves, the items an order line points at. Until now they existed only
because app.seed inserted them, so changing a price or adding table 27 meant
editing seed code or opening the SQLite file by hand.

Nothing here deletes. Anything that has ever appeared on an order is referenced
by that order, its payments, its receipts and the star-schema dimensions built
from them; deleting the row would strand all of it and silently change every
historical report. Records are deactivated instead — hidden from the
operational screens, still resolvable by history.

Gating follows the existing access matrix: staff editing is 'staff.manage' and
tables/menu are 'settings', both owner-only per section 3 (which excludes
managers from settings explicitly).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import render, require
from app.models.oltp import (
    MenuCategory,
    MenuItem,
    Modifier,
    Order,
    OrderStatus,
    RestaurantTable,
    Role,
    Staff,
    TableStatus,
)

router = APIRouter(prefix="/admin")

# Width of the floor-plan editor grid. Seed data lays tables out 8 per row, so
# anything at or above 8 keeps existing positions valid.
GRID_COLS = 10

# An order in one of these states is finished; its table and waiter are free.
LIVE_ORDER_STATES = (
    OrderStatus.OPEN,
    OrderStatus.PREPARING,
    OrderStatus.READY,
    OrderStatus.PARTIALLY_PAID,
)


def _cents(value: str, field: str = "Price") -> int:
    """Parse a currency string typed by a human into integer cents.

    Money is integer cents everywhere (see database.py); this is the one place
    a decimal string is allowed in, so it converts through a scaled int rather
    than float(x) * 100 — 19.99 * 100 is 1998.9999... and truncates to 1998.
    """
    raw = value.strip().replace("$", "").replace(",", "").lstrip("+")
    negative = raw.startswith("-")
    if negative:
        raw = raw[1:]
    if not raw:
        raise HTTPException(400, f"{field} is required.")
    whole, _, frac = raw.partition(".")
    whole = whole or "0"
    if not whole.isdigit() or (frac and not frac.isdigit()) or len(frac) > 2:
        raise HTTPException(400, f"{field} '{value}' is not a valid amount.")
    cents = int(whole) * 100 + int(frac.ljust(2, "0") or 0)
    return -cents if negative else cents


def _live_order_for_table(db: Session, table_id: int) -> Order | None:
    return db.execute(
        select(Order).where(
            Order.table_id == table_id, Order.status.in_(LIVE_ORDER_STATES)
        )
    ).scalars().first()


# --------------------------------------------------------------------------
# Tables — 4.1.1
# --------------------------------------------------------------------------

@router.get("")
def admin_home():
    return RedirectResponse("/admin/tables", status_code=303)


@router.get("/tables")
def tables_page(
    request: Request,
    done: int = 0,
    skipped: str = "",
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    tables = db.execute(
        select(RestaurantTable).order_by(RestaurantTable.number)
    ).scalars().all()
    active = [t for t in tables if t.is_active]

    # The editor canvas is a fixed-width grid; give it enough rows to hold
    # every table plus a spare row to drag into.
    rows = max([t.pos_y for t in active] + [len(active) // GRID_COLS]) + 2

    waiters = db.execute(
        select(Staff).where(Staff.role == Role.WAITER, Staff.is_active.is_(True))
        .order_by(Staff.name)
    ).scalars().all()
    zones = db.execute(
        select(RestaurantTable.zone).distinct().order_by(RestaurantTable.zone)
    ).scalars().all()

    return render(request, "admin_tables.html", {
        "db": db, "staff": staff,
        "tables": tables,
        "active_tables": active,
        "retired": [t for t in tables if not t.is_active],
        "busy_ids": {
            t.id for t in active
            if t.status != TableStatus.FREE or _live_order_for_table(db, t.id)
        },
        "cols": GRID_COLS, "rows": rows,
        "zones": zones, "waiters": waiters,
        "seats_total": sum(t.capacity for t in active),
        "done": done, "skipped": [s for s in skipped.split(",") if s],
        "title": "Manage tables",
    })


@router.post("/tables/create")
def create_table(
    number: int = Form(...),
    zone: str = Form("Main"),
    capacity: int = Form(4),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    if number < 1:
        raise HTTPException(400, "Table number must be 1 or greater.")
    if not 1 <= capacity <= 20:
        raise HTTPException(400, "Capacity must be between 1 and 20 seats.")

    clash = db.execute(
        select(RestaurantTable).where(RestaurantTable.number == number)
    ).scalars().first()
    if clash is not None:
        # The number is UNIQUE in the schema; a retired table still holds it.
        raise HTTPException(
            400,
            f"Table {number} already exists"
            + ("" if clash.is_active else " (retired — reactivate it instead).")
        )

    used = db.execute(
        select(func.count()).select_from(RestaurantTable)
        .where(RestaurantTable.is_active.is_(True))
    ).scalar_one()
    db.add(RestaurantTable(
        number=number, zone=zone.strip() or "Main", capacity=capacity,
        status=TableStatus.FREE, is_active=True,
        pos_x=used % GRID_COLS, pos_y=used // GRID_COLS,
    ))
    db.commit()
    return RedirectResponse("/admin/tables", status_code=303)


@router.post("/tables/{table_id}/edit")
def edit_table(
    table_id: int,
    number: int = Form(...),
    zone: str = Form("Main"),
    capacity: int = Form(4),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    table = db.get(RestaurantTable, table_id)
    if table is None:
        raise HTTPException(404, "Table not found")
    if not 1 <= capacity <= 20:
        raise HTTPException(400, "Capacity must be between 1 and 20 seats.")

    if number != table.number:
        if number < 1:
            raise HTTPException(400, "Table number must be 1 or greater.")
        clash = db.execute(
            select(RestaurantTable).where(
                RestaurantTable.number == number, RestaurantTable.id != table_id
            )
        ).scalars().first()
        if clash is not None:
            raise HTTPException(400, f"Table {number} already exists.")
        # Renumbering a table mid-service would relabel a live ticket and the
        # receipt printed from it.
        if _live_order_for_table(db, table_id):
            raise HTTPException(
                400,
                f"Table {table.number} has a live order — close it before renumbering.",
            )
        table.number = number

    table.zone = zone.strip() or "Main"
    table.capacity = capacity
    db.commit()
    return RedirectResponse("/admin/tables", status_code=303)


@router.post("/tables/{table_id}/active")
def toggle_table(
    table_id: int,
    active: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    table = db.get(RestaurantTable, table_id)
    if table is None:
        raise HTTPException(404, "Table not found")

    if not active:
        order = _live_order_for_table(db, table_id)
        if order is not None:
            raise HTTPException(
                400,
                f"Table {table.number} has open order {order.code}. "
                "Close or pay it before retiring the table.",
            )
        if table.status != TableStatus.FREE:
            raise HTTPException(
                400, f"Table {table.number} is {table.status.replace('_', ' ')}."
            )
        table.current_waiter_id = None

    table.is_active = bool(active)
    db.commit()
    return RedirectResponse("/admin/tables", status_code=303)


@router.post("/tables/bulk")
def bulk_tables(
    action: str = Form(...),
    table_ids: list[int] = Form([]),
    zone: str = Form(""),
    capacity: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Apply one change to many tables.

    Partial application on purpose: a batch that touches one table in service
    applies to the rest and reports what it skipped. The all-or-nothing
    alternative would make a 30-table retire fail because a single table has a
    live order, and the operator would have to hunt for it by hand.

    Input errors (bad action, invalid capacity) still reject the whole batch —
    those are wrong for every row, not just some.
    """
    if not table_ids:
        raise HTTPException(400, "No tables selected.")
    if action not in ("retire", "restore", "zone", "capacity"):
        raise HTTPException(400, f"Unknown bulk action '{action}'.")
    if action == "capacity" and not 1 <= capacity <= 20:
        raise HTTPException(400, "Capacity must be between 1 and 20 seats.")
    if action == "zone" and not zone.strip():
        raise HTTPException(400, "Zone is required.")

    tables = db.execute(
        select(RestaurantTable).where(RestaurantTable.id.in_(table_ids))
    ).scalars().all()

    done = 0
    skipped: list[str] = []
    for table in tables:
        if action == "retire":
            # Same guard as the single-table route: a table with a live order
            # or a seated party stays on the floor.
            if not table.is_active:
                continue                       # already retired, not a failure
            if table.status != TableStatus.FREE or _live_order_for_table(db, table.id):
                skipped.append(str(table.number))
                continue
            table.current_waiter_id = None
            table.is_active = False
        elif action == "restore":
            if table.is_active:
                continue
            table.is_active = True
        elif action == "zone":
            table.zone = zone.strip()
        elif action == "capacity":
            table.capacity = capacity
        done += 1

    db.commit()
    return RedirectResponse(
        f"/admin/tables?done={done}&skipped={','.join(skipped)}", status_code=303
    )


@router.post("/tables/layout")
def save_layout(
    layout: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Persist grid coordinates from the drag editor.

    Payload is 'id:x:y' triples separated by commas — a hidden field on a plain
    form post, so the editor needs script only for the dragging itself and the
    save path stays the same POST-redirect-GET as every other screen here.
    """
    tables = {
        t.id: t for t in db.execute(select(RestaurantTable)).scalars().all()
    }
    seen: set[tuple[int, int]] = set()
    for chunk in layout.split(","):
        if not chunk.strip():
            continue
        parts = chunk.split(":")
        if len(parts) != 3 or not all(p.strip().lstrip("-").isdigit() for p in parts):
            raise HTTPException(400, f"Malformed layout entry '{chunk}'.")
        tid, x, y = (int(p) for p in parts)
        table = tables.get(tid)
        if table is None:
            raise HTTPException(400, f"Unknown table id {tid} in layout.")
        if not (0 <= x < GRID_COLS and 0 <= y):
            raise HTTPException(400, f"Position {x},{y} is off the grid.")
        if (x, y) in seen:
            raise HTTPException(400, f"Two tables share position {x},{y}.")
        seen.add((x, y))
        table.pos_x, table.pos_y = x, y
    db.commit()
    return RedirectResponse("/admin/tables", status_code=303)


# --------------------------------------------------------------------------
# Staff — section 3
# --------------------------------------------------------------------------

@router.get("/staff")
def staff_page(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("staff.manage")),
):
    people = db.execute(select(Staff).order_by(Staff.role, Staff.name)).scalars().all()
    live = db.execute(
        select(Order).where(Order.status.in_(LIVE_ORDER_STATES))
    ).scalars().all()

    return render(request, "admin_staff.html", {
        "db": db, "staff": staff,
        "people": [p for p in people if p.is_active],
        "retired": [p for p in people if not p.is_active],
        "open_counts": {
            p.id: sum(1 for o in live if o.waiter_id == p.id) for p in people
        },
        "roles": Role.ALL,
        "title": "Manage staff",
    })


def _check_pin(pin: str) -> str:
    pin = pin.strip()
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        raise HTTPException(400, "PIN must be 4 to 8 digits.")
    return pin


def _check_role(role: str) -> str:
    if role not in Role.ALL:
        raise HTTPException(400, f"'{role}' is not a valid role.")
    return role


@router.post("/staff/create")
def create_staff(
    name: str = Form(...),
    role: str = Form(...),
    pin_code: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("staff.manage")),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name is required.")
    db.add(Staff(
        name=name, role=_check_role(role), pin_code=_check_pin(pin_code),
        is_active=True,
    ))
    db.commit()
    return RedirectResponse("/admin/staff", status_code=303)


@router.post("/staff/{staff_id}/edit")
def edit_staff(
    staff_id: int,
    name: str = Form(...),
    role: str = Form(...),
    pin_code: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("staff.manage")),
):
    person = db.get(Staff, staff_id)
    if person is None:
        raise HTTPException(404, "Staff member not found")

    name = name.strip()
    if not name:
        raise HTTPException(400, "Name is required.")
    role = _check_role(role)

    if person.role == Role.OWNER and role != Role.OWNER:
        _guard_last_owner(db, person, "demote")

    person.name = name
    person.role = role
    if pin_code.strip():                       # blank means "leave the PIN alone"
        person.pin_code = _check_pin(pin_code)
    db.commit()
    return RedirectResponse("/admin/staff", status_code=303)


def _guard_last_owner(db: Session, person: Staff, verb: str) -> None:
    """Locking every owner out is unrecoverable through the UI.

    staff.manage and settings are owner-only, so with no active owner left
    nobody can create one — the only way back would be editing the database
    directly.
    """
    owners = db.execute(
        select(func.count()).select_from(Staff).where(
            Staff.role == Role.OWNER, Staff.is_active.is_(True), Staff.id != person.id
        )
    ).scalar_one()
    if owners == 0:
        raise HTTPException(
            400,
            f"{person.name} is the only active owner — you cannot {verb} them. "
            "Promote another owner first.",
        )


@router.post("/staff/{staff_id}/active")
def toggle_staff(
    staff_id: int,
    active: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("staff.manage")),
):
    person = db.get(Staff, staff_id)
    if person is None:
        raise HTTPException(404, "Staff member not found")

    if not active:
        if person.role == Role.OWNER:
            _guard_last_owner(db, person, "deactivate")

        open_orders = db.execute(
            select(Order).where(
                Order.waiter_id == staff_id, Order.status.in_(LIVE_ORDER_STATES)
            )
        ).scalars().all()
        if open_orders:
            codes = ", ".join(o.code for o in open_orders[:3])
            raise HTTPException(
                400,
                f"{person.name} still has {len(open_orders)} open order(s): {codes}. "
                "Reassign them on the floor plan first.",
            )
        # Drop the section assignment so no table points at inactive staff.
        for table in db.execute(
            select(RestaurantTable).where(RestaurantTable.current_waiter_id == staff_id)
        ).scalars().all():
            table.current_waiter_id = None

    person.is_active = bool(active)
    db.commit()
    return RedirectResponse("/admin/staff", status_code=303)


# --------------------------------------------------------------------------
# Menu — 4.1.2
# --------------------------------------------------------------------------

@router.get("/menu")
def menu_page(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    categories = db.execute(
        select(MenuCategory).order_by(MenuCategory.sort_order, MenuCategory.name)
    ).scalars().all()
    items = db.execute(
        select(MenuItem).order_by(MenuItem.name)
    ).scalars().all()
    modifiers = db.execute(select(Modifier).order_by(Modifier.name)).scalars().all()

    by_cat = {c.id: [] for c in categories}
    for item in items:
        by_cat.setdefault(item.category_id, []).append(item)

    return render(request, "admin_menu.html", {
        "db": db, "staff": staff,
        "categories": categories, "by_cat": by_cat, "modifiers": modifiers,
        "active_count": sum(1 for i in items if i.is_active),
        "title": "Manage menu",
    })


@router.post("/menu/categories/create")
def create_category(
    name: str = Form(...),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Category name is required.")
    exists = db.execute(
        select(MenuCategory).where(MenuCategory.name == name)
    ).scalars().first()
    if exists is not None:
        raise HTTPException(400, f"Category '{name}' already exists.")
    db.add(MenuCategory(name=name, sort_order=sort_order))
    db.commit()
    return RedirectResponse("/admin/menu", status_code=303)


@router.post("/menu/categories/{category_id}/edit")
def edit_category(
    category_id: int,
    name: str = Form(...),
    sort_order: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    cat = db.get(MenuCategory, category_id)
    if cat is None:
        raise HTTPException(404, "Category not found")
    name = name.strip()
    if not name:
        raise HTTPException(400, "Category name is required.")
    clash = db.execute(
        select(MenuCategory).where(
            MenuCategory.name == name, MenuCategory.id != category_id
        )
    ).scalars().first()
    if clash is not None:
        raise HTTPException(400, f"Category '{name}' already exists.")
    cat.name = name
    cat.sort_order = sort_order
    db.commit()
    return RedirectResponse("/admin/menu", status_code=303)


@router.post("/menu/items/create")
def create_item(
    category_id: int = Form(...),
    name: str = Form(...),
    price: str = Form(...),
    description: str = Form(""),
    is_shareable: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    if db.get(MenuCategory, category_id) is None:
        raise HTTPException(404, "Category not found")
    name = name.strip()
    if not name:
        raise HTTPException(400, "Item name is required.")
    cents = _cents(price)
    if cents < 0:
        raise HTTPException(400, "Price cannot be negative.")

    db.add(MenuItem(
        category_id=category_id, name=name, description=description.strip(),
        price_cents=cents, is_active=True, is_shareable=bool(is_shareable),
    ))
    db.commit()
    return RedirectResponse("/admin/menu", status_code=303)


@router.post("/menu/items/{item_id}/edit")
def edit_item(
    item_id: int,
    category_id: int = Form(...),
    name: str = Form(...),
    price: str = Form(...),
    description: str = Form(""),
    is_shareable: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    item = db.get(MenuItem, item_id)
    if item is None:
        raise HTTPException(404, "Menu item not found")
    if db.get(MenuCategory, category_id) is None:
        raise HTTPException(404, "Category not found")
    name = name.strip()
    if not name:
        raise HTTPException(400, "Item name is required.")
    cents = _cents(price)
    if cents < 0:
        raise HTTPException(400, "Price cannot be negative.")

    # OrderItem captures unit_price_cents at the time it is added, so a price
    # change here never rewrites an existing bill.
    item.category_id = category_id
    item.name = name
    item.description = description.strip()
    item.price_cents = cents
    item.is_shareable = bool(is_shareable)
    db.commit()
    return RedirectResponse("/admin/menu", status_code=303)


@router.post("/menu/items/{item_id}/active")
def toggle_item(
    item_id: int,
    active: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    item = db.get(MenuItem, item_id)
    if item is None:
        raise HTTPException(404, "Menu item not found")
    item.is_active = bool(active)
    db.commit()
    return RedirectResponse("/admin/menu", status_code=303)


@router.post("/menu/modifiers/create")
def create_modifier(
    name: str = Form(...),
    price_delta: str = Form("0"),
    category_id: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Modifier name is required.")
    db.add(Modifier(
        name=name,
        price_delta_cents=_cents(price_delta, "Price delta"),
        category_id=category_id or None,      # 0 from the form means "any category"
    ))
    db.commit()
    return RedirectResponse("/admin/menu", status_code=303)


@router.post("/menu/modifiers/{modifier_id}/edit")
def edit_modifier(
    modifier_id: int,
    name: str = Form(...),
    price_delta: str = Form("0"),
    category_id: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    mod = db.get(Modifier, modifier_id)
    if mod is None:
        raise HTTPException(404, "Modifier not found")
    name = name.strip()
    if not name:
        raise HTTPException(400, "Modifier name is required.")
    # OrderItemModifier snapshots price_delta_cents per line, so edits here do
    # not disturb orders already taken.
    mod.name = name
    mod.price_delta_cents = _cents(price_delta, "Price delta")
    mod.category_id = category_id or None
    db.commit()
    return RedirectResponse("/admin/menu", status_code=303)
