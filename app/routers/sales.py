"""Sales & Orders — section 4.1."""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.deps import can, current_staff, render, require
from app.models.oltp import (
    ALLERGEN_OPTIONS,
    COURSE_LABELS,
    DAY_MENU_COURSES,
    AuditEvent,
    Channel,
    DayMenu,
    DeliveryOrder,
    DeliveryStatus,
    KitchenStatus,
    course_label,
    day_menu_course_label,
    MenuCategory,
    MenuItem,
    Modifier,
    ModifierOption,
    Order,
    OrderItem,
    OrderItemModifier,
    OrderItemOption,
    OrderStatus,
    PreparationTask,
    PreparationTaskModifier,
    PreparationTaskStatus,
    Payment,
    build_allergens,
    order_status_label,
    RestaurantTable,
    Role,
    Seat,
    SeatStatus,
    SharedItemShare,
    Staff,
    Station,
    TableStatus,
    Zone,
)
from app.services import daymenu, happyhour
from app.services import reservations as reservations_svc
from app.services import settings as settings_svc
from app.services import upsell
from app.services.payments import balance_panel, ensure_seats, set_shared_item_shares

router = APIRouter()


def _next_code(db: Session) -> str:
    n = db.execute(select(func.count()).select_from(Order)).scalar_one()
    return f"ORD-{datetime.now().strftime('%y%m%d')}-{n + 1:05d}"


# --------------------------------------------------------------------------
# 4.1.1  Floor plan
# --------------------------------------------------------------------------


def _order_station_status(order: "Order") -> list[dict]:
    """Compact per-station readiness for a table card (Kitchen Stations, Stage A).

    Groups the order's fired lines by their station snapshot; each station is
    ready only when all of its lines are. Returns the small breakdown the floor
    card shows (Pizza ✓ · Grill ⏳ · Bar ✓) — display only, it never changes the
    card's aggregate status.
    """
    groups: dict = {}
    for i in order.items:
        if i.kitchen_status in (KitchenStatus.PREPARING, KitchenStatus.READY):
            groups.setdefault(i.station_id, []).append(i)
    out = []
    for _sid, items in groups.items():
        st = items[0].station
        out.append({
            "station": st,
            "name": st.name if st else "Unassigned",
            "ready": all(it.kitchen_status == KitchenStatus.READY for it in items),
            "count": sum(it.quantity for it in items),
        })
    out.sort(key=lambda g: (g["station"].display_order if g["station"] else 999, g["name"]))
    return out


@router.get("/")
def floor_plan(request: Request, floor: str = "", db: Session = Depends(get_db), staff: Staff = Depends(current_staff)):
    # Retired tables (admin) keep their history but leave the floor.
    tables = db.execute(
        select(RestaurantTable)
        .where(RestaurantTable.is_active.is_(True))
        .order_by(RestaurantTable.pos_y, RestaurantTable.pos_x, RestaurantTable.number)
    ).scalars().all()

    open_orders = db.execute(
        select(Order).where(
            Order.status.in_(
                (OrderStatus.OPEN, OrderStatus.PREPARING, OrderStatus.READY,
                 OrderStatus.SERVED, OrderStatus.PARTIALLY_PAID)
            ),
            Order.table_id.is_not(None),
        )
        # Card total is sum(item.line_total_cents) — a computed property, so the
        # items (and their modifiers) must be loaded; batch them across all
        # orders instead of one graph load per occupied table. Station is
        # selectinloaded too (the per-station strip reads it, no N+1).
        .options(selectinload(Order.items).selectinload(OrderItem.station))
    ).scalars().all()
    by_table = {o.table_id: o for o in open_orders}

    # Self-heal: a table is only "Ready to pay" once its order is served (or
    # part-paid). Correct any stale flag left from before that rule (e.g. a table
    # flagged then pulled back to Preparing by a re-fire) so the floor never
    # shows "Ready to serve" and "Ready to pay" at once.
    stale = [
        t for t in tables
        if t.status == TableStatus.READY_TO_PAY
        and by_table.get(t.id)
        and by_table[t.id].status not in (OrderStatus.SERVED, OrderStatus.PARTIALLY_PAID)
    ]
    if stale:
        for t in stale:
            t.status = TableStatus.OCCUPIED
        db.commit()

    # Tables a booking is holding right now (its window is open). A free table in
    # this map shows as "reserved" instead of free, and can be seated straight
    # from the map. An already-occupied table with a hold still gets a due-soon
    # reminder (Reservations 4.1.5) — see "reservation" on the card below.
    # Auto-drops any no-show past its window.
    holds = reservations_svc.active_holds(db)

    # Role visibility: managers/owners see every table; a waiter sees only their
    # own tables plus occupied ones no one has claimed yet (which they can pick
    # up by opening the order) — never the free ones. Held tables stay visible to
    # everyone, so a waiter can seat the booking from the map.
    if staff.role not in (Role.OWNER, Role.MANAGER):
        tables = [
            t for t in tables
            if t.current_waiter_id == staff.id
            or (t.status != TableStatus.FREE and t.current_waiter_id is None)
            or t.id in holds
        ]

    cards = []
    for t in tables:
        order = by_table.get(t.id)
        total = sum(i.line_total_cents for i in order.items) if order else 0
        claimed = t.current_waiter is not None
        # Food that's up but not yet served — the waiter's cue to run it. Counts
        # even while the rest of the order is still Preparing, so partial readiness
        # isn't hidden behind the order's aggregate.
        ready_ct = sum(
            i.quantity for i in order.items if i.kitchen_status == KitchenStatus.READY
        ) if order else 0
        # Display status, derived (no schema change) — the icon shown on the card:
        #   seated       occupied but no waiter yet (guests waiting)
        #   ready_serve  a waiter's on it and food is up — RUN it (wins on partial)
        #   served       delivered, guests dining
        #   occupied     order in, kitchen still cooking, nothing up yet
        res = holds.get(t.id)
        if t.status == TableStatus.FREE:
            # A held free table reads as "reserved" (booking incoming), not free.
            disp = "reserved" if res else "free"
        elif t.status == TableStatus.READY_TO_PAY:
            disp = "ready_to_pay"
        elif not claimed:
            disp = "seated"
        elif ready_ct > 0:
            disp = "ready_serve"
        elif order is not None and order.status == OrderStatus.SERVED:
            disp = "served"
        else:
            disp = "occupied"
        cards.append(
            {
                "table": t,
                "order": order,
                "total_cents": total,
                "guests": order.guest_count if order else 0,
                "disp_status": disp,
                "waiter": t.current_waiter.name if t.current_waiter else None,
                "waiter_id": t.current_waiter_id if claimed else None,
                # The responsible waiter's floor-plan colour — the card is coloured
                # by WHO is serving it (None -> unclaimed, a neutral border).
                "waiter_color": (t.current_waiter.swatch
                                 if (claimed and t.current_waiter.is_active) else None),
                "ready_count": ready_ct,
                # Minutes since the guests sat (order opened). On a seated table
                # this is how long they've waited for a waiter — the manager's cue.
                "minutes": (
                    int((datetime.now() - order.opened_at).total_seconds() // 60)
                    if order else 0
                ),
                # The booking holding this table right now (None if not reserved).
                # Carries guest name / time / party for the reserved card, and its
                # id for the one-tap "Seat" straight from the map — set regardless
                # of disp_status, so an occupied table due for a booking still
                # carries the reminder even though its colour stays "occupied".
                "reservation": res,
                # Per-station readiness of what's fired (Pizza ✓ · Grill ⏳ …),
                # shown as a compact strip on the card. Empty when nothing's fired.
                "stations": _order_station_status(order) if order else [],
            }
        )

    def tally(group):
        def n(status):
            return sum(1 for c in group if c["disp_status"] == status)
        return {
            "free": n("free"), "seated": n("seated"), "occupied": n("occupied"),
            "ready_serve": n("ready_serve"), "served": n("served"),
            "ready": n("ready_to_pay"), "reserved": n("reserved"),
        }

    counts = tally(cards)

    # One section per floor rather than one grid of everything: a waiter works a
    # floor, and 50+ cards from three storeys interleaved cannot be scanned.
    # Sections, not tabs — nothing is hidden behind a click mid-service.
    def zones_of(group):
        # Distinct zones present in the section, ordered as on the floor plan —
        # for the colour key. zone_ref carries the swatch each card is dotted with.
        seen: dict[int, object] = {}
        for c in group:
            z = c["table"].zone_ref
            if z and z.id not in seen:
                seen[z.id] = z
        return sorted(seen.values(), key=lambda z: (z.sort_order or 0, z.name))

    def waiters_of(group):
        # Distinct waiters covering tables in this section — the colour key that
        # replaces the name on each card. Cards are coloured by their waiter.
        seen: dict[int, object] = {}
        for c in group:
            w = c["table"].current_waiter
            if w and w.is_active and w.id not in seen:
                seen[w.id] = w
        return sorted(seen.values(), key=lambda w: w.name)

    def zone_groups(group):
        # Cards bucketed by zone, each with its table + seat totals, for the
        # bordered per-zone panels in the list view. Ordered as on the floor.
        buckets: dict[int, dict] = {}
        for c in group:
            z = c["table"].zone_ref
            key = z.id if z else 0
            b = buckets.setdefault(key, {"zone": z, "cards": [], "tables": 0, "seats": 0})
            b["cards"].append(c)
            b["tables"] += 1
            b["seats"] += c["table"].capacity
        return sorted(
            buckets.values(),
            key=lambda b: (b["zone"].sort_order if b["zone"] else 999,
                           b["zone"].name if b["zone"] else "zzz"),
        )

    sections = []
    for fl in sorted(
        {c["table"].floor for c in cards if c["table"].floor},
        key=lambda f: (f.sort_order, f.name),
    ):
        group = [c for c in cards if c["table"].floor_id == fl.id]
        sections.append({"floor": fl, "cards": group, "counts": tally(group),
                         "zones": zones_of(group), "waiters": waiters_of(group),
                         "zone_groups": zone_groups(group)})

    # A table whose zone was removed would otherwise vanish from the floor.
    homeless = [c for c in cards if not c["table"].floor]
    if homeless:
        sections.append({"floor": None, "cards": homeless, "counts": tally(homeless),
                         "zones": zones_of(homeless), "waiters": waiters_of(homeless),
                         "zone_groups": zone_groups(homeless)})

    # Every active zone (with its map rectangle) per floor, so the spatial map can
    # draw each zone — including empty ones — behind its tables.
    zones_by_floor: dict[int | None, list] = {}
    for z in db.execute(
        select(Zone).where(Zone.is_active.is_(True)).order_by(Zone.sort_order, Zone.name)
    ).scalars().all():
        zones_by_floor.setdefault(z.floor_id, []).append(z)
    for s in sections:
        s["map_zones"] = zones_by_floor.get(s["floor"].id if s["floor"] else None, [])

    # Floors are tabs, not stacked sections (mirrors Manage): a waiter works one
    # floor, and the ?floor= tab picks which. The rest is a tap away, so the
    # heading and the old per-floor filter don't both compete for the same job.
    floor_tabs = [{"id": s["floor"].id if s["floor"] else 0,
                   "name": s["floor"].name if s["floor"] else "No floor"}
                  for s in sections]
    # ?floor=all shows every floor at once; otherwise a numeric id picks one
    # (default: the first). Kept as a string so "all" and ids share one param.
    show_all = floor == "all"
    try:
        sel = int(floor) if floor and not show_all else 0
    except ValueError:
        sel = 0
    current = next((s for s in sections
                    if (s["floor"].id if s["floor"] else 0) == sel),
                   sections[0] if sections else None)
    current_floor_id = current["floor"].id if current and current["floor"] else 0
    # Every floor is rendered (so the search can reach any table); the page shows
    # one at a time via tabs, or all at once. Header/status counts start on the
    # shown scope and the client keeps them in step as you switch or search.
    counts = tally(cards) if show_all else (
        current["counts"] if current else {"free": 0, "occupied": 0, "ready": 0})

    delivery_pending = db.execute(
        select(func.count()).select_from(DeliveryOrder).where(
            DeliveryOrder.status.not_in((DeliveryStatus.DELIVERED,))
        )
    ).scalar_one()

    # Zones on the shown floor — for the "Add table" quick form.
    current_zones = db.execute(
        select(Zone).where(Zone.floor_id == current_floor_id, Zone.is_active.is_(True))
        .order_by(Zone.sort_order, Zone.name)
    ).scalars().all() if current_floor_id else []
    # Every zone, floor and all — the "Add table" form needs the full set on the
    # "All tables" view, where there's no single current floor to imply one.
    all_zones = db.execute(
        select(Zone).where(Zone.is_active.is_(True))
        .order_by(Zone.floor_id, Zone.sort_order, Zone.name)
    ).scalars().all()

    return render(request, "floor.html", {
        "db": db, "staff": staff, "cards": cards, "counts": counts,
        "sections": sections,
        "floor_tabs": floor_tabs, "current_floor_id": current_floor_id,
        "current_zones": current_zones, "all_zones": all_zones,
        "show_all": show_all,
        "floor_card_count": len(cards) if show_all else (len(current["cards"]) if current else 0),
        "delivery_pending": delivery_pending,
        "create_nonce": secrets.token_hex(8),   # one-time token for the Add-table form
        "title": "Floor plan",
    })


@router.post("/tables/create")
def create_table_from_floor(
    floor_id: int = Form(...),
    zone_id: int = Form(...),
    capacity: int = Form(4),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Quick "Add table" from the floor plan: the number is assigned
    automatically, and the new table inherits the waiter already covering its
    zone (so a zone handed to one server keeps new tables on that server)."""
    from app.routers.admin import _add_tables   # lazy: admin imports from here

    zone = db.get(Zone, zone_id)
    if zone is None or zone.floor_id != floor_id:
        raise HTTPException(400, "Choose a zone on this floor.")
    if not 1 <= capacity <= 20:
        raise HTTPException(400, "Seats must be between 1 and 20.")

    # The waiter covering this zone, if its active tables agree on one.
    covering = {
        t.current_waiter_id for t in db.execute(
            select(RestaurantTable).where(
                RestaurantTable.zone_id == zone.id,
                RestaurantTable.is_active.is_(True),
            )
        ).scalars().all() if t.current_waiter_id
    }
    zone_waiter = next(iter(covering)) if len(covering) == 1 else None

    number = _add_tables(db, zone, 1, capacity)[0]
    db.flush()   # autoflush is off; make the new row visible to the query below
    new_table = db.execute(
        select(RestaurantTable).where(RestaurantTable.number == number)
    ).scalar_one()
    new_table.current_waiter_id = zone_waiter
    db.commit()
    return RedirectResponse(f"/?floor={floor_id}", status_code=303)


@router.post("/tables/{table_id}/remove")
def remove_table_from_floor(
    table_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("settings")),
):
    """Retire a table off the floor. Nothing is deleted — the table is kept with
    all its info and history and simply marked inactive, so it drops off the
    floor plan but still lists under Manage → Tables (Retired), where it can be
    restored. A table in service can't be pulled out from under a party."""
    table = db.get(RestaurantTable, table_id)
    if table is None:
        raise HTTPException(404, "Table not found.")
    if table.status != TableStatus.FREE:
        raise HTTPException(400, "Free the table before retiring it.")
    floor_id = table.floor_id or 0

    table.current_waiter_id = None       # same as the Manage retire path
    table.is_active = False
    db.commit()
    return RedirectResponse(f"/?floor={floor_id}", status_code=303)


@router.post("/tables/{table_id}/open")
def open_table(
    table_id: int,
    guests: int = Form(2),
    # Optional: a restaurant with no waiters yet (or a host seating before
    # assignment) can still open the table; 0 means "unassigned".
    waiter_id: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """Workflow 6.1 step 1-2 — host seats guests, order is created."""
    table = db.get(RestaurantTable, table_id)
    if table is None:
        raise HTTPException(404, "Table not found")
    if table.status != TableStatus.FREE:
        raise HTTPException(400, f"Table {table.number} is not free.")

    order = open_order_on_table(db, table, guests, waiter_id)
    db.commit()
    return RedirectResponse(f"/orders/{order.id}", status_code=303)


def open_order_on_table(db: Session, table: RestaurantTable, guests: int, waiter_id: int) -> Order:
    """Create a dine-in order on a free table and occupy it (does not commit).

    Shared by the floor "open table" action and seating a reservation, so both
    paths create the order, its seats and the table state identically.
    """
    wid = waiter_id or None                  # 0 / unset -> no waiter assigned
    channel = db.execute(select(Channel).where(Channel.code == "dine_in")).scalar_one()
    order = Order(
        code=_next_code(db),
        table_id=table.id,
        channel_id=channel.id,
        waiter_id=wid,
        status=OrderStatus.OPEN,
        guest_count=guests,
        opened_at=datetime.now(),
    )
    if wid is not None:
        # A waiter is responsible from the moment they open the table — the
        # seated->attended lead time is 0 here; it only grows past 0 when a
        # table opens unclaimed and reassign_waiter() stamps it later.
        order.attended_at = order.opened_at
    db.add(order)
    db.flush()

    ensure_seats(db, order, guests)          # 4.2.4 — a payer per seat
    table.status = TableStatus.OCCUPIED      # system response: Occupied
    table.current_waiter_id = wid
    return order


@router.post("/tables/{table_id}/waiter")
def reassign_waiter(
    table_id: int,
    waiter_id: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """4.1.1 — assign or reassign a waiter to any table."""
    table = db.get(RestaurantTable, table_id)
    if table is None:
        raise HTTPException(404, "Table not found")
    table.current_waiter_id = waiter_id
    order = db.execute(
        select(Order).where(
            Order.table_id == table_id,
            Order.status.not_in((OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED)),
        )
    ).scalars().first()
    if order:
        order.waiter_id = waiter_id
        if order.attended_at is None:
            order.attended_at = datetime.now()
    db.commit()
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# 4.1.1  Move a party to another table
# --------------------------------------------------------------------------

LIVE_STATES = (
    OrderStatus.OPEN, OrderStatus.PREPARING,
    OrderStatus.READY, OrderStatus.SERVED, OrderStatus.PARTIALLY_PAID,
)


def _covers_tables(db: Session, staff: Staff) -> bool:
    """Whether this person is currently serving any tables — used to default the
    "my tables" filter on for them. Owners, managers and kitchen staff who cover
    none fall back to seeing everything, so the default never hides the board."""
    mine = db.execute(
        select(RestaurantTable.id).where(
            RestaurantTable.is_active.is_(True),
            RestaurantTable.current_waiter_id == staff.id,
        ).limit(1)
    ).first()
    if mine:
        return True
    return db.execute(
        select(Order.id).where(
            Order.waiter_id == staff.id,
            Order.status.in_(LIVE_STATES),
        ).limit(1)
    ).first() is not None


def _live_order(db: Session, table_id: int) -> Order | None:
    """The open order sitting on a table, if any."""
    return db.execute(
        select(Order).where(
            Order.table_id == table_id, Order.status.in_(LIVE_STATES)
        )
    ).scalars().first()


def _audit(db: Session, staff: Staff, action: str, detail: str, order_id: int | None = None) -> None:
    """Append a table-action to the security trail (4.1.1). Append-only."""
    db.add(AuditEvent(staff_id=staff.id, action=action, detail=detail, order_id=order_id))


@router.post("/tables/{table_id}/move")
def move_table(
    table_id: int,
    to_table_id: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """Relocate a seated party's whole order to a different free table.

    The order (with its seats, items and any partial payments) simply changes
    tables — nothing about the bill is touched. The old table is freed and the
    new one takes on the party's status and waiter.
    """
    src = db.get(RestaurantTable, table_id)
    if src is None:
        raise HTTPException(404, "Table not found")
    order = _live_order(db, src.id)
    if order is None:
        raise HTTPException(400, f"Table {src.number} has no open order to move.")

    dest = db.get(RestaurantTable, to_table_id)
    if dest is None:
        raise HTTPException(404, "Destination table not found")
    if dest.id == src.id:
        raise HTTPException(400, "That's the same table.")
    if not dest.is_active or dest.status != TableStatus.FREE:
        raise HTTPException(400, f"Table {dest.number} is not free.")

    order.table_id = dest.id
    dest.status = src.status
    dest.current_waiter_id = src.current_waiter_id
    src.status = TableStatus.FREE
    src.current_waiter_id = None
    _audit(db, staff, "move_table",
           f"{order.code}: Table {src.number} -> Table {dest.number}", order.id)
    db.commit()
    return RedirectResponse(f"/orders/{order.id}", status_code=303)


def _has_paid_items(order: Order) -> bool:
    """True if any of the order's lines are already allocated to a payment."""
    return any(i.allocations for i in order.items)


@router.post("/tables/{table_id}/merge")
def merge_tables(
    table_id: int,
    from_table_id: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """Combine another table's order into this one — two parties onto one bill.

    The source order's guests become extra seats on the destination (renumbered
    after its existing seats), its items move across keeping their seat, course
    and kitchen state, and the emptied table is freed. The destination may
    already be partially paid — its own items and payments are untouched. Only
    the source must be unpaid, since moving a settled item would orphan its
    payment.
    """
    dest = db.get(RestaurantTable, table_id)
    if dest is None:
        raise HTTPException(404, "Table not found")
    target = _live_order(db, dest.id)
    if target is None:
        raise HTTPException(400, f"Table {dest.number} has no open order.")

    src = db.get(RestaurantTable, from_table_id)
    if src is None:
        raise HTTPException(404, "Table to merge not found")
    if src.id == dest.id:
        raise HTTPException(400, "That's the same table.")
    source = _live_order(db, src.id)
    if source is None:
        raise HTTPException(400, f"Table {src.number} has no open order to merge.")

    if _has_paid_items(source):
        raise HTTPException(
            400,
            f"Table {src.number} has already taken payment — settle or void it "
            "before merging it into another table.",
        )

    # Append the source party's seats after the destination's, remembering the
    # old→new mapping so each moved item keeps its seat.
    base = max((s.seat_number for s in target.seats), default=0)
    seat_map: dict[int, int] = {}
    for offset, s in enumerate(sorted(source.seats, key=lambda x: x.seat_number), start=1):
        new_seat = Seat(
            order_id=target.id, seat_number=base + offset,
            label=s.label, status=s.status, tip_cents=s.tip_cents,
        )
        db.add(new_seat)
        db.flush()
        seat_map[s.id] = new_seat.id

    moved = list(source.items)
    for item in moved:
        item.order_id = target.id
        item.seat_id = seat_map.get(item.seat_id) if item.seat_id else None
        # Stamp provenance so the line shows "from Table N" and stays traceable.
        item.merged_from_order_id = source.id

    target.guest_count += source.guest_count
    now = datetime.now()
    if source.sent_to_kitchen_at:
        target.sent_to_kitchen_at = target.sent_to_kitchen_at or source.sent_to_kitchen_at
    _recompute_kitchen(target, now)

    # The absorbed order is dissolved and its table freed.
    source.status = OrderStatus.CANCELLED
    source.closed_at = now
    src.status = TableStatus.FREE
    src.current_waiter_id = None
    _audit(db, staff, "merge_tables",
           f"Table {src.number} ({source.code}, {len(moved)} item(s)) "
           f"merged into Table {dest.number} ({target.code})", target.id)
    db.commit()
    return RedirectResponse(f"/orders/{target.id}", status_code=303)


@router.post("/orders/{order_id}/split")
def split_order(
    order_id: int,
    to_table_id: int = Form(...),
    seat_numbers: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """Peel selected seats (and their items) onto a free table as a new order.

    The reverse of merge: a subset of the party moves to another table. Each
    picked seat and its items relocate to a fresh order there, renumbered from
    1; the rest stay put. Blocked for a seat that's already been paid or that
    shares an item, since either would strand money or a split-item's shares.
    """
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    if order.status in (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED):
        raise HTTPException(400, "This order is closed.")

    dest = db.get(RestaurantTable, to_table_id)
    if dest is None:
        raise HTTPException(404, "Destination table not found")
    if not dest.is_active or dest.status != TableStatus.FREE:
        raise HTTPException(400, f"Table {dest.number} is not free.")

    picked = {n for n in seat_numbers if n}
    seats = [s for s in order.seats if s.seat_number in picked]
    if not seats:
        raise HTTPException(400, "Pick at least one seat to split off.")
    if len(seats) == len(order.seats):
        raise HTTPException(400, "That moves the whole table — use Move table instead.")

    moved_seat_ids = {s.id for s in seats}
    if any(s.status != SeatStatus.OPEN for s in seats):
        raise HTTPException(400, "Settle or void a seat's payment before splitting it.")
    shared = db.execute(
        select(SharedItemShare).where(SharedItemShare.seat_id.in_(moved_seat_ids))
    ).scalars().first()
    if shared is not None:
        raise HTTPException(400, "Un-share shared items before splitting those seats.")

    now = datetime.now()
    new_order = Order(
        code=_next_code(db), table_id=dest.id, channel_id=order.channel_id,
        waiter_id=order.waiter_id, status=OrderStatus.OPEN,
        guest_count=len(seats), opened_at=now,
    )
    db.add(new_order)
    db.flush()

    seat_map: dict[int, int] = {}
    for idx, s in enumerate(sorted(seats, key=lambda x: x.seat_number), start=1):
        new_seat = Seat(
            order_id=new_order.id, seat_number=idx,
            label=s.label, status=s.status, tip_cents=s.tip_cents,
        )
        db.add(new_seat)
        db.flush()
        seat_map[s.id] = new_seat.id

    moving_items = [i for i in order.items if i.seat_id in moved_seat_ids]
    for item in moving_items:
        item.order_id = new_order.id
        item.seat_id = seat_map[item.seat_id]
        item.merged_from_order_id = order.id      # provenance: split from here

    order.guest_count = max(1, order.guest_count - len(seats))
    for s in seats:
        db.delete(s)                              # now empty; items moved off

    if order.sent_to_kitchen_at:
        new_order.sent_to_kitchen_at = order.sent_to_kitchen_at
    _recompute_kitchen(order, now)
    _recompute_kitchen(new_order, now)

    dest.status = TableStatus.OCCUPIED
    dest.current_waiter_id = order.waiter_id
    _audit(db, staff, "split_table",
           f"{order.code}: {len(seats)} seat(s) split to Table {dest.number} "
           f"({new_order.code})", new_order.id)
    db.commit()
    return RedirectResponse(f"/orders/{new_order.id}", status_code=303)


# --------------------------------------------------------------------------
# 4.1.2  Order creation
# --------------------------------------------------------------------------

@router.get("/orders/{order_id}")
def order_screen(
    order_id: int,
    request: Request,
    category: int | None = None,
    seat: int | None = None,
    item: int | None = None,
    hh: int = 0,
    db: Session = Depends(get_db),
    staff: Staff = Depends(current_staff),
):
    # Load the whole order graph up front. Without this, rendering the screen
    # lazy-loads each item's modifiers/options/shares one row at a time — cheap
    # on local SQLite but dozens of network round-trips against remote Postgres,
    # which is what made this page crawl in production. selectinload collapses
    # it to a handful of batched queries regardless of how many items there are.
    order = db.execute(
        select(Order)
        .where(Order.id == order_id)
        .options(
            selectinload(Order.items).selectinload(OrderItem.modifiers),
            selectinload(Order.items).selectinload(OrderItem.options),
            selectinload(Order.items).selectinload(OrderItem.shares),
            # Payment allocations per item — build_ledgers reads item.allocations
            # for every line, which is otherwise one query per item.
            selectinload(Order.items).selectinload(OrderItem.allocations),
            selectinload(Order.seats),
            selectinload(Order.payments).selectinload(Payment.allocations),
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(404, "Order not found")

    # The item currently being configured (its modifier groups drive the panel).
    configuring = None
    if item:
        configuring = db.get(MenuItem, item)
        if configuring and not (configuring.is_active and configuring.available):
            configuring = None

    categories = db.execute(
        select(MenuCategory).order_by(MenuCategory.sort_order)
    ).scalars().all()

    # Happy hour in force right now (services/happyhour) — drives the category-rail
    # stars, the optional "Happy hour" filter view, and the per-item pricing below.
    _now = datetime.now()
    _hh_open = order.status not in (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED)
    _ad = happyhour.load_active(db, _now) if _hh_open else happyhour.ActiveDiscounts()
    hh_cats = set(_ad.cat_pct)
    if _ad.item_pct:
        for (cid,) in db.execute(
            select(MenuItem.category_id).where(MenuItem.id.in_(_ad.item_pct.keys()))
        ):
            hh_cats.add(cid)

    hh_mode = bool(hh) and _ad.any()
    active_cat = None if hh_mode else (category or (categories[0].id if categories else None))
    # Which seat new items are being added for. The waiter takes the order seat
    # by seat, so it defaults to the first seat and auto-advances after each add
    # (see add_item); 0 means the table / unassigned.
    seat_numbers = [s.seat_number for s in order.seats]
    if seat is not None:
        active_seat = seat if seat in seat_numbers else 0
    else:
        active_seat = seat_numbers[0] if seat_numbers else 0
    # 86'd items (available=False) drop off the order screen alongside items the
    # owner has taken off the menu (is_active=False).
    if hh_mode:
        # "Happy hour" filter: every item on an active window right now, across
        # categories (a covered category's items plus any item-level targets).
        covered_ids = set(_ad.item_pct)
        if _ad.cat_pct:
            for (iid,) in db.execute(
                select(MenuItem.id).where(
                    MenuItem.category_id.in_(_ad.cat_pct.keys()),
                    MenuItem.is_active.is_(True), MenuItem.available.is_(True))
            ):
                covered_ids.add(iid)
        items = db.execute(
            select(MenuItem).where(
                MenuItem.id.in_(covered_ids),
                MenuItem.is_active.is_(True), MenuItem.available.is_(True),
            ).order_by(MenuItem.name).options(selectinload(MenuItem.modifier_groups))
        ).scalars().all() if covered_ids else []
    else:
        items = db.execute(
            select(MenuItem).where(
                MenuItem.category_id == active_cat,
                MenuItem.is_active.is_(True), MenuItem.available.is_(True),
            ).order_by(MenuItem.name)
            # The grid shows an "options" badge per item (mi.modifier_groups), which
            # otherwise lazy-loads groups (and their options) one item at a time.
            .options(selectinload(MenuItem.modifier_groups))
        ).scalars().all()

    modifiers = db.execute(select(Modifier).order_by(Modifier.name)).scalars().all()
    panel = balance_panel(db, order)

    # Per-seat summary for the seat cards: how many items and their firing state
    # (empty / ordered but not fired / at least one fired). Shared items sit
    # under the table card, not a seat.
    def _seat_status(its: list) -> str:
        if not its:
            return "empty"
        if any(i.kitchen_status != KitchenStatus.PENDING for i in its):
            return "fired"
        return "ordered"

    seat_cards = []
    for s in order.seats:
        its = [i for i in order.items if i.seat_id == s.id]
        seat_cards.append({
            "seat": s, "count": sum(i.quantity for i in its),
            "status": _seat_status(its),
        })
    shared_items = [i for i in order.items if i.is_shared]
    table_card = {
        "count": sum(i.quantity for i in shared_items),
        "status": _seat_status(shared_items),
    }

    # Order lines grouped by seat for the order panel, in seat order, then the
    # shared/table items, then anything still unassigned.
    # 'lines' not 'items': in Jinja, group.items would resolve to the dict's
    # built-in .items() method, not this list.
    line_groups = []
    for s in order.seats:
        its = [i for i in order.items if i.seat_id == s.id and not i.is_shared]
        if its:
            line_groups.append({"label": f"Seat {s.seat_number}", "num": s.seat_number,
                                "count": sum(i.quantity for i in its), "lines": its})
    if shared_items:
        line_groups.append({"label": "Table", "num": 0,
                            "count": sum(i.quantity for i in shared_items), "lines": shared_items})
    unassigned = [i for i in order.items if i.seat_id is None and not i.is_shared]
    if unassigned:
        line_groups.append({"label": "Unassigned", "num": None,
                            "count": sum(i.quantity for i in unassigned), "lines": unassigned})

    # Free tables this party could be moved to, and other occupied tables it
    # could be merged with (4.1.1). Both only when this order is on a table,
    # still open, and hasn't taken any payment.
    free_tables = []
    mergeable_tables = []
    movable = (
        order.table_id
        and order.status not in (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED)
    )
    if movable:
        free_tables = db.execute(
            select(RestaurantTable).where(
                RestaurantTable.is_active.is_(True),
                RestaurantTable.status == TableStatus.FREE,
                RestaurantTable.id != order.table_id,
            ).order_by(RestaurantTable.number)
        ).scalars().all()
        # This order (the merge destination) may be partially paid; only the
        # tables it could absorb must be unpaid.
        others = db.execute(
            select(Order).where(
                Order.status.in_(LIVE_STATES),
                Order.table_id.is_not(None),
                Order.table_id != order.table_id,
            )
            # _has_paid_items reads each candidate's items/allocations; batch them
            # so the merge list is a couple of queries, not one graph per table.
            .options(selectinload(Order.items).selectinload(OrderItem.allocations))
        ).scalars().all()
        mergeable_tables = sorted(
            (o.table for o in others if not _has_paid_items(o)),
            key=lambda t: t.number,
        )

    # Day menus (prix fixe) for this order's date — offered as one-tap combos
    # while the order is still open. Every menu that's on right now is shown (a
    # standing all-day menu and a concurrent happy hour together), each with its
    # choices grouped by course for the picker.
    day_menus = []
    dm_mods: dict[int, list] = {}
    hh_deals: dict[int, dict] = {}
    hh_ended: dict | None = None
    if _hh_open:
        for dm in daymenu.resolve_all_for(db, _now.date(), _now):
            by_slot: dict[int, list] = {}
            for c in dm.choices:
                if c.menu_item and c.menu_item.is_active and c.menu_item.available:
                    by_slot.setdefault(c.course, []).append(c)
            if by_slot:
                day_menus.append({
                    "menu": dm,
                    "courses": [
                        {"slot": s, "label": day_menu_course_label(s), "choices": by_slot[s]}
                        for s in sorted(by_slot)
                    ],
                })
                # Each choosable dish's FREE (zero-price) modifier groups, so a
                # combo dish can be customised without changing the fixed price.
                for slot_choices in by_slot.values():
                    for ch in slot_choices:
                        mi = ch.menu_item
                        if mi.id in dm_mods:
                            continue
                        groups = []
                        for g in mi.modifier_groups:
                            free = [{"id": o.id, "label": o.name}
                                    for o in g.options if o.price_delta_cents == 0]
                            if free:
                                groups.append({"name": g.name, "single": g.single, "options": free})
                        dm_mods[mi.id] = groups

        # Happy hour (services/happyhour, Model A). First put back any un-fired
        # discounted line whose grace has passed, and flag it so the screen tells
        # the waiter it ended. Then mark which visible items are on happy hour now
        # (a star) and the price they'd get — the discount auto-applies at Add.
        _reverted = happyhour.revert_expired(db, order, _now)
        if _reverted:
            db.commit()
            hh_ended = {"count": len(_reverted),
                        "names": ", ".join(sorted({r.menu_item.name for r in _reverted}))}
        if _ad.any():
            _covered = list(items) + ([configuring] if configuring is not None else [])
            for mi in _covered:
                if mi.id in hh_deals:
                    continue
                _deal = _ad.for_item(mi)
                if _deal:
                    _pct, _h = _deal
                    hh_deals[mi.id] = {
                        "pct": _pct,
                        "price": happyhour.deal_price_cents(mi.price_cents, _pct),
                        "name": _h.name,
                    }

    # Collapse each combo's component lines into a single display unit carrying
    # the menu name and its fixed total; ordinary lines pass through unchanged.
    ctotals = daymenu.combo_totals(order)
    for g in line_groups:
        units = []
        seen: dict[int, dict] = {}
        for i in g["lines"]:
            if i.combo_id is not None:
                u = seen.get(i.combo_id)
                if u is None:
                    u = {"kind": "combo", "combo_id": i.combo_id, "name": "",
                         "total": ctotals.get(i.combo_id, 0), "lines": []}
                    seen[i.combo_id] = u
                    units.append(u)
                if i.combo_name:
                    u["name"] = i.combo_name
                u["lines"].append(i)
            else:
                units.append({"kind": "item", "line": i})
        g["units"] = units

    return render(request, "order.html", {
        "db": db, "staff": staff, "order": order, "categories": categories,
        "active_cat": active_cat, "menu_items": items, "modifiers": modifiers,
        "hh_mode": hh_mode, "hh_cats": hh_cats,
        "panel": panel, "subtotal": sum(i.line_total_cents for i in order.items),
        "free_tables": free_tables, "mergeable_tables": mergeable_tables,
        "active_seat": active_seat,
        "seat_cards": seat_cards, "table_card": table_card,
        "line_groups": line_groups,
        "day_menus": day_menus, "dm_mods": dm_mods,
        "hh_deals": hh_deals, "hh_ended": hh_ended,
        "configuring": configuring,
        # Data-driven upsell: 1-2 add-ons learned from this venue's order history,
        # scoped to the category the waiter is browsing.
        "upsells": upsell.suggest_upsells(db, order, limit=2, category_id=active_cat),
        "active_cat_name": next((c.name for c in categories if c.id == active_cat), None),
        "title": f"Order {order.code}",
    })


@router.post("/orders/{order_id}/seats/add")
def add_seat(
    order_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """Add the next seat to a live dine-in order — a guest joined the table.

    Party size is fixed when the table is opened, but parties grow. This adds
    seat N+1 without a move/merge, capped at the table's capacity (raise it in
    Manage tables to seat more). guest_count moves with it so the header, the
    floor card's "N guests" and the covers in reports stay in step, and the new
    seat is selected so the next item added lands on it.
    """
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    if order.status in (OrderStatus.PAID, OrderStatus.CLOSED):
        raise HTTPException(400, "This order is closed.")
    if order.table is None:
        raise HTTPException(400, "Only dine-in table orders have seats.")

    next_n = max((s.seat_number for s in order.seats), default=0) + 1
    if next_n > order.table.capacity:
        raise HTTPException(
            400,
            f"Table {order.table.number} seats {order.table.capacity}. Raise its "
            "capacity in Manage tables before adding another seat.",
        )
    db.add(Seat(order_id=order.id, seat_number=next_n, label=f"Seat {next_n}"))
    order.guest_count = max(order.guest_count, next_n)
    db.commit()
    return RedirectResponse(f"/orders/{order_id}?seat={next_n}", status_code=303)


@router.post("/orders/{order_id}/items")
def add_item(
    order_id: int,
    menu_item_id: int = Form(...),
    seat_number: int = Form(0),
    quantity: int = Form(1),
    notes: str = Form(""),
    course: int = Form(0),
    category: int = Form(0),
    hh: int = Form(0),
    modifier_ids: list[int] = Form(default=[]),
    option_ids: list[int] = Form(default=[]),
    allergens: list[str] = Form(default=[]),
    allergen_other: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """4.1.2 — add items with modifiers, allergies, instructions and a course."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    if order.status in (OrderStatus.PAID, OrderStatus.CLOSED):
        raise HTTPException(400, "This order is closed.")

    mi = db.get(MenuItem, menu_item_id)
    if mi is None:
        raise HTTPException(404, "Menu item not found")
    if not mi.available or not mi.is_active:
        # A stale order screen could still POST an item 86'd moments ago.
        raise HTTPException(400, f"{mi.name} is not available right now.")

    # 4.1.2 — validate the item's modifier groups against what was picked, and
    # gather the chosen options (snapshotted onto the line below).
    chosen = set(option_ids)
    picked_options = []
    for g in mi.modifier_groups:
        picks = [o for o in g.options if o.id in chosen]
        need = g.min_select or (1 if g.required else 0)
        if len(picks) < need:
            raise HTTPException(400, f"Please choose {g.name} for {mi.name}.")
        if g.max_select and len(picks) > g.max_select:
            raise HTTPException(400, f"Choose at most {g.max_select} for {g.name}.")
        picked_options.extend((g, o) for o in picks)

    seat = None
    if seat_number:
        seat = db.execute(
            select(Seat).where(Seat.order_id == order.id, Seat.seat_number == seat_number)
        ).scalar_one_or_none()

    resolved_course = course if course in COURSE_LABELS else mi.default_course
    resolved_allergens = build_allergens(allergens, allergen_other)
    new_mods = {m.id for m in (db.get(Modifier, i) for i in modifier_ids) if m}
    new_opts = {o.id for _, o in picked_options}

    # Happy hour (Model A): the discount is decided now, at Add, and snapshotted
    # onto the line — the percent, the full base it came off, and the hold
    # deadline (window end + grace). It never changes once the line is fired.
    hh_ld = happyhour.price_for(db, mi, datetime.now())
    hh_unit = hh_ld.unit_price_cents if hh_ld else mi.price_cents

    # Merge into an identical, still-pending line on the same seat rather than
    # stacking duplicate rows — pressing + repeatedly just bumps the quantity.
    # Shared (table) items are left alone: their per-seat shares make merging
    # ambiguous. A fired or paid line is never touched.
    if seat_number:
        for ex in order.items:
            if (ex.menu_item_id == mi.id
                    and ex.seat_id == (seat.id if seat else None)
                    and ex.kitchen_status == KitchenStatus.PENDING
                    and not ex.allocations and not ex.is_shared
                    and ex.notes == notes.strip()
                    and ex.allergens == resolved_allergens
                    and ex.course == resolved_course
                    and {m.modifier_id for m in ex.modifiers} == new_mods
                    and {o.option_id for o in ex.options} == new_opts
                    and ex.unit_price_cents == hh_unit
                    and ex.hh_id == (hh_ld.hh_id if hh_ld else None)):
                ex.quantity = min(99, ex.quantity + max(1, quantity))
                db.commit()
                dest = f"/orders/{order_id}?seat={seat_number}"
                if category:
                    dest += f"&category={category}"
                if hh:
                    dest += "&hh=1"
                return RedirectResponse(dest, status_code=303)

    item = OrderItem(
        order_id=order.id,
        menu_item_id=mi.id,
        seat_id=seat.id if seat else None,
        quantity=max(1, quantity),
        unit_price_cents=hh_unit,
        notes=notes.strip(),
        allergens=resolved_allergens,
        # 0 = "auto" from the menu section; an explicit choice overrides it.
        course=resolved_course,
        kitchen_status=KitchenStatus.PENDING,
    )
    if hh_ld:
        item.hh_id = hh_ld.hh_id
        item.hh_percent = hh_ld.percent
        item.hh_full_cents = hh_ld.full_cents
        item.hh_hold_until = hh_ld.hold_until
    db.add(item)
    db.flush()

    for mod_id in modifier_ids:
        mod = db.get(Modifier, mod_id)
        if mod:
            item.modifiers.append(
                OrderItemModifier(modifier_id=mod.id, price_delta_cents=mod.price_delta_cents)
            )
    # Snapshot the chosen modifier-group options (name + price) onto the line.
    for g, o in picked_options:
        item.options.append(OrderItemOption(
            option_id=o.id, group_name=g.name, label=o.name,
            price_delta_cents=o.price_delta_cents,
        ))

    # 4.2.4 — an item added to the table (seat 0) is a shared item: split it
    # evenly across every seat right away, so "Table" means shared, not just
    # unassigned. Waiters can still re-share or reassign it at payment.
    if seat_number == 0 and order.seats:
        db.flush()
        set_shared_item_shares(db, item, [s.id for s in order.seats])

    db.commit()

    # Stay on the same seat (and menu category, or the happy-hour filter) after
    # adding — the waiter picks the seat manually and it holds until they change it.
    dest = f"/orders/{order_id}?seat={seat_number}"
    if category:
        dest += f"&category={category}"
    if hh:
        dest += "&hh=1"
    return RedirectResponse(dest, status_code=303)


@router.post("/orders/{order_id}/day-menu")
def add_day_menu_combo(
    order_id: int,
    seat_number: int = Form(0),
    day_menu_id: int = Form(0),
    item_ids: list[int] = Form(default=[]),
    customizations: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """4.1 — add the day menu (prix fixe) as one fixed-price combo, one item per
    course. The components fire to the kitchen; the bill shows a single price.
    `customizations` is a JSON list of per-dish allergies/notes/free options."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    if order.status in (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED):
        raise HTTPException(400, "This order is closed.")
    _now = datetime.now()
    # Several menus can be on at once (a standing menu and a happy hour), so the
    # form names which one — but only honour it if it's genuinely orderable now.
    available = daymenu.resolve_all_for(db, _now.date(), _now)
    dm = next((m for m in available if m.id == day_menu_id), None) if day_menu_id else None
    if dm is None:
        dm = daymenu.resolve_for(db, _now.date(), _now)
    if dm is None:
        raise HTTPException(400, "No day menu is on right now.")
    seat = None
    if seat_number:
        seat = db.execute(
            select(Seat).where(Seat.order_id == order.id, Seat.seat_number == seat_number)
        ).scalar_one_or_none()
    # Per-dish customisation, keyed by menu item id (a stale/garbled blob is
    # simply ignored — the combo still adds).
    custom: dict[int, dict] = {}
    if customizations.strip():
        try:
            for row in json.loads(customizations):
                if isinstance(row, dict) and "item_id" in row:
                    custom[int(row["item_id"])] = row
        except (ValueError, TypeError):
            custom = {}
    try:
        daymenu.add_combo_to_order(db, order, dm, item_ids, seat, custom=custom)
    except daymenu.DayMenuError as e:
        raise HTTPException(400, str(e))
    db.commit()
    dest = f"/orders/{order_id}?seat={seat_number}" if seat_number else f"/orders/{order_id}"
    return RedirectResponse(dest, status_code=303)


@router.post("/orders/{order_id}/combo/{combo_id}/remove")
def remove_combo(
    order_id: int,
    combo_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """Remove a whole day-menu combo (all its component lines) at once, unless
    part of it has already been paid."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    lines = [i for i in order.items if i.combo_id == combo_id]
    if any(i.allocations for i in lines):
        raise HTTPException(400, "Part of this combo has already been paid for.")
    for i in lines:
        db.delete(i)
    db.commit()
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/orders/{order_id}/items/{item_id}/edit")
def edit_item(
    order_id: int,
    item_id: int,
    quantity: int = Form(...),
    notes: str = Form(""),
    course: int = Form(0),
    modifier_ids: list[int] = Form(default=[]),
    allergens: list[str] = Form(default=[]),
    allergen_other: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """4.1.2 — fix a line already on the order: quantity, note, modifiers, course.

    Blocked once any of the line has been paid (it has allocations that price
    it at the old value). Editing before payment saves a delete-and-re-add.
    """
    item = db.get(OrderItem, item_id)
    if item is None or item.order_id != order_id:
        raise HTTPException(404, "Item not found")
    if item.order.status in (OrderStatus.PAID, OrderStatus.CLOSED):
        raise HTTPException(400, "This order is closed.")
    if item.allocations:
        raise HTTPException(400, "This item has already been paid for.")
    if not 1 <= quantity <= 20:
        raise HTTPException(400, "Quantity must be between 1 and 20.")

    item.quantity = quantity
    item.notes = notes.strip()
    item.allergens = build_allergens(allergens, allergen_other)
    if course in COURSE_LABELS:
        item.course = course
    # Replace the modifier set; each captures the delta at edit time, matching
    # how add_item snapshots it.
    item.modifiers.clear()
    db.flush()
    for mod_id in modifier_ids:
        mod = db.get(Modifier, mod_id)
        if mod:
            item.modifiers.append(
                OrderItemModifier(modifier_id=mod.id, price_delta_cents=mod.price_delta_cents)
            )
    seat = item.seat.seat_number if item.seat else 0
    db.commit()
    return RedirectResponse(f"/orders/{order_id}?seat={seat}", status_code=303)


@router.post("/orders/{order_id}/items/{item_id}/remove")
def remove_item(
    order_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    item = db.get(OrderItem, item_id)
    if item is None or item.order_id != order_id:
        raise HTTPException(404, "Item not found")
    if item.allocations:
        raise HTTPException(400, "This item has already been paid for.")
    seat = item.seat.seat_number if item.seat else 0
    db.delete(item)
    db.commit()
    return RedirectResponse(f"/orders/{order_id}?seat={seat}", status_code=303)


# --------------------------------------------------------------------------
# 4.1.2  Menu availability — "86" an item mid-service
# --------------------------------------------------------------------------

@router.get("/availability")
def availability(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("menu.availability")),
):
    """A fast on/off board for the whole menu — front-of-house and kitchen use it
    to 86 an item the moment it sells out, and put it back when it's on again."""
    categories = db.execute(
        select(MenuCategory).order_by(MenuCategory.sort_order, MenuCategory.name)
    ).scalars().all()
    items = db.execute(
        select(MenuItem).where(MenuItem.is_active.is_(True)).order_by(MenuItem.name)
    ).scalars().all()
    by_cat: dict[int, list] = {c.id: [] for c in categories}
    for i in items:
        by_cat.setdefault(i.category_id, []).append(i)
    return render(request, "availability.html", {
        "db": db, "staff": staff, "categories": categories, "by_cat": by_cat,
        "eighty_sixed": sum(1 for i in items if not i.available),
        "title": "Menu availability",
    })


@router.post("/menu/{item_id}/availability")
def toggle_availability(
    item_id: int,
    available: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("menu.availability")),
):
    item = db.get(MenuItem, item_id)
    if item is None:
        raise HTTPException(404, "Menu item not found")
    item.available = bool(available)
    db.commit()
    return RedirectResponse("/availability", status_code=303)


@router.post("/orders/{order_id}/cancel")
def cancel_order(
    order_id: int,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("discount.approve")),
):
    """Cancel an order (manager action). Blocked while any payment stands.

    A paid order must have each payment voided first, so the money reversal is
    explicit rather than swept up in one click. On cancel the table is freed.
    """
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    if order.status in (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED):
        raise HTTPException(400, f"Order {order.code} is already closed.")
    live = [p for p in order.payments if not p.voided]
    if live:
        raise HTTPException(
            400,
            f"Order {order.code} has {len(live)} payment(s) on it. "
            "Void them before cancelling the order.",
        )

    order.status = OrderStatus.CANCELLED
    order.closed_at = datetime.now()
    if order.table:
        order.table.status = TableStatus.FREE
        order.table.current_waiter_id = None
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/orders/{order_id}/ready-to-pay")
def mark_ready_to_pay(
    order_id: int,
    ready: int = Form(1),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """4.1.1 — the waiter flags a table Ready to pay, or clears it to Occupied.

    Only meaningful once the order is Served: the guest asks for the bill after
    they've been fed. It only moves the floor-plan status, never the order's own
    kitchen/payment state.
    """
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    if order.table is None:
        raise HTTPException(400, "This is not a table order.")
    if order.status in (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED):
        raise HTTPException(400, f"Order {order.code} is already closed.")
    if ready and order.status not in (OrderStatus.SERVED, OrderStatus.PARTIALLY_PAID):
        raise HTTPException(400, "Mark the order Served before flagging Ready to pay.")

    order.table.status = (
        TableStatus.READY_TO_PAY if ready else TableStatus.OCCUPIED
    )
    db.commit()
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/orders/{order_id}/served")
def mark_served(
    order_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """Mark an order served — the waiter has delivered it to the table. Required
    before payment (flow: Preparing -> Ready to serve -> Served -> Ready to pay
    -> Paid). Normally done once the food is up, but allowed from any live
    pre-payment state so a drinks-only or quick order can be served too."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    servable = (
        OrderStatus.OPEN,
        OrderStatus.PREPARING,
        OrderStatus.READY,
        OrderStatus.PARTIALLY_PAID,
    )
    if order.status not in servable:
        raise HTTPException(
            400,
            f"Order {order.code} can't be marked served "
            f"(it's {order_status_label(order.status)}).",
        )
    # Deliver whatever's up (Ready) — those lines leave the kitchen line so a
    # later re-fire shows only the new items, not what was already served.
    for item in order.items:
        if item.kitchen_status == KitchenStatus.READY:
            item.kitchen_status = KitchenStatus.SERVED
    if order.status == OrderStatus.PARTIALLY_PAID:
        # A seat already settled — keep the payment state and just re-derive the
        # kitchen side (marks it off the board once nothing's left cooking).
        _recompute_kitchen(order, datetime.now())
    elif all(i.kitchen_status == KitchenStatus.SERVED for i in order.items):
        # Everything delivered — the order is served and off the board.
        order.status = OrderStatus.SERVED
        order.kitchen_status = KitchenStatus.SERVED
    else:
        # Still food cooking or held (pending/preparing) — the order is NOT fully
        # served. Re-derive so it stays Preparing and on the kitchen board (and
        # can't be paid) until the rest is up and served too.
        _recompute_kitchen(order, datetime.now())
    db.commit()
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


def _recompute_kitchen(order: Order, now: datetime) -> None:
    """Re-derive the order's kitchen/table state from its item statuses (4.1.3).

    With coursing, items advance course by course. The order is Ready only when
    every item is Ready; while any is preparing it's Preparing; before anything
    fires it's Pending. Payment states (partially_paid/paid) are never
    overwritten. A held (still-pending) course keeps the order from going Ready,
    which is the point — the table isn't done until dessert lands.
    """
    items = order.items
    if not items:
        return
    # Served items are delivered — they no longer drive the kitchen state. The
    # order's stage is derived from what's still on the line.
    active = [i for i in items if i.kitchen_status != KitchenStatus.SERVED]
    if not active:
        order.kitchen_status = KitchenStatus.SERVED   # nothing left to cook/plate
        return
    statuses = {i.kitchen_status for i in active}

    if statuses == {KitchenStatus.READY}:
        order.kitchen_status = KitchenStatus.READY
        order.ready_at = order.ready_at or now
        # Food up = "ready to serve", not "ready to pay". The table stays
        # occupied until the waiter serves it and the guest asks for the bill;
        # SERVED (already past READY) is left untouched.
        if order.status in (OrderStatus.OPEN, OrderStatus.PREPARING, OrderStatus.READY):
            order.status = OrderStatus.READY
        if order.delivery:
            order.delivery.status = DeliveryStatus.READY
    elif KitchenStatus.PREPARING in statuses or KitchenStatus.READY in statuses:
        order.kitchen_status = KitchenStatus.PREPARING
        # READY (a line was un-marked) and SERVED (a new item fired onto a served
        # order) both fall back to Preparing so the kitchen sees the work and the
        # order must be (re-)served before payment.
        if order.status in (OrderStatus.OPEN, OrderStatus.PREPARING,
                            OrderStatus.READY, OrderStatus.SERVED):
            order.status = OrderStatus.PREPARING
            # New food means the table isn't ready to pay any more — clear the
            # stale flag (the waiter re-flags it after re-serving). This is the
            # only place the system touches ready-to-pay, and only to clear it.
            if order.table and order.table.status == TableStatus.READY_TO_PAY:
                order.table.status = TableStatus.OCCUPIED
    else:
        order.kitchen_status = KitchenStatus.PENDING


def _create_tasks_for_item(db: Session, item: OrderItem, now: datetime) -> None:
    """Kitchen Stations B1 — create the PreparationTask set for a line being
    fired, snapshotting routing at this instant.

    One task per distinct target station: the item's own station (the base task,
    always present, may be NULL = Unassigned) plus any station its modifiers /
    options explicitly route to. Modifiers with no station (or the item's station)
    fold into the base task. The item is NOT duplicated — the sale line stays one
    `OrderItem`.

    Idempotent: if the line already has tasks it returns immediately, so a
    repeated/retried fire never double-creates. (No UNIQUE(order_item, station)
    constraint — a future re-fire/remake may legitimately add same-station tasks;
    idempotency here is the pending→preparing transition + this guard, serialized
    by the order row lock the fire routes take.)
    """
    if item.tasks:
        return
    base_station = item.menu_item.station_id
    label = item.menu_item.name
    groups: dict[int | None, list[tuple[str, int, str]]] = {}
    groups.setdefault(base_station, []).append(("base", item.id, label))
    for m in item.modifiers:
        st = m.modifier.station_id if m.modifier else None
        target = st if (st is not None and st != base_station) else base_station
        groups.setdefault(target, []).append(
            ("modifier", m.id, m.modifier.name if m.modifier else "")
        )
    for o in item.options:
        mo = db.get(ModifierOption, o.option_id) if o.option_id else None
        st = mo.station_id if mo else None
        target = st if (st is not None and st != base_station) else base_station
        groups.setdefault(target, []).append(("option", o.id, o.label))

    # The fire-batch number for this line. B1 fires a line exactly once, so this
    # is 1; a future re-fire/remake would compute the next batch (the DB unique
    # index on (order_item_id, fire_seq, COALESCE(station_id,-1)) allows a later
    # batch to reuse a station, and rejects a duplicate WITHIN this batch).
    fire_seq = 1 + max((t.fire_seq for t in item.tasks), default=0)
    for station_id, entries in groups.items():
        task = PreparationTask(
            order_item_id=item.id, order_id=item.order_id, station_id=station_id,
            course=item.course, kitchen_status=PreparationTaskStatus.PREPARING,
            quantity=item.quantity, item_label=label,
            is_base=(station_id == base_station), fire_seq=fire_seq, fired_at=now,
        )
        for kind, sid, lbl in entries:
            if kind == "base":
                continue                    # the item itself is already item_label
            task.details.append(PreparationTaskModifier(
                source_kind=kind, source_id=sid, label=lbl, station_id=station_id,
            ))
        db.add(task)
        item.tasks.append(task)


def rollup_item_kitchen_status(item: OrderItem) -> str:
    """Derive OrderItem.kitchen_status from its PreparationTasks (Stage B1).

    Rule: ready only when every non-served task is ready; otherwise preparing.
    NEVER sets served — served is owned by the existing serving flow. A line with
    no tasks (legacy / not fired) is left untouched, so old behavior is preserved.
    In B1 this drives only the fire→preparing transition; B2 makes it the live
    authority once the KDS marks individual tasks.
    """
    tasks = item.tasks
    if not tasks or item.kitchen_status == KitchenStatus.SERVED:
        return item.kitchen_status
    active = [t for t in tasks if t.kitchen_status != PreparationTaskStatus.SERVED]
    if active and all(t.kitchen_status == PreparationTaskStatus.READY for t in active):
        item.kitchen_status = KitchenStatus.READY
    elif active:
        item.kitchen_status = KitchenStatus.PREPARING
    return item.kitchen_status


def _fire_conflict_is_idempotent(db: Session, item_ids: list[int]) -> bool:
    """Decide whether an IntegrityError raised while committing a fire is the
    EXPECTED concurrent-duplicate case (safe to treat as an idempotent no-op) or
    an unrelated failure (must be surfaced). Call AFTER rollback.

    It is only idempotent if the database now shows the state a competing fire
    would have produced: every line we tried to fire is no longer PENDING AND
    already has ≥1 PreparationTask. Any other situation (a line still pending, a
    line with no tasks, missing rows) means the conflict was NOT the expected
    duplicate fire, so the caller must re-raise."""
    if not item_ids:
        return False
    rows = db.execute(
        select(OrderItem).where(OrderItem.id.in_(item_ids))
    ).scalars().all()
    if len(rows) != len(set(item_ids)):
        return False
    for it in rows:
        if it.kitchen_status == KitchenStatus.PENDING:
            return False
        has_tasks = db.execute(
            select(func.count()).select_from(PreparationTask)
            .where(PreparationTask.order_item_id == it.id)
        ).scalar_one()
        if not has_tasks:
            return False
    return True


def fired_items_without_tasks(db: Session) -> int:
    """B2 readiness invariant (Kitchen Stations): how many fired OrderItems
    (preparing/ready) still have NO PreparationTask. The KDS must NOT be switched
    to task-based reads (B2) unless this is 0 for the population expected to have
    tasks — otherwise task-based reads would silently hide that kitchen work.
    Informational in B1; the B2 slice will gate on it."""
    return db.execute(
        select(func.count()).select_from(OrderItem).where(
            OrderItem.kitchen_status.in_((KitchenStatus.PREPARING, KitchenStatus.READY)),
            ~OrderItem.tasks.any(),
        )
    ).scalar_one()


# --------------------------------------------------------------------------
# Kitchen Stations B2.1 — task-based KDS reads (DORMANT until activated)
# --------------------------------------------------------------------------

# PreparationTask statuses that put station work on the board (mirrors the
# OrderItem KITCHEN_STATES). SERVED tasks are off the line.
TASK_ACTIVE = (PreparationTaskStatus.PREPARING, PreparationTaskStatus.READY)

# Code-level activation guard. Task-based KDS is only safe once the B2.2 slice
# (per-task READY + the legacy-write compatibility bridge + rollup) exists — the
# hard gate that made B2.1 non-activatable. B2.2 IS that slice, so it is now True:
# the per-task READY route and the legacy-write bridge below give task mode a
# correct write path. Activation still ALSO requires the manual `kitchen_b2_active`
# setting AND the readiness invariant (fired_items_without_tasks == 0); this guard
# is the code-level precondition, not the whole gate.
B2_2_ACTIVE = True


def task_kds_active(db: Session) -> bool:
    """B2.1 activation gate (design v3 §2) — DORMANT, and NOT activatable in B2.1.

    Returns True (task-based KDS reads) ONLY when ALL hold:
      * `B2_2_ACTIVE` — the code-level B2.2 guard is on (False throughout B2.1, so
        this alone keeps the board on Stage A regardless of any setting);
      * the manual `kitchen_b2_active` setting is truthy (missing / false /
        non-boolean → False; B2.1 ships no UI path to set it); AND
      * every fired item already has PreparationTasks
        (`fired_items_without_tasks(db) == 0`).
    Any of these False → Stage A (never a hybrid). During B2.1 the first condition
    is always False, so manually flipping the setting cannot activate task mode."""
    if not B2_2_ACTIVE:
        return False
    if not settings_svc.flag(db, "kitchen_b2_active"):
        return False
    return fired_items_without_tasks(db) == 0


def _task_in_station(task, station_filter) -> bool:
    """The single, unambiguous station rule (design v3 §3.1): None = every station;
    "unassigned" = NULL station ONLY; an int = that station ONLY. An Unassigned
    task never matches a selected station, and a selected station never shows
    Unassigned."""
    if station_filter is None:
        return True
    if station_filter == "unassigned":
        return task.station_id is None
    return task.station_id == station_filter


def _task_ticket_courses(order: Order, station_filter=None) -> list[dict]:
    """An order's items grouped by course for the TASK-mode KDS (design v3 §3.7).

    Each sale OrderItem renders once; its active PreparationTasks matching the
    station filter become station sub-rows (attached as `item.display_tasks`).
    The course/line status shown is the rolled-up `OrderItem.kitchen_status`
    (decision #3), not a per-task status. Only fired items (those with active
    tasks) appear; held courses have no tasks. Read-only — no mutation here."""
    first_fire = order.sent_to_kitchen_at
    groups: dict[int, list] = {}
    for i in order.items:
        tks = [t for t in i.tasks
               if t.kitchen_status in TASK_ACTIVE and _task_in_station(t, station_filter)]
        if not tks:
            continue
        i.is_new = bool(first_fire and i.created_at and i.created_at > first_fire)
        i.display_tasks = sorted(
            tks, key=lambda t: (t.station.display_order if t.station else 999, t.id)
        )
        groups.setdefault(i.course, []).append(i)
    out = []
    for course in sorted(groups):
        items = groups[course]
        statuses = {it.kitchen_status for it in items}
        status = KitchenStatus.READY if statuses == {KitchenStatus.READY} else KitchenStatus.PREPARING
        out.append({
            "course": course, "label": course_label(course),
            "lines": items, "status": status, "multi": len(groups) > 1,
        })
    return out


@router.post("/orders/{order_id}/send")
def send_to_kitchen(
    order_id: int,
    course: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """4.1.2 / 4.1.3 — fire food to the kitchen.

    course=0 fires every held item; a specific course fires only that stage, so
    a waiter sends the starters now and holds the mains until the table is ready.
    """
    # Serialize concurrent fires of the same order (Postgres row lock; no-op on
    # SQLite, which already serializes writers) so two in-flight fires can't both
    # create the same line's tasks. The idempotency guard is the pending→preparing
    # transition: the loser re-reads no pending items and creates nothing.
    #
    # Lock on the scalar id, never on the entity: selecting Order pulls its
    # eager-loaded relationships into LEFT OUTER JOINs, and Postgres refuses
    # FOR UPDATE on the nullable side of an outer join. Same shape as task_ready.
    if db.execute(
        select(Order.id).where(Order.id == order_id).with_for_update()
    ).first() is None:
        raise HTTPException(404, "Order not found")
    # Nothing read before the lock may inform a decision — the competing fire we
    # just waited on may have moved these very lines. Discard any pre-lock view
    # and reselect the order and its items fresh, then derive `pending` from that.
    order = db.execute(
        select(Order).where(Order.id == order_id)
        .execution_options(populate_existing=True)
    ).scalars().first()
    if order is None:
        raise HTTPException(404, "Order not found")
    db.refresh(order, ["items"])
    pending = [i for i in order.items if i.kitchen_status == KitchenStatus.PENDING]
    if course:
        pending = [i for i in pending if i.course == course]
    if not pending:
        raise HTTPException(400, "Nothing new to fire to the kitchen.")
    pending_ids = [i.id for i in pending]

    now = datetime.now()
    # Lock happy-hour pricing at the fire: anything whose grace has passed un-fired
    # goes back to full price before it's committed to the kitchen.
    happyhour.revert_expired(db, order, now)
    for item in pending:
        # Snapshot the routing at fire time (Kitchen Stations): the KDS/Expo read
        # this, not the live MenuItem, so re-routing later never moves work that's
        # already on the line. NULL menu_item.station_id → Unassigned bucket.
        item.station_id = item.menu_item.station_id
        # Stage B1: create the preparation tasks (base + modifier stations) and
        # derive the line status from them (all tasks preparing → preparing).
        _create_tasks_for_item(db, item, now)
        rollup_item_kitchen_status(item)
    order.sent_to_kitchen_at = order.sent_to_kitchen_at or now
    _recompute_kitchen(order, now)
    try:
        db.commit()
    except IntegrityError:
        # Only an EXPECTED concurrent-duplicate fire is treated as idempotent —
        # verified against the DB state, not the error text. An unrelated
        # integrity failure is re-raised, not hidden as a successful fire.
        db.rollback()
        if not _fire_conflict_is_idempotent(db, pending_ids):
            raise
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.post("/orders/{order_id}/items/{item_id}/fire")
def fire_item(
    order_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """4.1.3 — fire a single line, for finer control than firing a whole course.

    The exception to coursing: push one dish to the kitchen early (or re-pace it)
    without sending its course-mates. It's just the send path scoped to one item,
    so the order/table state is re-derived the same way afterwards.
    """
    # Lock the parent order first, on the scalar id (an entity select would drag
    # the eager relationships into LEFT OUTER JOINs, which Postgres refuses to
    # lock). Every check below then runs on post-lock state: a competing fire may
    # have taken this line while we were waiting, so existence, ownership and the
    # PENDING precondition are all revalidated after the lock, never before it.
    if db.execute(
        select(Order.id).where(Order.id == order_id).with_for_update()
    ).first() is None:
        raise HTTPException(404, "Order not found")
    order = db.execute(
        select(Order).where(Order.id == order_id)
        .execution_options(populate_existing=True)
    ).scalars().first()
    if order is None:
        raise HTTPException(404, "Order not found")
    item = db.execute(
        select(OrderItem).where(OrderItem.id == item_id)
        .execution_options(populate_existing=True)
    ).scalars().first()
    if item is None or item.order_id != order.id:
        raise HTTPException(404, "Item not on this order")
    if item.kitchen_status != KitchenStatus.PENDING:
        raise HTTPException(400, "That item has already been fired.")

    now = datetime.now()
    happyhour.revert_expired(db, order, now)   # lock at full price if grace passed
    seat = item.seat.seat_number if item.seat else 0
    item.station_id = item.menu_item.station_id     # snapshot routing at fire time
    _create_tasks_for_item(db, item, now)           # Stage B1 tasks
    rollup_item_kitchen_status(item)                # tasks preparing → line preparing
    order.sent_to_kitchen_at = order.sent_to_kitchen_at or now
    _recompute_kitchen(order, now)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Idempotent only if a competing fire actually produced the expected
        # state for this line; otherwise surface the (unrelated) error.
        if not _fire_conflict_is_idempotent(db, [item_id]):
            raise
        return RedirectResponse(f"/orders/{order_id}?seat={seat}", status_code=303)
    # Stay on the seat you were working — don't collapse it.
    return RedirectResponse(f"/orders/{order_id}?seat={seat}", status_code=303)


# --------------------------------------------------------------------------
# 4.1.3  Kitchen display
# --------------------------------------------------------------------------

# Cap how many tickets the board renders (oldest-fired first). Keeps the page
# responsive under an unusually large open-order set; the rest are counted.
KITCHEN_LIMIT = 80


@router.get("/kitchen")
def kitchen_display(
    request: Request,
    view: str = "all",
    kstatus: str = "all",
    station: str = "all",
    mine: int | None = None,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("kitchen.view")),
):
    """Real-time feed with colour-coded urgency by elapsed time.

    Only orders that have actually been fired to the kitchen appear here — an
    order with nothing sent (kitchen_status PENDING, e.g. a waiter still building
    it) would render as an empty card, so it's left off the board entirely.
    """
    KITCHEN_STATES = (KitchenStatus.PREPARING, KitchenStatus.READY)
    if kstatus != "all" and kstatus not in KITCHEN_STATES:
        kstatus = "all"

    # Station filter (Kitchen Stations, Stage A): "all" = the full board;
    # "unassigned" = fired lines with no routing; else a station id. Reads the
    # snapshot on the order line, never the live MenuItem.
    if station == "unassigned":
        station_filter: str | int | None = "unassigned"
    elif station not in ("all", ""):
        try:
            station_filter = int(station)
        except ValueError:
            station_filter, station = None, "all"
    else:
        station_filter = None

    # "My tables" defaults on for anyone actually serving tables; unset in the URL
    # (first visit) means "decide by coverage", an explicit 0/1 (the toggle) wins.
    mine = (1 if _covers_tables(db, staff) else 0) if mine is None else (1 if mine else 0)

    # Kitchen Stations B2.1 — task-based reads are DORMANT and NOT activatable until
    # B2.2. The code-level B2_2_ACTIVE guard gates everything: while it is False
    # (all of B2.1), task mode can never switch on even if `kitchen_b2_active` is set
    # manually, and the blocked notice does not apply (there is no activation to
    # block). Single authority: TASK mode ONLY when B2.2 is present AND the flag is
    # on AND every fired item has tasks; otherwise the COMPLETE Stage A board.
    b2_flag = B2_2_ACTIVE and settings_svc.flag(db, "kitchen_b2_active")
    b2_missing = fired_items_without_tasks(db) if b2_flag else 0
    task_mode = b2_flag and b2_missing == 0
    b2_blocked = b2_flag and b2_missing > 0

    # Self-heal: an order left SERVED while it still has food on the line
    # (preparing/ready items) is inconsistent — re-derive so its cooking items
    # show on the board instead of vanishing.
    stale = db.execute(
        select(Order).where(
            Order.status == OrderStatus.SERVED,
            Order.id.in_(
                select(OrderItem.order_id).where(
                    OrderItem.kitchen_status.in_(KITCHEN_STATES)
                )
            ),
        )
    ).scalars().all()
    if stale:
        for o in stale:
            _recompute_kitchen(o, datetime.now())
        db.commit()

    _on_board = [
        Order.kitchen_status.in_(KITCHEN_STATES),
        # Served orders have been delivered — off the kitchen's plate.
        Order.status.not_in(
            (OrderStatus.SERVED, OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED)
        ),
    ]

    def _view_mine(stmt):
        """Apply the channel view + 'my tables' in SQL, so they narrow the set
        BEFORE the LIMIT — a valid order must never be pushed past the cap and
        silently vanish (e.g. an Unassigned order at position 81)."""
        if view in ("dine_in", "delivery"):
            stmt = stmt.join(Channel, Channel.id == Order.channel_id).where(
                Channel.channel_type == "delivery" if view == "delivery"
                else Channel.channel_type != "delivery"
            )
        if mine:
            stmt = stmt.where(Order.waiter_id == staff.id)
        return stmt

    # Status tallies over the board (view/mine applied, but NOT the status or
    # station filter) so each status button shows what it would reveal.
    status_counts = {KitchenStatus.PREPARING: 0, KitchenStatus.READY: 0}
    for st, n in db.execute(
        _view_mine(select(Order.kitchen_status, func.count()).where(*_on_board))
        .group_by(Order.kitchen_status)
    ):
        status_counts[st] = n

    # Fired lines per station (+ explicit "unassigned"), over the same board —
    # the station tab badges. Aggregated in SQL, not by scanning a capped page.
    station_line_counts: dict = {}
    if task_mode:
        # Badges count active PreparationTasks (station-work units) over the SAME
        # order universe/view/mine AND the same kstatus filter as the board
        # (design v3 §3.3), so a tab's count matches exactly what selecting it
        # would reveal. Only the station filter is replaced by GROUP BY station.
        cq = _view_mine(
            select(PreparationTask.station_id, func.count())
            .select_from(PreparationTask).join(Order, Order.id == PreparationTask.order_id)
            .where(*_on_board, PreparationTask.kitchen_status.in_(TASK_ACTIVE))
        )
        if kstatus != "all":
            cq = cq.where(Order.kitchen_status == kstatus)
        for sid, n in db.execute(cq.group_by(PreparationTask.station_id)):
            station_line_counts[sid if sid is not None else "unassigned"] = n
    else:
        for sid, n in db.execute(
            _view_mine(
                select(OrderItem.station_id, func.count())
                .select_from(OrderItem).join(Order, Order.id == OrderItem.order_id)
                .where(*_on_board, OrderItem.kitchen_status.in_(KITCHEN_STATES))
            ).group_by(OrderItem.station_id)
        ):
            station_line_counts[sid if sid is not None else "unassigned"] = n

    # The rendered set: view/mine + the status and station filters, ALL in SQL,
    # oldest-fired first, then capped. board_total counts the SAME filtered set
    # (no LIMIT) so "showing N of M" is honest.
    tq = _view_mine(select(Order).where(*_on_board))
    if kstatus != "all":
        tq = tq.where(Order.kitchen_status == kstatus)
    if station_filter is not None:
        if task_mode:
            # Task-mode membership from PreparationTask.station_id ONLY (the fire
            # snapshot), single-rule (design v3 §3.1): selected station = that id;
            # Unassigned = NULL; the two are mutually exclusive.
            conds = [
                PreparationTask.order_id == Order.id,
                PreparationTask.kitchen_status.in_(TASK_ACTIVE),
                PreparationTask.station_id.is_(None) if station_filter == "unassigned"
                else PreparationTask.station_id == station_filter,
            ]
            tq = tq.where(select(PreparationTask.id).where(*conds).exists())
        else:
            sub = select(OrderItem.id).where(
                OrderItem.order_id == Order.id,
                OrderItem.kitchen_status.in_(KITCHEN_STATES),
                OrderItem.station_id.is_(None) if station_filter == "unassigned"
                else OrderItem.station_id == station_filter,
            )
            tq = tq.where(sub.exists())
    board_total = db.execute(
        select(func.count()).select_from(tq.subquery())
    ).scalar_one()
    # Task mode also eager-loads each item's tasks + their station (station
    # sub-rows) so rendering stays one SELECT per level, no N+1.
    load_opts = [selectinload(Order.items).selectinload(OrderItem.station)]
    if task_mode:
        load_opts.append(
            selectinload(Order.items).selectinload(OrderItem.tasks).selectinload(PreparationTask.station)
        )
    orders = db.execute(
        tq.options(*load_opts)
        .order_by(Order.sent_to_kitchen_at).limit(KITCHEN_LIMIT)
    ).scalars().all()

    now = datetime.now()
    tickets = []
    for o in orders:
        is_delivery = o.channel.channel_type == "delivery"
        # Items grouped by course, scoped to the chosen station. Task mode groups
        # by PreparationTask (one sale line, task station sub-rows); Stage A keeps
        # the line-snapshot grouping.
        courses = (_task_ticket_courses(o, station_filter) if task_mode
                   else _ticket_courses(o, station_filter))
        if station_filter is not None and not courses:
            continue
        sent_at = o.sent_to_kitchen_at or o.opened_at
        elapsed = int((now - sent_at).total_seconds() // 60)
        # Colour-coded urgency (4.1.3).
        urgency = "ok" if elapsed < 10 else ("warn" if elapsed < 20 else "late")
        # total_items is SALE-item quantity (never a station-work/task count). In
        # task mode and any station-scoped view it sums the shown OrderItems' qty.
        if task_mode or station_filter is not None:
            total_items = sum(i.quantity for c in courses for i in c["lines"])
        else:
            total_items = sum(
                i.quantity for i in o.items if i.kitchen_status != KitchenStatus.SERVED
            )
        tickets.append({
            "order": o,
            "elapsed": elapsed,
            # Clock time the ticket reached the kitchen — the receipt's field 3.
            "sent_at": sent_at,
            "urgency": urgency,
            "is_delivery": is_delivery,
            # Field 2: who fired the order. Delivery tickets have no waiter.
            "server": o.waiter.name if o.waiter else None,
            # Field 5: the line count the expo checks the plated tray against —
            # only what's still on the line (served items have left the kitchen).
            "total_items": total_items,
            "courses": courses,
            "where": (
                f"Table {o.table.number}" if o.table
                else f"{o.channel.name}"
                + (f" · {o.delivery.platform_ref}" if o.delivery and o.delivery.platform_ref else "")
            ),
        })
    tickets.sort(key=lambda t: (-t["elapsed"],))

    # Station tabs: active stations PLUS any inactive station that still has
    # fired lines on the board — so a just-deactivated station's live work stays
    # reachable (its lines keep their snapshot; they don't move to Unassigned).
    tab_ids = {sid for sid in station_line_counts if isinstance(sid, int)}
    stations = db.execute(
        select(Station).where(or_(Station.is_active.is_(True), Station.id.in_(tab_ids)))
        .order_by(Station.display_order, Station.name)
    ).scalars().all()

    # Station tab counts: total on the board (All) plus per station / unassigned.
    station_tab_counts = {
        "all": sum(station_line_counts.values()),
        "unassigned": station_line_counts.get("unassigned", 0),
    }
    for s in stations:
        station_tab_counts[s.id] = station_line_counts.get(s.id, 0)

    return render(request, "kitchen.html", {
        "db": db, "staff": staff, "tickets": tickets, "view": view,
        "kstatus": kstatus, "mine": 1 if mine else 0, "status_counts": status_counts,
        "stations": stations, "station": station,
        "station_tab_counts": station_tab_counts,
        # Only offer the Unassigned tab when something un-routed is actually on
        # the board — but never hide those lines: the All board still shows them.
        "has_unassigned": station_line_counts.get("unassigned", 0) > 0,
        "board_total": board_total, "limit": KITCHEN_LIMIT,
        # Kitchen Stations B2.1: task_mode drives task station sub-rows + read-only
        # rendering; b2_blocked shows the "activation blocked" operator notice while
        # the board falls back to complete Stage A.
        "task_mode": task_mode, "b2_blocked": b2_blocked, "b2_missing": b2_missing,
        "title": "Kitchen display",
    })


def _line_in_station(item: OrderItem, station_filter) -> bool:
    """Whether a line belongs to the selected station filter (Kitchen Stations).

    None = every station (the full expo board); "unassigned" = un-routed lines;
    an int = that station id. Matches the snapshot on the line, not live routing.
    """
    if station_filter is None:
        return True
    if station_filter == "unassigned":
        return item.station_id is None
    return item.station_id == station_filter


def _ticket_courses(order: Order, station_filter=None) -> list[dict]:
    """An order's items grouped by course for the kitchen display, in meal order.

    Only fired items (preparing/ready) reach the kitchen, so a held course
    simply doesn't appear until the waiter fires it. Each group's status is the
    least-advanced item in it — a course is 'ready' only when all its items are.
    A station_filter scopes the lines to one station (or the Unassigned bucket).
    """
    # A line added after the order was first fired is a later addition — flag it
    # so the line can tell a new item apart from what it already made/plated
    # (transient attribute, display-only).
    first_fire = order.sent_to_kitchen_at
    groups: dict[int, list] = {}
    for i in order.items:
        if i.kitchen_status in (KitchenStatus.PREPARING, KitchenStatus.READY):
            if not _line_in_station(i, station_filter):
                continue
            i.is_new = bool(first_fire and i.created_at and i.created_at > first_fire)
            groups.setdefault(i.course, []).append(i)
    out = []
    for course in sorted(groups):
        items = groups[course]
        statuses = {i.kitchen_status for i in items}
        status = KitchenStatus.READY if statuses == {KitchenStatus.READY} else KitchenStatus.PREPARING
        out.append({
            # 'lines' not 'items': in Jinja, `course.items` would resolve to the
            # dict's built-in .items() method, not this list.
            "course": course, "label": course_label(course),
            "lines": items, "status": status,
            "multi": len(groups) > 1,
        })
    return out


def _expo_courses(order: Order) -> list[dict]:
    """An order's fired lines grouped by course, then by station — the Expo's
    coordination view (Kitchen Stations, Stage A).

    Reads the station snapshot on each line. Per station: ready only when all of
    its lines are ready. Per course: ready only when every station is — else it
    is "waiting on" the stations that aren't. Coursing is preserved, so a held
    later course never blocks an earlier one from reading ready.
    """
    by_course: dict[int, dict] = {}
    for i in order.items:
        if i.kitchen_status in (KitchenStatus.PREPARING, KitchenStatus.READY):
            by_course.setdefault(i.course, {}).setdefault(i.station_id, []).append(i)
    out = []
    for course in sorted(by_course):
        st_groups, waiting = [], []
        all_ready = True
        for _sid, items in by_course[course].items():
            ready = all(it.kitchen_status == KitchenStatus.READY for it in items)
            st = items[0].station                      # snapshot relationship
            name = st.name if st else "Unassigned"
            if not ready:
                all_ready = False
                waiting.append(name)
            st_groups.append({"station": st, "name": name, "ready": ready, "lines": items})
        st_groups.sort(key=lambda g: (g["station"].display_order if g["station"] else 999, g["name"]))
        waiting.sort()
        out.append({
            "course": course, "label": course_label(course),
            "stations": st_groups, "ready": all_ready, "waiting_on": waiting,
            "multi": len(by_course) > 1,
        })
    return out


# The Expo board caps how many orders it renders — with a large open-order set a
# full render would be unusable. Oldest-fired first (most urgent), rest counted.
EXPO_LIMIT = 60


@router.get("/expo")
def expo_display(
    request: Request,
    view: str = "all",
    mine: int | None = None,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("kitchen.view")),
):
    """Coordination board: per table/course, which stations are ready and the
    overall 'waiting on X' → 'ready to serve'. Reuses the per-item kitchen status
    and the station snapshot; no new state (Kitchen Stations, Stage A)."""
    mine = (1 if _covers_tables(db, staff) else 0) if mine is None else (1 if mine else 0)

    # Channel view + "my tables" applied in SQL, before the LIMIT, so the cap
    # never drops a valid order off the end.
    board = select(Order).where(
        Order.kitchen_status.in_((KitchenStatus.PREPARING, KitchenStatus.READY)),
        Order.status.not_in(
            (OrderStatus.SERVED, OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED)
        ),
    )
    if view in ("dine_in", "delivery"):
        board = board.join(Channel, Channel.id == Order.channel_id).where(
            Channel.channel_type == "delivery" if view == "delivery"
            else Channel.channel_type != "delivery"
        )
    if mine:
        board = board.where(Order.waiter_id == staff.id)

    total = db.execute(select(func.count()).select_from(board.subquery())).scalar_one()
    orders = db.execute(
        board.options(selectinload(Order.items).selectinload(OrderItem.station))
        .order_by(Order.sent_to_kitchen_at).limit(EXPO_LIMIT)
    ).scalars().all()

    now = datetime.now()
    cards = []
    for o in orders:
        is_delivery = o.channel.channel_type == "delivery"
        courses = _expo_courses(o)
        if not courses:
            continue
        sent_at = o.sent_to_kitchen_at or o.opened_at
        elapsed = int((now - sent_at).total_seconds() // 60)
        urgency = "ok" if elapsed < 10 else ("warn" if elapsed < 20 else "late")
        cards.append({
            "order": o, "courses": courses, "elapsed": elapsed, "urgency": urgency,
            "is_delivery": is_delivery,
            "server": o.waiter.name if o.waiter else None,
            "all_ready": o.kitchen_status == KitchenStatus.READY,
            "where": (
                f"Table {o.table.number}" if o.table
                else f"{o.channel.name}"
                + (f" · {o.delivery.platform_ref}" if o.delivery and o.delivery.platform_ref else "")
            ),
        })
    cards.sort(key=lambda t: (-t["elapsed"],))

    return render(request, "expo.html", {
        "db": db, "staff": staff, "cards": cards, "view": view,
        "mine": 1 if mine else 0, "shown": total, "limit": EXPO_LIMIT,
        "title": "Expo",
    })


def _safe_next_board(target: str) -> str:
    """Allowlist the post-action redirect. The only legitimate destinations in
    Stage A are the kitchen and expo boards, so the PATH must be exactly
    "/kitchen" or "/expo" (the query string may ride along). Everything else —
    an external URL, "//host", a backslash, or a look-alike like "/expo-x" —
    falls back to "/kitchen". Validated with urlsplit, not startswith."""
    if not target or "\\" in target:
        return "/kitchen"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or parts.path not in ("/kitchen", "/expo"):
        return "/kitchen"
    return urlunsplit(("", "", parts.path, parts.query, ""))


@router.post("/kitchen/{order_id}/status")
def kitchen_status(
    order_id: int,
    status: str = Form(...),
    course: int = Form(0),
    item_id: int = Form(0),
    next: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("kitchen.update")),
):
    """Advance items to Pending/Preparing/Ready (4.1.3).

    item_id marks a single line; else course marks one stage; else (course=0)
    the whole order. For task-backed items (Kitchen Stations B2.2) the request is
    translated through their PreparationTasks so the tasks and the OrderItem rollup
    can never contradict — the item's kitchen_status is never written directly.
    Two-phase under the parent Order lock: every target is VALIDATED before any
    mutation, so an unsupported request (PENDING on a task-backed item) rejects the
    whole request with no partial update. Legacy items with no tasks keep the
    Stage A direct write.
    """
    if status not in (KitchenStatus.PENDING, KitchenStatus.PREPARING, KitchenStatus.READY):
        raise HTTPException(400, "Invalid kitchen status.")
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    # Serialize against concurrent task READYs / kitchen writes on this order, then
    # work only from post-lock refreshed state (never pre-lock objects).
    db.execute(select(Order.id).where(Order.id == order_id).with_for_update()).first()
    db.refresh(order, ["items"])

    if item_id:
        targets = [i for i in order.items if i.id == item_id]
    elif course:
        targets = [i for i in order.items if i.course == course]
    else:
        targets = list(order.items)
    for i in targets:
        db.refresh(i, ["tasks"])            # fresh sibling task state per target

    now = datetime.now()
    # PHASE 1 — validate EVERY task-backed target against the requested transition
    # BEFORE any mutation, so an invalid target rejects the whole request with no
    # partial commit (design v3 §5.1).
    for item in targets:
        if item.kitchen_status == KitchenStatus.SERVED:
            continue                        # delivered; a course-level action skips it
        if not item.tasks:
            continue                        # legacy no-task item — Stage A direct write
        if status == KitchenStatus.PENDING:
            # Un-firing tasked work needs the deferred re-fire model.
            db.rollback()
            raise HTTPException(409, "A fired kitchen item cannot be set back to pending.")
        # READY / PREPARING act on the item's ACTIVE (non-served) tasks, which must
        # EACH be in a state the per-task READY route supports (PREPARING or READY).
        # No active task (e.g. all SERVED), or any non-served task in an unsupported
        # state (e.g. an unexpected PENDING), means there is no valid transition —
        # reject the whole request rather than silently ignoring it in Phase 2.
        non_served = [t for t in item.tasks if t.kitchen_status != PreparationTaskStatus.SERVED]
        if not non_served or any(t.kitchen_status not in TASK_ACTIVE for t in non_served):
            db.rollback()
            raise HTTPException(409, "This kitchen item has no valid task transition.")

    # PHASE 2 — apply. Task-backed items route through their tasks + rollup; legacy
    # (no-task) items keep the Stage A direct write.
    for item in targets:
        if item.kitchen_status == KitchenStatus.SERVED:
            continue
        if item.tasks:
            active = [t for t in item.tasks if t.kitchen_status != PreparationTaskStatus.SERVED]
            if status == KitchenStatus.READY:
                for t in active:
                    if t.kitchen_status == PreparationTaskStatus.PREPARING:
                        t.kitchen_status = PreparationTaskStatus.READY
                        t.ready_at = t.ready_at or now      # preserve an existing ready_at
                rollup_item_kitchen_status(item)
            elif status == KitchenStatus.PREPARING:
                # Coarse "un-ready this item" (design v3 §5.3) — DESTRUCTIVE: clears
                # READY at EVERY station of the item.
                for t in active:
                    t.kitchen_status = PreparationTaskStatus.PREPARING
                    t.ready_at = None
                rollup_item_kitchen_status(item)
        else:
            item.kitchen_status = status

    if status == KitchenStatus.PREPARING:
        order.sent_to_kitchen_at = order.sent_to_kitchen_at or now
    _recompute_kitchen(order, now)
    db.commit()
    # Return to the board the action came from — allowlisted to /kitchen or /expo.
    return RedirectResponse(_safe_next_board(next), status_code=303)


@router.post("/kitchen/tasks/{task_id}/ready")
def task_ready(
    task_id: int,
    next: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("kitchen.update")),
):
    """Kitchen Stations B2.2 — mark one PreparationTask READY, then roll its
    OrderItem up.

    PreparationTask is the prep-state authority for task-backed items;
    `OrderItem.kitchen_status` is a rollup result (never written directly here) and
    becomes READY only when every non-served task is READY. Tasks never own SERVED,
    so this never serves the item and the payment gate is unchanged.

    Concurrency: the parent Order is locked FOR UPDATE, then the task and its sibling
    tasks are RE-SELECTED from committed state — the transition and rollup use only
    that refreshed state, never the pre-lock object. `PREPARING → READY` sets
    `ready_at`; `READY → READY` is an idempotent no-op that preserves the original
    `ready_at`; a SERVED (or otherwise unexpected) task is rejected.
    """
    task = db.get(PreparationTask, task_id)
    if task is None:
        raise HTTPException(404, "Preparation task not found")
    order_id = task.order_id
    # Lock the parent order, then discard the pre-lock view and reselect fresh.
    if db.execute(select(Order.id).where(Order.id == order_id).with_for_update()).first() is None:
        raise HTTPException(404, "Order not found")
    task = db.execute(
        select(PreparationTask).where(PreparationTask.id == task_id)
        .execution_options(populate_existing=True)
    ).scalars().first()
    if task is None:
        raise HTTPException(409, "Preparation task no longer exists.")
    item = task.order_item
    db.refresh(item, ["tasks"])             # fresh sibling set for the rollup

    now = datetime.now()
    if task.kitchen_status == PreparationTaskStatus.READY:
        pass                                # idempotent — keep the original ready_at
    elif task.kitchen_status == PreparationTaskStatus.PREPARING:
        task.kitchen_status = PreparationTaskStatus.READY
        task.ready_at = task.ready_at or now
    else:                                   # SERVED or anything unexpected
        raise HTTPException(409, "This task cannot be marked ready from its current state.")

    rollup_item_kitchen_status(item)        # item READY only when ALL non-served tasks READY
    _recompute_kitchen(item.order, now)
    db.commit()
    return RedirectResponse(_safe_next_board(next), status_code=303)


# --------------------------------------------------------------------------
# 4.1.4  Delivery
# --------------------------------------------------------------------------

@router.get("/delivery")
def delivery_queue(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("delivery.view")),
):
    rows = db.execute(
        select(DeliveryOrder).join(Order).where(
            Order.status.not_in((OrderStatus.CANCELLED,))
        ).order_by(DeliveryOrder.id.desc()).limit(60)
    ).scalars().all()

    active = [d for d in rows if d.status != DeliveryStatus.DELIVERED]
    recent = [d for d in rows if d.status == DeliveryStatus.DELIVERED][:12]
    drivers = db.execute(
        select(Staff).where(
            Staff.role == Role.DELIVERY_COORDINATOR, Staff.is_active.is_(True)
        )
    ).scalars().all()

    return render(request, "delivery.html", {
        "db": db, "staff": staff, "active": active, "recent": recent,
        "drivers": drivers, "statuses": [
            DeliveryStatus.PENDING, DeliveryStatus.PREPARING, DeliveryStatus.READY,
            DeliveryStatus.ON_THE_WAY, DeliveryStatus.DELIVERED,
        ],
        "title": "Delivery",
    })


@router.post("/delivery/{delivery_id}/update")
def delivery_update(
    delivery_id: int,
    status: str = Form(None),
    driver_id: int = Form(None),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("delivery.update")),
):
    """Track Pending -> Preparing -> Ready -> On the way -> Delivered (4.1.4)."""
    d = db.get(DeliveryOrder, delivery_id)
    if d is None:
        raise HTTPException(404, "Delivery order not found")

    if driver_id:
        d.driver_id = driver_id
        d.assigned_at = datetime.now()
    if status:
        valid = (
            DeliveryStatus.PENDING, DeliveryStatus.PREPARING, DeliveryStatus.READY,
            DeliveryStatus.ON_THE_WAY, DeliveryStatus.DELIVERED,
        )
        if status not in valid:
            raise HTTPException(400, "Invalid delivery status.")
        d.status = status
        if status == DeliveryStatus.DELIVERED:
            d.delivered_at = datetime.now()
    db.commit()
    return RedirectResponse("/delivery", status_code=303)
