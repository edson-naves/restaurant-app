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

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import render, require
from app.models.oltp import (
    Floor,
    MenuCategory,
    MenuItem,
    Modifier,
    Order,
    OrderStatus,
    RestaurantTable,
    Role,
    Staff,
    TableStatus,
    Zone,
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


def _active_tables(db: Session, floor_id: int | None = None) -> list[RestaurantTable]:
    tables = db.execute(
        select(RestaurantTable).where(RestaurantTable.is_active.is_(True))
    ).scalars().all()
    if floor_id is None:
        return tables
    return [t for t in tables if t.floor_id == floor_id]


def _free_cells(db: Session, count: int, floor_id: int | None) -> list[tuple[int, int]]:
    """Pick `count` unoccupied grid squares on one floor, scanning row-major.

    A new table's position cannot be derived from how many tables exist: the
    seeded floor is laid out 8 to a row while the editor grid is GRID_COLS
    wide, so counting drops the new table straight onto an occupied square.
    Retired tables are ignored — they are off the floor and their stale
    coordinates should not reserve space. Each floor has its own grid, so only
    tables on the same floor contend for a square.
    """
    taken = {(t.pos_x, t.pos_y) for t in _active_tables(db, floor_id)}
    cells: list[tuple[int, int]] = []
    i = 0
    while len(cells) < count:
        cell = (i % GRID_COLS, i // GRID_COLS)
        if cell not in taken:
            taken.add(cell)
            cells.append(cell)
        i += 1
    return cells


def _parse_names(raw: str, limit: int = 50) -> list[str]:
    """Split a typed list of names on commas or newlines.

    Setup reads better as "Ground, First, Second" than as a count, and the
    names are the operator's own words — a floor called "Mezzanine" should not
    have to be renamed from "2nd floor" afterwards.
    """
    parts = [p.strip() for chunk in raw.split("\n") for p in chunk.split(",")]
    names: list[str] = []
    for p in parts:
        if p and p not in names:            # ignore blanks and repeats in one paste
            names.append(p)
    if not names:
        raise HTTPException(400, "Type at least one name.")
    if len(names) > limit:
        raise HTTPException(400, f"That is more than {limit} names at once.")
    return names


def _check_color(value: str) -> str:
    """Accept a #rrggbb colour, the only form <input type=color> submits.

    Validated rather than trusted because the value lands straight in a style
    attribute on the floor plan.
    """
    value = value.strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", value):
        raise HTTPException(400, f"'{value}' is not a #rrggbb colour.")
    return value


def _zone_or_400(db: Session, zone_id: int) -> Zone:
    zone = db.get(Zone, zone_id)
    if zone is None:
        raise HTTPException(404, "Zone not found")
    if not zone.is_active or not zone.floor.is_active:
        raise HTTPException(400, f"{zone.label} is retired — pick an active zone.")
    return zone


def _assign_zone(table: RestaurantTable, zone: Zone) -> None:
    """Point a table at a zone, keeping the denormalized label in step.

    RestaurantTable.zone is what the ETL copies into DimTable and what the
    floor plan prints; letting it drift from zone_id would make the floor plan
    and the reports disagree about where a table is.
    """
    table.zone_id = zone.id
    table.zone = zone.name


def _add_tables(db: Session, zone: Zone, count: int, capacity: int) -> list[int]:
    """Create `count` tables of `capacity` seats in one zone. Does not commit.

    Numbers continue from the highest in use and skip anything taken: a retired
    table keeps its number, and reissuing it would make two eras of service
    look like one table in any report read by number.
    """
    taken_numbers = {
        t.number for t in db.execute(select(RestaurantTable)).scalars().all()
    }
    cells = _free_cells(db, count, zone.floor_id)

    number = max(taken_numbers, default=0)
    made: list[int] = []
    for (pos_x, pos_y) in cells:
        number += 1
        while number in taken_numbers:
            number += 1
        table = RestaurantTable(
            number=number, capacity=capacity,
            status=TableStatus.FREE, is_active=True, pos_x=pos_x, pos_y=pos_y,
        )
        _assign_zone(table, zone)
        db.add(table)
        made.append(number)
    return made


def _assign_waiter(db: Session, table: RestaurantTable, waiter_id: int) -> None:
    """Set or clear the waiter covering a table. 0 means nobody.

    A live order carries its own waiter_id — that is who the sales report
    credits — so reassigning the table moves the open order with it. Leaving
    them apart would show one name on the floor plan and bill the sale to
    another. This mirrors reassign_waiter in routers/sales.py.
    """
    if waiter_id:
        waiter = db.get(Staff, waiter_id)
        if waiter is None:
            raise HTTPException(404, "Waiter not found")
        if not waiter.is_active:
            raise HTTPException(400, f"{waiter.name} is not an active staff member.")
        if waiter.role != Role.WAITER:
            raise HTTPException(
                400, f"{waiter.name} is a {waiter.role.replace('_', ' ')}, not a waiter."
            )
        table.current_waiter_id = waiter.id
    else:
        table.current_waiter_id = None

    order = _live_order_for_table(db, table.id)
    if order is not None:
        order.waiter_id = table.current_waiter_id


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
    created: str = "",
    skipped: str = "",
    floor: int = 0,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    all_tables = db.execute(
        select(RestaurantTable).order_by(RestaurantTable.number)
    ).scalars().all()

    floors = db.execute(
        select(Floor).where(Floor.is_active.is_(True))
        .order_by(Floor.sort_order, Floor.name)
    ).scalars().all()
    # One floor at a time, for the grid and the list alike — a 3-floor
    # restaurant otherwise scrolls past 120 rows to reach the one being edited.
    current = next((f for f in floors if f.id == floor), floors[0] if floors else None)

    tables = [t for t in all_tables if current and t.floor_id == current.id]
    active = [t for t in tables if t.is_active]
    on_floor = active

    # Enough rows to hold this floor's tables plus a spare row to drag into.
    rows = max([t.pos_y for t in on_floor] + [len(on_floor) // GRID_COLS]) + 2

    waiters = db.execute(
        select(Staff).where(Staff.role == Role.WAITER, Staff.is_active.is_(True))
        .order_by(Staff.name)
    ).scalars().all()
    zones = db.execute(
        select(Zone).where(Zone.is_active.is_(True)).order_by(Zone.floor_id, Zone.sort_order)
    ).scalars().all()

    return render(request, "admin_tables.html", {
        "db": db, "staff": staff,
        "tables": tables,
        "active_tables": active,
        "floor_tables": on_floor,
        "retired": [t for t in tables if not t.is_active],
        "busy_ids": {
            t.id for t in active
            if t.status != TableStatus.FREE or _live_order_for_table(db, t.id)
        },
        "cols": GRID_COLS, "rows": rows,
        "floors": floors, "current_floor": current,
        # Table numbers are unique across the whole restaurant, so the next one
        # has to come from every table, not just this floor's.
        "next_number": max([t.number for t in all_tables], default=0) + 1,
        "grand_total": len(all_tables),
        # Every active zone, for the bulk "move to" control — moving a table
        # between floors is a legitimate action.
        "zones": [z for z in zones if z.floor.is_active],
        # Just this floor's, for adding a table to the floor on screen.
        "floor_zones": [
            z for z in zones if current and z.floor_id == current.id
        ],
        "waiters": waiters,
        "seats_total": sum(t.capacity for t in active),
        "done": done, "created": created,
        "skipped": [s for s in skipped.split(",") if s],
        "title": "Manage tables",
    })


@router.post("/tables/create")
def create_table(
    number: int = Form(...),
    zone_id: int = Form(...),
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

    zone = _zone_or_400(db, zone_id)
    (pos_x, pos_y), = _free_cells(db, 1, zone.floor_id)
    table = RestaurantTable(
        number=number, capacity=capacity,
        status=TableStatus.FREE, is_active=True, pos_x=pos_x, pos_y=pos_y,
    )
    _assign_zone(table, zone)
    db.add(table)
    db.commit()
    return RedirectResponse(f"/admin/tables?floor={zone.floor_id}", status_code=303)


# Adding several tables at once lives on the Floors & zones page (step 3),
# where the zone is the row you are already looking at rather than something
# to re-pick from a dropdown. Both paths shared _add_tables(); only that one
# remains.


@router.post("/tables/{table_id}/edit")
def edit_table(
    table_id: int,
    number: int = Form(...),
    zone_id: int = Form(...),
    capacity: int = Form(4),
    waiter_id: int = Form(0),
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

    zone = _zone_or_400(db, zone_id)
    # Moving to another floor moves the table to a different grid, where its
    # old coordinates may already be taken.
    if zone.floor_id != table.floor_id:
        (table.pos_x, table.pos_y), = _free_cells(db, 1, zone.floor_id)
    _assign_zone(table, zone)
    table.capacity = capacity
    _assign_waiter(db, table, waiter_id)
    db.commit()
    return RedirectResponse(f"/admin/tables?floor={zone.floor_id}", status_code=303)


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
    zone_id: int = Form(0),
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
    target_zone = _zone_or_400(db, zone_id) if action == "zone" else None

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
            if target_zone.floor_id != table.floor_id:
                (table.pos_x, table.pos_y), = _free_cells(db, 1, target_zone.floor_id)
            _assign_zone(table, target_zone)
            # The session does not autoflush, so without this the next
            # _free_cells call would not see this table and would hand out the
            # same square again.
            db.flush()
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
    moving = {
        int(c.split(":")[0]) for c in layout.split(",")
        if c.strip() and c.split(":")[0].strip().lstrip("-").isdigit()
    }
    # Squares held by active tables the payload does not mention, keyed by
    # floor — each floor is its own grid, so (0,0) upstairs and (0,0)
    # downstairs are different squares. The drag editor submits every chip on
    # the floor being edited, so this normally only holds other floors; a
    # partial payload must not be able to park one table on top of another.
    seen: set[tuple[int | None, int, int]] = {
        (t.floor_id, t.pos_x, t.pos_y)
        for t in _active_tables(db) if t.id not in moving
    }
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
        if (table.floor_id, x, y) in seen:
            raise HTTPException(400, f"Two tables share position {x},{y}.")
        seen.add((table.floor_id, x, y))
        table.pos_x, table.pos_y = x, y
    db.commit()
    return RedirectResponse("/admin/tables", status_code=303)


# --------------------------------------------------------------------------
# Floors & zones — the two-level location behind 4.1.1
# --------------------------------------------------------------------------

@router.get("/floors")
def floors_page(
    request: Request,
    done: int = 0,
    created: str = "",
    floor: int = 0,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    floors = db.execute(
        select(Floor).order_by(Floor.sort_order, Floor.name)
    ).scalars().all()
    # One floor's zones at a time, picked from the selector. Retired floors stay
    # in the list so they can be restored.
    current = next((f for f in floors if f.id == floor), floors[0] if floors else None)
    tables = db.execute(select(RestaurantTable)).scalars().all()

    counts: dict[int, int] = {}
    for t in tables:
        if t.zone_id is not None:
            counts[t.zone_id] = counts.get(t.zone_id, 0) + 1

    return render(request, "admin_floors.html", {
        "db": db, "staff": staff,
        "floors": floors, "current_floor": current, "counts": counts,
        "done": done, "created": created,
        "seats": {
            z.id: sum(t.capacity for t in tables if t.zone_id == z.id)
            for f in floors for z in f.zones
        },
        "title": "Manage floors & zones",
    })


@router.post("/floors/create")
def create_floors(
    names: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Step 1 — name the floors. Several at once, comma or newline separated."""
    wanted = _parse_names(names, limit=20)
    existing = {f.name for f in db.execute(select(Floor)).scalars().all()}
    top = db.execute(select(func.max(Floor.sort_order))).scalar_one() or 0

    made = 0
    for name in wanted:
        if name in existing:
            continue                           # already there; re-adding is a no-op
        top += 1
        db.add(Floor(name=name, sort_order=top, is_active=True))
        existing.add(name)
        made += 1

    db.commit()
    return RedirectResponse(f"/admin/floors?done={made}", status_code=303)


@router.post("/floors/{floor_id}/zones/create")
def create_zones(
    floor_id: int,
    names: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Step 2 — name the zones on one floor."""
    floor = db.get(Floor, floor_id)
    if floor is None:
        raise HTTPException(404, "Floor not found")

    wanted = _parse_names(names, limit=26)
    existing = {z.name for z in floor.zones}
    back = f"/admin/floors?floor={floor_id}"
    top = max([z.sort_order for z in floor.zones], default=-1)

    made = 0
    for name in wanted:
        if name in existing:
            continue
        top += 1
        db.add(Zone(floor_id=floor.id, name=name, sort_order=top, is_active=True))
        existing.add(name)
        made += 1

    db.commit()
    return RedirectResponse(f"{back}&done={made}", status_code=303)


@router.post("/zones/{zone_id}/tables")
def create_zone_tables(
    zone_id: int,
    count: int = Form(...),
    capacity: int = Form(4),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Step 3 — how many tables, and how many seats each, for one zone."""
    if not 1 <= count <= 50:
        raise HTTPException(400, "Choose between 1 and 50 tables to add.")
    if not 1 <= capacity <= 20:
        raise HTTPException(400, "Seats must be between 1 and 20.")

    zone = _zone_or_400(db, zone_id)
    made = _add_tables(db, zone, count, capacity)
    db.commit()
    span = f"{made[0]}" if len(made) == 1 else f"{made[0]}–{made[-1]}"
    return RedirectResponse(
        f"/admin/floors?floor={zone.floor_id}&done={len(made)}&created={span}",
        status_code=303,
    )


def _reorder(siblings: list, target, direction: str) -> None:
    """Move one item up or down among its siblings.

    sort_order is renumbered 0..n-1 across the whole group on every move, so a
    list that arrived with duplicate or sparse values (hand-edited, or migrated)
    ends up consistent rather than compounding the mess. Raw numbers are never
    shown — the operator only ever presses up or down.
    """
    if direction not in ("up", "down"):
        raise HTTPException(400, "Direction must be up or down.")
    ids = [s.id for s in siblings]
    i = ids.index(target.id)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(siblings):
        siblings[i], siblings[j] = siblings[j], siblings[i]
    for position, s in enumerate(siblings):
        s.sort_order = position


@router.post("/floors/{floor_id}/move")
def move_floor(
    floor_id: int,
    direction: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    floor = db.get(Floor, floor_id)
    if floor is None:
        raise HTTPException(404, "Floor not found")
    siblings = db.execute(
        select(Floor).order_by(Floor.sort_order, Floor.name)
    ).scalars().all()
    _reorder(list(siblings), floor, direction)
    db.commit()
    return RedirectResponse(f"/admin/floors?floor={floor_id}", status_code=303)


@router.post("/zones/{zone_id}/move")
def move_zone(
    zone_id: int,
    direction: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    zone = db.get(Zone, zone_id)
    if zone is None:
        raise HTTPException(404, "Zone not found")
    siblings = db.execute(
        select(Zone).where(Zone.floor_id == zone.floor_id)
        .order_by(Zone.sort_order, Zone.name)
    ).scalars().all()
    _reorder(list(siblings), zone, direction)
    db.commit()
    return RedirectResponse(f"/admin/floors?floor={zone.floor_id}", status_code=303)


@router.post("/floors/{floor_id}/edit")
def edit_floor(
    floor_id: int,
    name: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    floor = db.get(Floor, floor_id)
    if floor is None:
        raise HTTPException(404, "Floor not found")
    name = name.strip()
    if not name:
        raise HTTPException(400, "Floor name is required.")
    clash = db.execute(
        select(Floor).where(Floor.name == name, Floor.id != floor_id)
    ).scalars().first()
    if clash is not None:
        raise HTTPException(400, f"A floor called '{name}' already exists.")
    floor.name = name
    db.commit()
    return RedirectResponse(f"/admin/floors?floor={floor_id}", status_code=303)


@router.post("/floors/{floor_id}/active")
def toggle_floor(
    floor_id: int,
    active: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    floor = db.get(Floor, floor_id)
    if floor is None:
        raise HTTPException(404, "Floor not found")
    if not active:
        held = [
            t for t in _active_tables(db) if t.floor_id == floor_id
        ]
        if held:
            raise HTTPException(
                400,
                f"{floor.name} still holds {len(held)} active table(s). "
                "Move or retire them first.",
            )
    floor.is_active = bool(active)
    db.commit()
    return RedirectResponse(f"/admin/floors?floor={floor_id}", status_code=303)


@router.post("/zones/{zone_id}/edit")
def edit_zone(
    zone_id: int,
    name: str = Form(...),
    color: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    zone = db.get(Zone, zone_id)
    if zone is None:
        raise HTTPException(404, "Zone not found")
    name = name.strip()
    if not name:
        raise HTTPException(400, "Zone name is required.")
    clash = db.execute(
        select(Zone).where(
            Zone.floor_id == zone.floor_id, Zone.name == name, Zone.id != zone_id
        )
    ).scalars().first()
    if clash is not None:
        raise HTTPException(400, f"{zone.floor.name} already has a '{name}'.")

    zone.name = name
    if color.strip():
        zone.color = _check_color(color)
    # Keep every table's denormalized label in step with the rename.
    for t in db.execute(
        select(RestaurantTable).where(RestaurantTable.zone_id == zone_id)
    ).scalars().all():
        t.zone = name
    db.commit()
    return RedirectResponse(f"/admin/floors?floor={zone.floor_id}", status_code=303)


@router.post("/zones/{zone_id}/active")
def toggle_zone(
    zone_id: int,
    active: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    zone = db.get(Zone, zone_id)
    if zone is None:
        raise HTTPException(404, "Zone not found")
    if not active:
        held = [t for t in _active_tables(db) if t.zone_id == zone_id]
        if held:
            raise HTTPException(
                400,
                f"{zone.label} still holds {len(held)} active table(s). "
                "Move or retire them first.",
            )
    zone.is_active = bool(active)
    db.commit()
    return RedirectResponse(f"/admin/floors?floor={zone.floor_id}", status_code=303)


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
