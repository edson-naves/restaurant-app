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

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.deps import WEB_DIR, render, require, role_capabilities
from app.security import hash_pin
from app.services import settings as settings_svc
from app.services.images import resize_to_data_uri, save_image
from app.models.oltp import (
    DAY_MENU_COURSES,
    DayMenu,
    DayMenuChoice,
    day_menu_course_for_category,
    Floor,
    HappyHour,
    HappyHourCategory,
    HappyHourItem,
    MenuCategory,
    MenuItem,
    Modifier,
    Order,
    OrderStatus,
    Position,
    RestaurantTable,
    Role,
    Shift,
    Staff,
    Station,
    StationType,
    TableStatus,
    Zone,
    day_menu_course_label,
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
    OrderStatus.SERVED,
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

    # For the "assign a waiter to a zone" picker: the waiter currently covering
    # each zone on this floor, so the dropdown can pre-select it. A zone's waiter
    # is well-defined only when its active tables all agree; a mix (or none) maps
    # to 0 (unassigned). Keys are zone ids, values are staff ids.
    zone_waiter: dict[int, int] = {}
    for z in [zz for zz in zones if current and zz.floor_id == current.id]:
        covering = {t.current_waiter_id for t in active
                    if t.zone_id == z.id and t.current_waiter_id}
        zone_waiter[z.id] = next(iter(covering)) if len(covering) == 1 else 0

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
        "zone_waiter": zone_waiter,
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


def _apply_table_edit(
    db: Session, table: RestaurantTable,
    number: int, zone_id: int, capacity: int, waiter_id: int,
) -> bool:
    """Apply one row's edits. Returns whether anything actually changed.

    Each field is compared before being written so that saving a page of rows
    only touches the ones that moved — untouched rows must not trip the
    renumbering guard or rewrite a waiter assignment made elsewhere since the
    page was rendered.
    """
    if not 1 <= capacity <= 20:
        raise HTTPException(400, "Capacity must be between 1 and 20 seats.")
    changed = False

    if number != table.number:
        if number < 1:
            raise HTTPException(400, "Table number must be 1 or greater.")
        clash = db.execute(
            select(RestaurantTable).where(
                RestaurantTable.number == number, RestaurantTable.id != table.id
            )
        ).scalars().first()
        if clash is not None:
            raise HTTPException(400, f"Table {number} already exists.")
        # Renumbering a table mid-service would relabel a live ticket and the
        # receipt printed from it.
        if _live_order_for_table(db, table.id):
            raise HTTPException(
                400,
                f"Table {table.number} has a live order — close it before renumbering.",
            )
        table.number = number
        changed = True

    if zone_id != table.zone_id:
        zone = _zone_or_400(db, zone_id)
        # Moving to another floor moves the table to a different grid, where
        # its old coordinates may already be taken.
        if zone.floor_id != table.floor_id:
            (table.pos_x, table.pos_y), = _free_cells(db, 1, zone.floor_id)
        _assign_zone(table, zone)
        changed = True

    if capacity != table.capacity:
        table.capacity = capacity
        changed = True

    if (waiter_id or None) != table.current_waiter_id:
        _assign_waiter(db, table, waiter_id)
        changed = True

    return changed


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
    _apply_table_edit(db, table, number, zone_id, capacity, waiter_id)
    db.commit()
    return RedirectResponse(f"/admin/tables?floor={table.floor_id}", status_code=303)


@router.post("/tables/save-all")
def save_all_tables(
    table_id: list[int] = Form([]),
    number: list[int] = Form([]),
    zone_id: list[int] = Form([]),
    capacity: list[int] = Form([]),
    waiter_id: list[int] = Form([]),
    floor: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Save every row on the tables page in one go.

    The fields arrive as parallel lists in row order. A mismatched length means
    the form was tampered with or a control failed to submit, and applying it
    would write one row's value onto another — so the whole batch is refused
    rather than guessed at.

    All-or-nothing: one bad row rolls the lot back, because the operator edited
    the page as a unit and a partial save would leave them unable to tell which
    of their changes survived.
    """
    lengths = {len(table_id), len(number), len(zone_id), len(capacity), len(waiter_id)}
    if len(lengths) != 1:
        raise HTTPException(400, "The form did not submit completely — reload and retry.")
    if not table_id:
        return RedirectResponse(f"/admin/tables?floor={floor}", status_code=303)

    dupes = {n for n in number if number.count(n) > 1}
    if dupes:
        raise HTTPException(
            400, f"Table number {sorted(dupes)[0]} appears on more than one row."
        )

    changed = 0
    try:
        for tid, num, zid, cap, wid in zip(
            table_id, number, zone_id, capacity, waiter_id
        ):
            table = db.get(RestaurantTable, tid)
            if table is None:
                raise HTTPException(404, f"Table id {tid} not found")
            if _apply_table_edit(db, table, num, zid, cap, wid):
                changed += 1
            db.flush()          # no autoflush: later rows must see earlier ones
        db.commit()
    except Exception:
        db.rollback()
        raise

    return RedirectResponse(
        f"/admin/tables?floor={floor}&done={changed}", status_code=303
    )


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


@router.post("/tables/assign-zone-waiter")
def assign_zone_waiter(
    zone_id: int = Form(...),
    waiter_id: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Set (or clear) the covering waiter for every active table in a zone.

    A shift-start shortcut: hand a whole section to one server instead of
    setting each table by hand. Live orders on those tables move with the
    waiter, exactly as the per-table assignment does (see _assign_waiter).
    An invalid or inactive waiter rejects the whole batch before any change.
    """
    zone = _zone_or_400(db, zone_id)
    tables = db.execute(
        select(RestaurantTable).where(
            RestaurantTable.zone_id == zone.id,
            RestaurantTable.is_active.is_(True),
        ).order_by(RestaurantTable.number)
    ).scalars().all()
    for table in tables:
        _assign_waiter(db, table, waiter_id)
    db.commit()
    return RedirectResponse(
        f"/admin/tables?floor={zone.floor_id}&done={len(tables)}", status_code=303
    )


@router.post("/zones/{zone_id}/set-tables")
def set_zone_tables(
    zone_id: int,
    count: int = Form(...),
    capacity: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Declarative zone size (auto-commit, no button): make the zone hold
    exactly `count` tables of `capacity` seats. A shortfall is added on free
    grid squares (_free_cells, same placement as a single Add). A surplus is
    retired newest-first — soft only, same as every other table removal here,
    so no service history is ever lost — but ONLY from tables that are FREE
    with no live order (same guard as the single-table and bulk retire
    routes). If reducing to `count` would require touching an occupied one,
    the whole call is rejected and nothing changes — never a partial retire
    that could pick the wrong table. Capacity likewise only ever applies to a
    table that is free; an occupied table is left untouched, seated party and
    all. AJAX endpoint — returns 204."""
    if not 0 <= count <= 50:
        raise HTTPException(400, "Tables must be between 0 and 50.")
    if not 1 <= capacity <= 20:
        raise HTTPException(400, "Seats must be between 1 and 20.")
    zone = _zone_or_400(db, zone_id)
    active = db.execute(
        select(RestaurantTable).where(
            RestaurantTable.zone_id == zone.id, RestaurantTable.is_active.is_(True)
        )
    ).scalars().all()
    current = len(active)
    free = [t for t in active
            if t.status == TableStatus.FREE and not _live_order_for_table(db, t.id)]

    if count < current:
        surplus = current - count
        if len(free) < surplus:
            raise HTTPException(
                400,
                f"Only {len(free)} of this zone's {current} tables are free — "
                f"seat or clear the rest before reducing to {count}."
            )
        for t in sorted(free, key=lambda t: t.id, reverse=True)[:surplus]:
            t.is_active = False                      # soft-retire only; keeps history

    for t in free:
        if t.is_active:                              # skip the ones just retired above
            t.capacity = capacity

    if count > current:
        _add_tables(db, zone, count - current, capacity)
    db.commit()
    return Response(status_code=204)


@router.post("/tables/layout")
def save_layout(
    layout: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Persist grid coordinates and shape from the drag editor.

    Payload is 'id:x:y' triples, each with an optional ':shape' suffix,
    separated by commas — a hidden field on a plain form post, so the editor
    needs script only for the dragging itself and the save path stays the
    same POST-redirect-GET as every other screen here.
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
    shapes = {"round", "square", "rect"}
    for chunk in layout.split(","):
        if not chunk.strip():
            continue
        parts = chunk.split(":")
        if len(parts) not in (3, 4) or not all(p.strip().lstrip("-").isdigit() for p in parts[:3]):
            raise HTTPException(400, f"Malformed layout entry '{chunk}'.")
        tid, x, y = (int(p) for p in parts[:3])
        table = tables.get(tid)
        if table is None:
            raise HTTPException(400, f"Unknown table id {tid} in layout.")
        if not (0 <= x < GRID_COLS and 0 <= y):
            raise HTTPException(400, f"Position {x},{y} is off the grid.")
        if (table.floor_id, x, y) in seen:
            raise HTTPException(400, f"Two tables share position {x},{y}.")
        seen.add((table.floor_id, x, y))
        table.pos_x, table.pos_y = x, y
        if len(parts) == 4 and parts[3].strip() in shapes:
            table.shape = parts[3].strip()
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
    pos_x: int | None = Form(None),
    pos_y: int | None = Form(None),
    width: int | None = Form(None),
    height: int | None = Form(None),
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
    # The zone's rectangle on the floor map (per-mille box, 0..1000). Optional:
    # a plain name/colour edit does not touch it.
    if pos_x is not None:
        zone.pos_x = max(0, min(1000, pos_x))
    if pos_y is not None:
        zone.pos_y = max(0, min(1000, pos_y))
    if width is not None:
        zone.width = max(60, min(1000, width))
    if height is not None:
        zone.height = max(60, min(1000, height))
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

    positions = db.execute(
        select(Position).where(Position.is_active.is_(True)).order_by(Position.sort_order)
    ).scalars().all()
    return render(request, "admin_staff.html", {
        "db": db, "staff": staff,
        "people": [p for p in people if p.is_active],
        "retired": [p for p in people if not p.is_active],
        "open_counts": {
            p.id: sum(1 for o in live if o.waiter_id == p.id) for p in people
        },
        "roles": Role.ALL,
        "positions": positions,
        "role_caps": role_capabilities(),
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


def _wage_cents(raw: str) -> int:
    """Parse a pay-rate in dollars to whole cents (0 or greater)."""
    try:
        return max(0, int(round(float(raw or 0) * 100)))
    except (TypeError, ValueError):
        raise HTTPException(400, "Pay rate must be a number.")


@router.post("/staff/create")
def create_staff(
    name: str = Form(...),
    role: str = Form(...),
    pin_code: str = Form(...),
    position_id: int = Form(0),
    wage: str = Form("0"),
    availability: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("staff.manage")),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name is required.")
    db.add(Staff(
        name=name, role=_check_role(role), pin_code=hash_pin(_check_pin(pin_code)),
        position_id=(position_id or None), wage_cents=_wage_cents(wage),
        availability_note=availability.strip()[:60], is_active=True,
    ))
    db.commit()
    return RedirectResponse("/admin/staff", status_code=303)


def _apply_staff_edit(db: Session, person: Staff, name: str, role: str,
                      pin_code: str, position_id: int = 0, wage: str = "0",
                      availability: str = "") -> None:
    """Apply one person's edits. Shared by the single-row and save-all routes."""
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name is required.")
    role = _check_role(role)

    if person.role == Role.OWNER and role != Role.OWNER:
        _guard_last_owner(db, person, "demote")

    person.name = name
    person.role = role
    person.position_id = position_id or None
    person.wage_cents = _wage_cents(wage)
    person.availability_note = availability.strip()[:60]
    if pin_code.strip():                       # blank means "leave the PIN alone"
        person.pin_code = hash_pin(_check_pin(pin_code))


@router.post("/staff/{staff_id}/edit")
def edit_staff(
    staff_id: int,
    name: str = Form(...),
    role: str = Form(...),
    pin_code: str = Form(""),
    position_id: int = Form(0),
    wage: str = Form("0"),
    availability: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("staff.manage")),
):
    person = db.get(Staff, staff_id)
    if person is None:
        raise HTTPException(404, "Staff member not found")
    _apply_staff_edit(db, person, name, role, pin_code, position_id, wage, availability)
    db.commit()
    return RedirectResponse("/admin/staff", status_code=303)


@router.post("/staff/{staff_id}/photo")
def upload_staff_photo(
    staff_id: int,
    photo: UploadFile = File(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("staff.manage")),
):
    """Attach a resized avatar, stored inline as a data-URI (no upload disk)."""
    person = db.get(Staff, staff_id)
    if person is None:
        raise HTTPException(404, "Staff member not found")
    uri = resize_to_data_uri(photo.file.read(), fallback_mime=photo.content_type or "image/png")
    if uri is None:
        raise HTTPException(400, "Please upload a valid image (JPG/PNG/WEBP).")
    person.photo = uri
    db.commit()
    return RedirectResponse("/admin/staff", status_code=303)


@router.post("/staff/{staff_id}/photo/remove")
def remove_staff_photo(
    staff_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("staff.manage")),
):
    person = db.get(Staff, staff_id)
    if person is not None:
        person.photo = None
        db.commit()
    return RedirectResponse("/admin/staff", status_code=303)


@router.post("/staff/save-all")
def save_all_staff(
    staff_id: list[int] = Form([]),
    name: list[str] = Form([]),
    role: list[str] = Form([]),
    pin_code: list[str] = Form([]),
    position_id: list[int] = Form([]),
    wage: list[str] = Form([]),
    availability: list[str] = Form([]),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("staff.manage")),
):
    """Save every staff row on the page at once.

    Fields arrive as parallel lists in row order; a length mismatch means the
    form did not submit completely and would pair one person's value with
    another, so the whole batch is refused. All-or-nothing: one bad row rolls
    the lot back, so the operator can always tell which of their edits stuck.
    """
    if len({len(staff_id), len(name), len(role), len(pin_code),
            len(position_id), len(wage), len(availability)}) != 1:
        raise HTTPException(400, "The form did not submit completely — reload and retry.")
    try:
        for sid, nm, rl, pin, pos, wg, av in zip(
            staff_id, name, role, pin_code, position_id, wage, availability
        ):
            person = db.get(Staff, sid)
            if person is None:
                raise HTTPException(404, f"Staff id {sid} not found")
            _apply_staff_edit(db, person, nm, rl, pin, pos, wg, av)
            db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
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


@router.post("/staff/{staff_id}/color")
def set_staff_color(
    staff_id: int,
    color: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("staff.manage")),
):
    """Set a staff member's floor-plan colour (AJAX; 204). Empty/invalid clears
    it, falling back to the palette default (Staff.swatch)."""
    person = db.get(Staff, staff_id)
    if person is not None:
        person.color = color if (len(color) == 7 and color.startswith("#")) else None
        db.commit()
    return Response(status_code=204)


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

@router.get("/settings")
def settings_page(
    request: Request,
    saved: int = 0,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    return render(request, "admin_settings.html", {
        "db": db, "staff": staff,
        "settings": settings_svc.all_settings(db),
        "saved": saved,
        "title": "Settings",
    })


@router.post("/settings")
def save_settings(
    request: Request,
    gst_rate: str = Form("0"),
    gst_number: str = Form(""),
    pst_rate: str = Form("0"),
    pst_number: str = Form(""),
    auto_gratuity_party: str = Form("0"),
    auto_gratuity_rate: str = Form("0"),
    service_charge_rate: str = Form("0"),
    card_surcharge_rate: str = Form("0"),
    schedule_start_hour: str = Form("8"),
    schedule_end_hour: str = Form("23"),
    labor_pct_target: str = Form("30"),
    biz_name: str = Form(""),
    biz_address: str = Form(""),
    biz_postal: str = Form(""),
    biz_phone: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    # Every percentage/count on the form must be a number, 0 or greater.
    for label, raw in (
        ("GST", gst_rate), ("PST", pst_rate),
        ("Auto-gratuity party size", auto_gratuity_party),
        ("Auto-gratuity rate", auto_gratuity_rate),
        ("Service charge", service_charge_rate),
        ("Card fee", card_surcharge_rate),
        ("Labor target", labor_pct_target),
    ):
        try:
            if float(raw) < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(400, f"{label} must be a number, 0 or greater.")

    # Schedule window: whole hours. An end at/before the start is an overnight
    # window (e.g. 7 to 4 runs into the next morning), so only the ranges matter.
    try:
        sh_start, sh_end = int(schedule_start_hour), int(schedule_end_hour)
    except ValueError:
        raise HTTPException(400, "Schedule hours must be whole numbers.")
    if not (0 <= sh_start <= 23 and 1 <= sh_end <= 24):
        raise HTTPException(400, "Schedule 'day starts' must be 0–23 and 'day ends' 1–24.")

    settings_svc.save(db, {
        "gst_rate": gst_rate, "gst_number": gst_number,
        "pst_rate": pst_rate, "pst_number": pst_number,
        "auto_gratuity_party": auto_gratuity_party,
        "auto_gratuity_rate": auto_gratuity_rate,
        "service_charge_rate": service_charge_rate,
        "card_surcharge_rate": card_surcharge_rate,
        "schedule_start_hour": str(sh_start),
        "schedule_end_hour": str(sh_end),
        "labor_pct_target": labor_pct_target,
        "biz_name": biz_name, "biz_address": biz_address,
        "biz_postal": biz_postal, "biz_phone": biz_phone,
    })
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


# --------------------------------------------------------------------------
# Schedule positions (name + colour) — used to colour-code the shift calendar.
# --------------------------------------------------------------------------

@router.get("/positions")
def positions_page(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    positions = db.execute(
        select(Position).order_by(Position.is_active.desc(), Position.sort_order, Position.name)
    ).scalars().all()
    return render(request, "admin_positions.html", {
        "db": db, "staff": staff, "positions": positions,
        "title": "Positions",
    })


@router.post("/positions")
def add_position(
    name: str = Form(...),
    color: str = Form("#3b82f6"),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "A position needs a name.")
    if db.execute(select(Position).where(func.lower(Position.name) == name.lower())).scalars().first():
        raise HTTPException(400, f"A position named '{name}' already exists.")
    nxt = (db.execute(select(func.coalesce(func.max(Position.sort_order), 0))).scalar() or 0) + 1
    db.add(Position(name=name, color=color or "#3b82f6", sort_order=nxt))
    db.commit()
    return RedirectResponse("/admin/positions", status_code=303)


@router.post("/positions/{position_id}/edit")
def edit_position(
    position_id: int,
    name: str = Form(...),
    color: str = Form("#3b82f6"),
    is_active: int = Form(0),          # unchecked box is omitted → inactive
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    pos = db.get(Position, position_id)
    if pos is None:
        raise HTTPException(404, "Position not found.")
    pos.name = name.strip() or pos.name
    pos.color = color or pos.color
    pos.is_active = bool(is_active)
    db.commit()
    return RedirectResponse("/admin/positions", status_code=303)


@router.post("/positions/{position_id}/delete")
def delete_position(
    position_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Delete if unused, otherwise just deactivate so existing shifts keep their
    colour and the position stops appearing in the pickers."""
    pos = db.get(Position, position_id)
    if pos is None:
        return RedirectResponse("/admin/positions", status_code=303)
    in_use = db.execute(
        select(func.count()).select_from(Shift).where(Shift.position_id == position_id)
    ).scalar()
    if in_use:
        pos.is_active = False
    else:
        db.delete(pos)
    db.commit()
    return RedirectResponse("/admin/positions", status_code=303)


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
        select(MenuItem).options(selectinload(MenuItem.station)).order_by(MenuItem.name)
    ).scalars().all()
    modifiers = db.execute(select(Modifier).order_by(Modifier.name)).scalars().all()

    by_cat = {c.id: [] for c in categories}
    for item in items:
        by_cat.setdefault(item.category_id, []).append(item)

    # Active PRODUCTION stations for the per-item "Kitchen routing" dropdown —
    # coordination stations don't prepare items, so they're not route targets.
    stations = db.execute(
        select(Station).where(
            Station.is_active.is_(True), Station.type == StationType.PRODUCTION
        ).order_by(Station.display_order, Station.name)
    ).scalars().all()

    return render(request, "admin_menu.html", {
        "db": db, "staff": staff,
        "categories": categories, "by_cat": by_cat, "modifiers": modifiers,
        "stations": stations,
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
    station_id: int = Form(0),
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
        station_id=_routed_station(db, station_id),
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
    station_id: int = Form(0),
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
    # Kitchen routing (Stage A). Changing this affects only lines fired AFTER
    # the change — already-fired OrderItems keep their snapshotted station.
    item.station_id = _routed_station(db, station_id)
    db.commit()
    return RedirectResponse("/admin/menu", status_code=303)


def _routed_station(db: Session, station_id: int) -> int | None:
    """Resolve a routing dropdown value: 0 = Unassigned (None); otherwise the id
    of an active PRODUCTION station. Coordination stations (Expo, Delivery)
    coordinate output — they don't prepare items — so they can't be a route
    target. Unknown / inactive / coordination ids are rejected."""
    if not station_id:
        return None
    st = db.get(Station, station_id)
    if st is None or not st.is_active or st.type != StationType.PRODUCTION:
        raise HTTPException(
            400, "Pick an active production station (coordination stations can't prepare items)."
        )
    return station_id


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


# Menu photos are written under web/static and served at /static/img/menu/.
# In the container this directory is bind-mounted to the host, so uploads
# survive rebuilds and can be managed from the host too.
MENU_IMG_DIR = WEB_DIR / "static" / "img" / "menu"
_ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@router.post("/menu/items/{item_id}/image")
def upload_item_image(
    item_id: int,
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Attach a photo to a menu item (4.1.2). Falls back to the category emoji
    whenever image_url is empty, so removing a photo just clears the field."""
    item = db.get(MenuItem, item_id)
    if item is None:
        raise HTTPException(404, "Menu item not found")

    ext = Path(image.filename or "").suffix.lower()
    if ext not in _ALLOWED_IMG_EXT:
        raise HTTPException(400, "Please upload a JPG, PNG, WEBP or GIF image.")

    MENU_IMG_DIR.mkdir(parents=True, exist_ok=True)
    # Deterministic name per item so re-uploads overwrite rather than pile up.
    slug = re.sub(r"[^a-z0-9]+", "-", item.name.lower()).strip("-") or f"item{item.id}"
    fname = f"{slug}-{item.id}{ext}"
    # Downscale on the way in so a phone photo doesn't become a multi-MB thumb.
    save_image(image.file, MENU_IMG_DIR / fname)

    item.image_url = f"/static/img/menu/{fname}"
    db.commit()
    return RedirectResponse("/admin/menu", status_code=303)


@router.post("/menu/items/{item_id}/image/clear")
def clear_item_image(
    item_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Drop a photo back to the category emoji. The file is left on disk (it may
    be shared or re-linked); only the reference is cleared."""
    item = db.get(MenuItem, item_id)
    if item is None:
        raise HTTPException(404, "Menu item not found")
    item.image_url = ""
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


# --------------------------------------------------------------------------
# Kitchen Stations (Stage A) — the venue configures its own stations and routes
# menu items to them. No station names are hardcoded; everything is data here.
# --------------------------------------------------------------------------

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _station_counts(db: Session) -> dict[int, int]:
    """Active menu items routed to each station, for the list's badge."""
    rows = db.execute(
        select(MenuItem.station_id, func.count())
        .where(MenuItem.station_id.is_not(None), MenuItem.is_active.is_(True))
        .group_by(MenuItem.station_id)
    ).all()
    return {sid: n for sid, n in rows}


def _render_stations(request, db, staff, import_result=None):
    """Shared render for the Stations page (GET and the bulk-import result)."""
    stations = db.execute(
        select(Station).order_by(Station.is_active.desc(), Station.display_order, Station.name)
    ).scalars().all()
    counts = _station_counts(db)
    # How many active items are still Unassigned — surfaced so routing gaps are
    # never invisible (they'll land in the KDS Unassigned bucket when fired).
    unassigned = db.execute(
        select(func.count()).select_from(MenuItem).where(
            MenuItem.station_id.is_(None), MenuItem.is_active.is_(True)
        )
    ).scalar_one()
    return render(request, "admin_stations.html", {
        "db": db, "staff": staff,
        "stations": stations, "counts": counts, "unassigned": unassigned,
        "STATION_TYPES": StationType.ALL, "import_result": import_result,
        "title": "Kitchen stations",
    })


@router.get("/stations")
def stations_page(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Kitchen Stations admin: create/edit/reorder/deactivate + item counts."""
    return _render_stations(request, db, staff)


@router.get("/stations/routing/template")
def export_routing_template(
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Download the routing template: every active item with its category and
    current station. The venue edits the `station` column (exact station name,
    or blank/Unassigned) and re-uploads it to bulk-route the whole menu."""
    rows = db.execute(
        select(MenuItem, MenuCategory.name, Station.name)
        .join(MenuCategory, MenuCategory.id == MenuItem.category_id)
        .join(Station, Station.id == MenuItem.station_id, isouter=True)
        .where(MenuItem.is_active.is_(True))
        .order_by(MenuCategory.sort_order, MenuCategory.name, MenuItem.name)
    ).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["item_id", "category", "item", "price", "station"])
    for item, cat_name, st_name in rows:
        w.writerow([item.id, cat_name, item.name, f"{item.price_cents / 100:.2f}", st_name or ""])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=menu-routing-template.csv"},
    )


# The routing import treats the upload as untrusted. Hard caps so a malformed or
# malicious file (zip bomb, runaway sheet) can't exhaust memory or wedge a worker.
_IMPORT_MAX_UPLOAD = 5 * 1024 * 1024        # 5 MB raw file
_XLSX_MAX_ENTRIES = 64                       # files inside the .xlsx zip
_XLSX_MAX_UNCOMPRESSED = 40 * 1024 * 1024    # total inflated bytes (anti zip-bomb)
_XLSX_MAX_ENTRY = 20 * 1024 * 1024           # inflated bytes per entry
_IMPORT_MAX_ROWS = 20000                     # data rows processed
_IMPORT_MAX_COLS = 64                        # columns read per row (ignore beyond)
_IMPORT_MAX_STR = 200                        # cell string length kept


def _parse_item_id(raw: str) -> int | None:
    """Strict positive-integer id. Accepts '12' and Excel's '12.0', but rejects
    '12.5', scientific notation, signs and anything else — so a fuzzy value can
    never silently resolve to a different, valid item id."""
    raw = (raw or "").strip()
    if re.fullmatch(r"\d+", raw):
        val = int(raw)
    elif re.fullmatch(r"\d+\.0+", raw):          # Excel often stores ints as "12.0"
        val = int(raw.split(".")[0])
    else:
        return None
    return val if val > 0 else None


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx_col_index(ref: str) -> int:
    """A1-style cell ref → 0-based column index ('B3' → 1)."""
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
        if idx > 1_000_000:                       # absurd ref — stop, don't overflow
            break
    return idx - 1


def _read_xlsx_rows(raw: bytes) -> list[list[str]]:
    """Read the first worksheet of an .xlsx as rows of strings — stdlib only, no
    openpyxl. Handles shared and inline strings; enough for the routing template
    (we only need the item_id and station columns)."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    infos = z.infolist()
    # Anti zip-bomb: bound entry count and inflated size before reading anything.
    if len(infos) > _XLSX_MAX_ENTRIES:
        raise ValueError("workbook has too many internal files")
    if (sum(i.file_size for i in infos) > _XLSX_MAX_UNCOMPRESSED
            or any(i.file_size > _XLSX_MAX_ENTRY for i in infos)):
        raise ValueError("workbook is too large to read safely")

    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{_XLSX_NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t"))[:_IMPORT_MAX_STR])
    sheet = next((n for n in sorted(z.namelist())
                  if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")), None)
    if sheet is None:
        return []
    root = ET.fromstring(z.read(sheet))
    rows: list[list[str]] = []
    for row in root.iter(f"{_XLSX_NS}row"):
        if len(rows) >= _IMPORT_MAX_ROWS + 1:      # +1 for a header row
            break
        cells: dict[int, str] = {}
        maxcol = -1
        for c in row.findall(f"{_XLSX_NS}c"):
            col = _xlsx_col_index(c.get("r", "A"))
            if col < 0 or col > _IMPORT_MAX_COLS:  # ignore cells beyond the cap
                continue
            ctype = c.get("t")
            if ctype == "s":
                v = c.find(f"{_XLSX_NS}v")
                try:
                    val = shared[int(v.text)] if v is not None and v.text else ""
                except (ValueError, IndexError):
                    val = ""
            elif ctype == "inlineStr":
                node = c.find(f"{_XLSX_NS}is")
                val = ("".join(t.text or "" for t in node.iter(f"{_XLSX_NS}t"))
                       if node is not None else "")
            else:
                v = c.find(f"{_XLSX_NS}v")
                val = v.text if v is not None and v.text is not None else ""
            cells[col] = (val or "")[:_IMPORT_MAX_STR]
            maxcol = max(maxcol, col)
        rows.append([cells.get(i, "") for i in range(maxcol + 1)])
    return rows


@router.post("/stations/routing/import")
def import_routing(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Apply a filled routing template (CSV or .xlsx). Reads `item_id` and
    `station` (by header, or the first two columns if headerless). Matches the
    station by name (case-insensitive, active); blank / "unassigned" clears
    routing. Nothing destructive: unknown stations and missing items are
    reported, not applied."""
    # Untrusted upload — bound the raw size before reading it all into memory.
    raw = file.file.read(_IMPORT_MAX_UPLOAD + 1)
    if len(raw) > _IMPORT_MAX_UPLOAD:
        return _render_stations(request, db, staff,
                                {"error": "File too large (max 5 MB)."})
    name = (file.filename or "").lower()
    if name.endswith(".xlsx") or raw[:2] == b"PK":
        try:
            rows = [r for r in _read_xlsx_rows(raw) if any((c or "").strip() for c in r)]
        except Exception:  # noqa: BLE001 — a bad/oversized workbook shouldn't 500
            return _render_stations(request, db, staff, {
                "error": "Couldn't read that Excel file (unreadable or too large). "
                         "Re-save it as CSV (File → Save As → CSV) and upload again."})
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        rows = [r for r in csv.reader(io.StringIO(text)) if any((c or "").strip() for c in r)]
    if not rows:
        return _render_stations(request, db, staff,
                                {"error": "The file was empty."})

    # Locate the id / station columns from a header, tolerant of spacing/case
    # ("Item ID", "item_id", "ItemId" all match). Falls back to the first two
    # columns only when there's no recognisable header.
    norm = [re.sub(r"[^a-z0-9]", "", (c or "").strip().lower()) for c in rows[0]]
    i_id = next((k for k, v in enumerate(norm) if v in ("itemid", "id")), None)
    i_st = next((k for k, v in enumerate(norm) if v == "station"), None)
    if i_id is not None and i_st is not None:
        body = rows[1:]
    else:
        i_id, i_st = 0, 1
        body = rows

    # Route only to active PRODUCTION stations — coordination stations (Expo,
    # Delivery) don't prepare items, so they're never a valid routing target.
    # Keyed by the SAME normalisation the duplicate-name check uses.
    by_name = {
        _normalize_station_name(s.name): s.id
        for s in db.execute(
            select(Station).where(
                Station.is_active.is_(True), Station.type == StationType.PRODUCTION
            )
        ).scalars().all()
    }

    result = {"rows": 0, "assigned": 0, "cleared": 0, "unchanged": 0,
              "missing_items": 0, "bad_rows": 0, "truncated": False, "unknown": {}}
    if len(body) > _IMPORT_MAX_ROWS:
        result["truncated"] = True
        body = body[:_IMPORT_MAX_ROWS]
    for r in body:
        if len(r) <= max(i_id, i_st):
            result["bad_rows"] += 1
            continue
        raw_id = (r[i_id] or "").strip()
        st_name = (r[i_st] or "").strip()[:_IMPORT_MAX_STR]
        item_id = _parse_item_id(raw_id)   # strict: '12' or '12.0', never '12.5'
        if item_id is None:
            result["bad_rows"] += 1
            continue
        result["rows"] += 1
        item = db.get(MenuItem, item_id)
        if item is None:
            result["missing_items"] += 1
            continue
        norm_st = _normalize_station_name(st_name)
        if norm_st in ("", "unassigned", "-", "none"):
            target = None
        else:
            target = by_name.get(norm_st)
            if target is None:
                result["unknown"][st_name] = result["unknown"].get(st_name, 0) + 1
                continue
        if item.station_id == target:
            result["unchanged"] += 1
        else:
            item.station_id = target
            if target is None:
                result["cleared"] += 1
            else:
                result["assigned"] += 1
    db.commit()
    return _render_stations(request, db, staff, result)


def _normalize_station_name(value: str) -> str:
    """The single station-name comparison key used everywhere (duplicate check
    AND bulk-import matching), so both agree on when two names are the same:
    trim + Unicode casefold. Storage keeps the original casing untouched."""
    return (value or "").strip().casefold()


def _station_name_clash(db: Session, name: str, exclude_id: int | None = None) -> bool:
    """True if another station already has this name after normalisation — so
    'Grill' and 'grill' can't both exist and make bulk-import matching ambiguous."""
    norm = _normalize_station_name(name)
    if not norm:
        return False
    rows = db.execute(select(Station.id, Station.name)).all()
    return any(
        sid != exclude_id and _normalize_station_name(nm) == norm
        for sid, nm in rows
    )


def _station_form(
    name: str, type_: str, color: str, icon: str, code: str, description: str,
    target_prep_min: str, printer_ref: str, kds_ref: str,
    visible_to_foh: int, visible_to_kitchen: int, station: Station,
) -> None:
    """Validate + apply the shared create/edit fields onto `station`."""
    name = name.strip()
    if not name:
        raise HTTPException(400, "Station name is required.")
    if type_ not in StationType.ALL:
        raise HTTPException(400, "Invalid station type.")
    color = (color or "").strip() or "#64748b"
    if not _HEX_RE.match(color):
        raise HTTPException(400, "Colour must be a #rrggbb hex value.")
    prep = (target_prep_min or "").strip()
    if prep:
        try:
            prep_val = int(prep)
        except ValueError:
            raise HTTPException(400, "Target prep must be a whole number of minutes.")
        if prep_val < 0:
            raise HTTPException(400, "Target prep can't be negative.")
        station.target_prep_min = prep_val
    else:
        station.target_prep_min = None

    station.name = name
    station.type = type_
    station.color = color
    station.icon = (icon or "").strip()[:8]
    station.code = (code or "").strip()[:12]
    station.description = (description or "").strip()[:200]
    # Coordination stations don't run a printer/prep line; keep those fields off.
    if type_ == StationType.COORDINATION:
        station.target_prep_min = None
        station.printer_ref = ""
    else:
        station.printer_ref = (printer_ref or "").strip()[:60]
    station.kds_ref = (kds_ref or "").strip()[:60]
    station.visible_to_foh = bool(visible_to_foh)
    station.visible_to_kitchen = bool(visible_to_kitchen)


@router.post("/stations/create")
def create_station(
    name: str = Form(...),
    type: str = Form(StationType.PRODUCTION),
    color: str = Form("#64748b"),
    icon: str = Form(""),
    code: str = Form(""),
    description: str = Form(""),
    target_prep_min: str = Form(""),
    printer_ref: str = Form(""),
    kds_ref: str = Form(""),
    visible_to_foh: int = Form(0),
    visible_to_kitchen: int = Form(1),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    if _station_name_clash(db, name):
        raise HTTPException(400, f"A station named “{name.strip()}” already exists.")
    station = Station()
    _station_form(name, type, color, icon, code, description, target_prep_min,
                  printer_ref, kds_ref, visible_to_foh, visible_to_kitchen, station)
    # New station goes to the end of the order.
    last = db.execute(select(func.max(Station.display_order))).scalar_one()
    station.display_order = (last or 0) + 1
    db.add(station)
    db.commit()
    return RedirectResponse("/admin/stations", status_code=303)


@router.post("/stations/{station_id}/edit")
def edit_station(
    station_id: int,
    name: str = Form(...),
    type: str = Form(StationType.PRODUCTION),
    color: str = Form("#64748b"),
    icon: str = Form(""),
    code: str = Form(""),
    description: str = Form(""),
    target_prep_min: str = Form(""),
    printer_ref: str = Form(""),
    kds_ref: str = Form(""),
    visible_to_foh: int = Form(0),
    visible_to_kitchen: int = Form(1),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(404, "Station not found")
    if _station_name_clash(db, name, exclude_id=station_id):
        raise HTTPException(400, f"A station named “{name.strip()}” already exists.")
    _station_form(name, type, color, icon, code, description, target_prep_min,
                  printer_ref, kds_ref, visible_to_foh, visible_to_kitchen, station)
    db.commit()
    return RedirectResponse("/admin/stations", status_code=303)


@router.post("/stations/{station_id}/move")
def move_station(
    station_id: int,
    dir: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Reorder by swapping display_order with the adjacent active station."""
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(404, "Station not found")
    ordered = db.execute(
        select(Station).where(Station.is_active.is_(True))
        .order_by(Station.display_order, Station.name)
    ).scalars().all()
    idx = next((i for i, s in enumerate(ordered) if s.id == station_id), None)
    if idx is not None:
        swap = idx - 1 if dir == "up" else idx + 1
        if 0 <= swap < len(ordered):
            a, b = ordered[idx], ordered[swap]
            a.display_order, b.display_order = b.display_order, a.display_order
            db.commit()
    return RedirectResponse("/admin/stations", status_code=303)


@router.post("/stations/{station_id}/active")
def set_station_active(
    station_id: int,
    active: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Activate, or deactivate — deactivation reassigns any routed items to
    Unassigned so a hidden station never silently orphans a menu item. The
    confirmation (with the count) happens in the UI before this is called."""
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(404, "Station not found")
    if active:
        station.is_active = True
    else:
        station.is_active = False
        # Explicitly un-route items pointing here — they become Unassigned.
        db.execute(
            update(MenuItem)
            .where(MenuItem.station_id == station_id)
            .values(station_id=None)
        )
    db.commit()
    return RedirectResponse("/admin/stations", status_code=303)


# --------------------------------------------------------------------------
# Day menus (prix fixe) — the manager composes a fixed-price combo (4.1)
# --------------------------------------------------------------------------

def _parse_schedule(kind: str, menu_date: str, weekday: str) -> tuple[date | None, int | None]:
    """Resolve the date-or-weekday schedule from the form (exactly one is set)."""
    if kind == "weekday":
        try:
            wd = int(weekday)
        except (TypeError, ValueError):
            raise HTTPException(400, "Choose a weekday.")
        if not (0 <= wd <= 6):
            raise HTTPException(400, "Weekday must be Monday–Sunday.")
        return None, wd
    try:
        d = datetime.strptime(menu_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(400, "Enter a valid date (YYYY-MM-DD).")
    return d, None


def _parse_time(value: str, field: str) -> str | None:
    """A blank stays None; otherwise validate and normalise an "HH:MM" time."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise HTTPException(400, f"Enter a valid {field} time (HH:MM).")


def _parse_offer(
    discount_type: str, price: str, discount_percent: str,
    start_time: str, end_time: str,
) -> tuple[str, int, int | None, str | None, str | None]:
    """Resolve the pricing model (fixed price or % discount) and the optional
    time window. Returns (discount_type, price_cents, percent, start, end)."""
    kind = "percent" if discount_type == "percent" else "fixed"
    percent: int | None = None
    price_cents = 0
    if kind == "percent":
        try:
            percent = int((discount_percent or "").strip())
        except (TypeError, ValueError):
            raise HTTPException(400, "Enter a discount between 1 and 100%.")
        if not (1 <= percent <= 100):
            raise HTTPException(400, "Discount must be between 1 and 100%.")
    else:
        price_cents = _cents(price, "Price")

    start = _parse_time(start_time, "start")
    end = _parse_time(end_time, "end")
    if (start is None) != (end is None):
        raise HTTPException(400, "Set both a start and end time, or neither.")
    if start is not None and start >= end:
        raise HTTPException(400, "The end time must be after the start time.")
    return kind, price_cents, percent, start, end


@router.get("/day-menus")
def day_menus_page(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    # Ordered by the calendar day sequence: weekday menus Monday→Sunday, then any
    # specific-date menus by date. (Nullable weekday sorts inconsistently across
    # SQLite/Postgres, so order in Python.)
    menus = db.execute(select(DayMenu)).scalars().all()
    menus.sort(key=lambda m: (
        0 if m.weekday is not None else 1,
        m.weekday if m.weekday is not None else 0,
        m.menu_date or date.max,
        m.name.lower(),
    ))
    items = db.execute(
        select(MenuItem).where(MenuItem.is_active.is_(True)).order_by(MenuItem.name)
    ).scalars().all()
    # Group items by their day-menu slot so the builder only offers, say, drinks
    # under Drink. Passed to the page as JSON for the course→item picker.
    items_by_course: dict[int, list] = {slot: [] for slot in DAY_MENU_COURSES}
    for it in items:
        slot = day_menu_course_for_category(it.category.name if it.category else "")
        items_by_course[slot].append([it.id, f"{it.name} — ${it.price_cents / 100:,.2f}"])
    return render(request, "admin_day_menus.html", {
        "db": db, "staff": staff, "menus": menus,
        "items_by_course": items_by_course,
        "courses": DAY_MENU_COURSES, "course_label": day_menu_course_label,
        "today": datetime.now().date().isoformat(),
        "title": "Day menus",
    })


@router.post("/day-menus/create")
def create_day_menu(
    name: str = Form(...),
    price: str = Form("0"),
    discount_type: str = Form("fixed"),
    discount_percent: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    schedule_kind: str = Form("date"),
    menu_date: str = Form(""),
    weekday: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "A day-menu name is required.")
    d, wd = _parse_schedule(schedule_kind, menu_date, weekday)
    kind, price_cents, percent, start, end = _parse_offer(
        discount_type, price, discount_percent, start_time, end_time)
    db.add(DayMenu(
        name=name, price_cents=price_cents, discount_type=kind,
        discount_percent=percent, start_time=start, end_time=end,
        menu_date=d, weekday=wd, is_active=True,
    ))
    db.commit()
    return RedirectResponse("/admin/day-menus", status_code=303)


@router.post("/day-menus/{menu_id}/edit")
def edit_day_menu(
    menu_id: int,
    name: str = Form(...),
    price: str = Form("0"),
    discount_type: str = Form("fixed"),
    discount_percent: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    schedule_kind: str = Form("date"),
    menu_date: str = Form(""),
    weekday: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    dm = db.get(DayMenu, menu_id)
    if dm is None:
        raise HTTPException(404, "Day menu not found.")
    name = name.strip()
    if not name:
        raise HTTPException(400, "A day-menu name is required.")
    d, wd = _parse_schedule(schedule_kind, menu_date, weekday)
    kind, price_cents, percent, start, end = _parse_offer(
        discount_type, price, discount_percent, start_time, end_time)
    dm.name = name
    dm.price_cents = price_cents
    dm.discount_type = kind
    dm.discount_percent = percent
    dm.start_time = start
    dm.end_time = end
    dm.menu_date = d
    dm.weekday = wd          # active is controlled by the toggle switch, not here
    db.commit()
    return RedirectResponse("/admin/day-menus", status_code=303)


@router.post("/day-menus/{menu_id}/active")
def toggle_day_menu(
    menu_id: int,
    active: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    dm = db.get(DayMenu, menu_id)
    if dm is None:
        raise HTTPException(404, "Day menu not found.")
    dm.is_active = bool(active)
    db.commit()
    return RedirectResponse("/admin/day-menus", status_code=303)


@router.post("/day-menus/{menu_id}/delete")
def delete_day_menu(
    menu_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    dm = db.get(DayMenu, menu_id)
    if dm is not None:
        db.delete(dm)          # cascades to its choices
        db.commit()
    return RedirectResponse("/admin/day-menus", status_code=303)


@router.post("/day-menus/{menu_id}/choices/add")
def add_day_menu_choice(
    menu_id: int,
    course: int = Form(...),
    menu_item_id: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    dm = db.get(DayMenu, menu_id)
    if dm is None:
        raise HTTPException(404, "Day menu not found.")
    if course not in DAY_MENU_COURSES:
        raise HTTPException(400, "Unknown course.")
    if db.get(MenuItem, menu_item_id) is None:
        raise HTTPException(404, "Menu item not found.")
    # Don't add the same item to the same course twice.
    if not any(c.course == course and c.menu_item_id == menu_item_id for c in dm.choices):
        n = sum(1 for c in dm.choices if c.course == course)
        db.add(DayMenuChoice(
            day_menu_id=dm.id, course=course, menu_item_id=menu_item_id, sort_order=n,
        ))
        db.commit()
    return RedirectResponse("/admin/day-menus", status_code=303)


@router.post("/day-menus/choices/{choice_id}/remove")
def remove_day_menu_choice(
    choice_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    c = db.get(DayMenuChoice, choice_id)
    if c is not None:
        db.delete(c)
        db.commit()
    return RedirectResponse("/admin/day-menus", status_code=303)


# Happy hour (services/happyhour) — a time-boxed automatic per-item discount,
# separate from the prix-fixe day menu. The manager sets when it runs and which
# categories / items it discounts, and the order screen applies it at Add.

def _parse_happy_hour(weekdays: list[int], date_from: str, date_to: str,
                      start_time: str, end_time: str, grace: int):
    mask = 0
    for d in weekdays:
        if 0 <= d <= 6:
            mask |= (1 << d)
    if mask == 0:
        mask = 127                                   # none ticked = every day
    start_time, end_time = start_time.strip(), end_time.strip()
    if len(start_time) != 5 or len(end_time) != 5:
        raise HTTPException(400, "Start and end time are required (HH:MM).")
    try:
        df = date.fromisoformat(date_from.strip()) if date_from.strip() else None
        dt = date.fromisoformat(date_to.strip()) if date_to.strip() else None
    except ValueError:
        raise HTTPException(400, "Invalid date.")
    if df and dt and dt < df:
        raise HTTPException(400, "The end date is before the start date.")
    return mask, df, dt, start_time, end_time, max(0, grace)


@router.get("/happy-hours")
def happy_hours_page(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    hhs = db.execute(select(HappyHour).order_by(HappyHour.name)).scalars().all()
    cats = db.execute(select(MenuCategory).order_by(MenuCategory.name)).scalars().all()
    items = db.execute(
        select(MenuItem).where(MenuItem.is_active.is_(True)).order_by(MenuItem.name)
    ).scalars().all()
    return render(request, "admin_happy_hours.html", {
        "db": db, "staff": staff, "happy_hours": hhs,
        "categories": cats, "menu_items": items,
        "weekday_names": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "title": "Happy hour",
    })


@router.post("/happy-hours/create")
def create_happy_hour(
    name: str = Form(...),
    weekdays: list[int] = Form(default=[]),
    date_from: str = Form(""),
    date_to: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    grace_minutes: int = Form(10),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "A happy-hour name is required.")
    mask, df, dt, st, et, g = _parse_happy_hour(
        weekdays, date_from, date_to, start_time, end_time, grace_minutes)
    db.add(HappyHour(name=name, weekday_mask=mask, date_from=df, date_to=dt,
                     start_time=st, end_time=et, grace_minutes=g, is_active=True))
    db.commit()
    return RedirectResponse("/admin/happy-hours", status_code=303)


@router.post("/happy-hours/{hh_id}/edit")
def edit_happy_hour(
    hh_id: int,
    name: str = Form(...),
    weekdays: list[int] = Form(default=[]),
    date_from: str = Form(""),
    date_to: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    grace_minutes: int = Form(10),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    h = db.get(HappyHour, hh_id)
    if h is None:
        raise HTTPException(404, "Happy hour not found.")
    name = name.strip()
    if not name:
        raise HTTPException(400, "A happy-hour name is required.")
    h.weekday_mask, h.date_from, h.date_to, h.start_time, h.end_time, h.grace_minutes = (
        _parse_happy_hour(weekdays, date_from, date_to, start_time, end_time, grace_minutes))
    h.name = name
    db.commit()
    return RedirectResponse("/admin/happy-hours", status_code=303)


@router.post("/happy-hours/{hh_id}/active")
def toggle_happy_hour(
    hh_id: int,
    active: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    h = db.get(HappyHour, hh_id)
    if h is None:
        raise HTTPException(404, "Happy hour not found.")
    h.is_active = bool(active)
    db.commit()
    return RedirectResponse("/admin/happy-hours", status_code=303)


@router.post("/happy-hours/{hh_id}/delete")
def delete_happy_hour(
    hh_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    h = db.get(HappyHour, hh_id)
    if h is not None:
        db.delete(h)          # cascades to its item/category targets
        db.commit()
    return RedirectResponse("/admin/happy-hours", status_code=303)


@router.post("/happy-hours/{hh_id}/targets/add")
def add_happy_hour_target(
    hh_id: int,
    category_id: int = Form(...),
    item_id: int = Form(0),          # 0 = discount the whole category
    percent: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """One flow: pick a category, then discount all of it (item_id=0) or a single
    item in it (an item target overrides its category)."""
    h = db.get(HappyHour, hh_id)
    if h is None:
        raise HTTPException(404, "Happy hour not found.")
    pct = max(1, min(100, percent))
    if item_id:
        if db.get(MenuItem, item_id) is None:
            raise HTTPException(404, "Menu item not found.")
        existing = next((t for t in h.items if t.menu_item_id == item_id), None)
        if existing:
            existing.discount_percent = pct
        else:
            db.add(HappyHourItem(happy_hour_id=h.id, menu_item_id=item_id, discount_percent=pct))
    else:
        if db.get(MenuCategory, category_id) is None:
            raise HTTPException(404, "Category not found.")
        existing = next((t for t in h.categories if t.category_id == category_id), None)
        if existing:
            existing.discount_percent = pct
        else:
            db.add(HappyHourCategory(happy_hour_id=h.id, category_id=category_id, discount_percent=pct))
    db.commit()
    return RedirectResponse("/admin/happy-hours", status_code=303)


@router.post("/happy-hours/targets/{kind}/{target_id}/remove")
def remove_happy_hour_target(
    kind: str,
    target_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    model = {"item": HappyHourItem, "category": HappyHourCategory}.get(kind)
    if model is None:
        raise HTTPException(400, "Unknown target kind.")
    t = db.get(model, target_id)
    if t is not None:
        db.delete(t)
        db.commit()
    return RedirectResponse("/admin/happy-hours", status_code=303)
