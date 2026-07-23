"""Sales & Orders — section 4.1."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import can, current_staff, render, require
from app.models.oltp import (
    ALLERGEN_OPTIONS,
    COURSE_LABELS,
    AuditEvent,
    Channel,
    DeliveryOrder,
    DeliveryStatus,
    KitchenStatus,
    course_label,
    MenuCategory,
    MenuItem,
    Modifier,
    Order,
    OrderItem,
    OrderItemModifier,
    OrderItemOption,
    OrderStatus,
    build_allergens,
    RestaurantTable,
    Role,
    Seat,
    SeatStatus,
    SharedItemShare,
    Staff,
    TableStatus,
)
from app.services.payments import balance_panel, ensure_seats, set_shared_item_shares

router = APIRouter()


def _next_code(db: Session) -> str:
    n = db.execute(select(func.count()).select_from(Order)).scalar_one()
    return f"ORD-{datetime.now().strftime('%y%m%d')}-{n + 1:05d}"


# --------------------------------------------------------------------------
# 4.1.1  Floor plan
# --------------------------------------------------------------------------

@router.get("/")
def floor_plan(request: Request, db: Session = Depends(get_db), staff: Staff = Depends(current_staff)):
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
                 OrderStatus.PARTIALLY_PAID)
            ),
            Order.table_id.is_not(None),
        )
    ).scalars().all()
    by_table = {o.table_id: o for o in open_orders}

    cards = []
    for t in tables:
        order = by_table.get(t.id)
        total = sum(i.line_total_cents for i in order.items) if order else 0
        cards.append(
            {
                "table": t,
                "order": order,
                "total_cents": total,
                "guests": order.guest_count if order else 0,
                "waiter": t.current_waiter.name if t.current_waiter else None,
                "minutes": (
                    int((datetime.now() - order.opened_at).total_seconds() // 60)
                    if order else 0
                ),
            }
        )

    def tally(group):
        return {
            "free": sum(1 for c in group if c["table"].status == TableStatus.FREE),
            "occupied": sum(1 for c in group if c["table"].status == TableStatus.OCCUPIED),
            "ready": sum(1 for c in group if c["table"].status == TableStatus.READY_TO_PAY),
        }

    counts = tally(cards)

    # One section per floor rather than one grid of everything: a waiter works a
    # floor, and 50+ cards from three storeys interleaved cannot be scanned.
    # Sections, not tabs — nothing is hidden behind a click mid-service.
    sections = []
    for floor in sorted(
        {c["table"].floor for c in cards if c["table"].floor},
        key=lambda f: (f.sort_order, f.name),
    ):
        group = [c for c in cards if c["table"].floor_id == floor.id]
        sections.append({"floor": floor, "cards": group, "counts": tally(group)})

    # A table whose zone was removed would otherwise vanish from the floor.
    homeless = [c for c in cards if not c["table"].floor]
    if homeless:
        sections.append({"floor": None, "cards": homeless, "counts": tally(homeless)})

    delivery_pending = db.execute(
        select(func.count()).select_from(DeliveryOrder).where(
            DeliveryOrder.status.not_in((DeliveryStatus.DELIVERED,))
        )
    ).scalar_one()

    return render(request, "floor.html", {
        "db": db, "staff": staff, "cards": cards, "counts": counts,
        "sections": sections,
        "delivery_pending": delivery_pending, "title": "Floor plan",
    })


@router.post("/tables/{table_id}/open")
def open_table(
    table_id: int,
    guests: int = Form(2),
    waiter_id: int = Form(...),
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
    channel = db.execute(select(Channel).where(Channel.code == "dine_in")).scalar_one()
    order = Order(
        code=_next_code(db),
        table_id=table.id,
        channel_id=channel.id,
        waiter_id=waiter_id,
        status=OrderStatus.OPEN,
        guest_count=guests,
        opened_at=datetime.now(),
    )
    db.add(order)
    db.flush()

    ensure_seats(db, order, guests)          # 4.2.4 — a payer per seat
    table.status = TableStatus.OCCUPIED      # system response: Occupied
    table.current_waiter_id = waiter_id
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
    db.commit()
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------
# 4.1.1  Move a party to another table
# --------------------------------------------------------------------------

LIVE_STATES = (
    OrderStatus.OPEN, OrderStatus.PREPARING,
    OrderStatus.READY, OrderStatus.PARTIALLY_PAID,
)


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
    db: Session = Depends(get_db),
    staff: Staff = Depends(current_staff),
):
    order = db.get(Order, order_id)
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
    active_cat = category or (categories[0].id if categories else None)
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
    items = db.execute(
        select(MenuItem).where(
            MenuItem.category_id == active_cat,
            MenuItem.is_active.is_(True), MenuItem.available.is_(True),
        ).order_by(MenuItem.name)
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
        ).scalars().all()
        mergeable_tables = sorted(
            (o.table for o in others if not _has_paid_items(o)),
            key=lambda t: t.number,
        )

    return render(request, "order.html", {
        "db": db, "staff": staff, "order": order, "categories": categories,
        "active_cat": active_cat, "menu_items": items, "modifiers": modifiers,
        "panel": panel, "subtotal": sum(i.line_total_cents for i in order.items),
        "free_tables": free_tables, "mergeable_tables": mergeable_tables,
        "active_seat": active_seat,
        "seat_cards": seat_cards, "table_card": table_card,
        "configuring": configuring,
        "title": f"Order {order.code}",
    })


@router.post("/orders/{order_id}/items")
def add_item(
    order_id: int,
    menu_item_id: int = Form(...),
    seat_number: int = Form(0),
    quantity: int = Form(1),
    notes: str = Form(""),
    course: int = Form(0),
    category: int = Form(0),
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

    item = OrderItem(
        order_id=order.id,
        menu_item_id=mi.id,
        seat_id=seat.id if seat else None,
        quantity=max(1, quantity),
        unit_price_cents=mi.price_cents,
        notes=notes.strip(),
        allergens=build_allergens(allergens, allergen_other),
        # 0 = "auto" from the menu section; an explicit choice overrides it.
        course=course if course in COURSE_LABELS else mi.default_course,
        kitchen_status=KitchenStatus.PENDING,
    )
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

    # Stay on the same seat (and menu category) after adding — the waiter picks
    # the seat manually and it holds until they change it.
    dest = f"/orders/{order_id}?seat={seat_number}"
    if category:
        dest += f"&category={category}"
    return RedirectResponse(dest, status_code=303)


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
    db.commit()
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


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
    db.delete(item)
    db.commit()
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


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
    """4.1.1 — a waiter flags a table Ready to pay, or clears it back to Occupied.

    The kitchen flips the table automatically when the food is up, but a guest
    asks for the bill on their own schedule — often before the kitchen is done,
    sometimes after. This is the waiter's manual control over that state; it
    only moves the floor-plan status, never the order's own kitchen/payment
    state, so it can't interfere with sending food or taking payment.
    """
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    if order.table is None:
        raise HTTPException(400, "This is not a table order.")
    if order.status in (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED):
        raise HTTPException(400, f"Order {order.code} is already closed.")

    order.table.status = (
        TableStatus.READY_TO_PAY if ready else TableStatus.OCCUPIED
    )
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
    statuses = {i.kitchen_status for i in items}

    if statuses == {KitchenStatus.READY}:
        order.kitchen_status = KitchenStatus.READY
        order.ready_at = order.ready_at or now
        if order.status in (OrderStatus.OPEN, OrderStatus.PREPARING, OrderStatus.READY):
            order.status = OrderStatus.READY
            if order.table:
                order.table.status = TableStatus.READY_TO_PAY
        if order.delivery:
            order.delivery.status = DeliveryStatus.READY
    elif KitchenStatus.PREPARING in statuses or KitchenStatus.READY in statuses:
        order.kitchen_status = KitchenStatus.PREPARING
        if order.status in (OrderStatus.OPEN, OrderStatus.PREPARING):
            order.status = OrderStatus.PREPARING
    else:
        order.kitchen_status = KitchenStatus.PENDING


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
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    pending = [i for i in order.items if i.kitchen_status == KitchenStatus.PENDING]
    if course:
        pending = [i for i in pending if i.course == course]
    if not pending:
        raise HTTPException(400, "Nothing new to fire to the kitchen.")

    now = datetime.now()
    for item in pending:
        item.kitchen_status = KitchenStatus.PREPARING
    order.sent_to_kitchen_at = order.sent_to_kitchen_at or now
    _recompute_kitchen(order, now)
    db.commit()
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
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    item = db.get(OrderItem, item_id)
    if item is None or item.order_id != order.id:
        raise HTTPException(404, "Item not on this order")
    if item.kitchen_status != KitchenStatus.PENDING:
        raise HTTPException(400, "That item has already been fired.")

    now = datetime.now()
    item.kitchen_status = KitchenStatus.PREPARING
    order.sent_to_kitchen_at = order.sent_to_kitchen_at or now
    _recompute_kitchen(order, now)
    db.commit()
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


# --------------------------------------------------------------------------
# 4.1.3  Kitchen display
# --------------------------------------------------------------------------

@router.get("/kitchen")
def kitchen_display(
    request: Request,
    view: str = "all",
    kstatus: str = "all",
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("kitchen.view")),
):
    """Real-time feed with colour-coded urgency by elapsed time."""
    KITCHEN_STATES = (KitchenStatus.PENDING, KitchenStatus.PREPARING, KitchenStatus.READY)
    if kstatus != "all" and kstatus not in KITCHEN_STATES:
        kstatus = "all"

    q = select(Order).where(
        Order.kitchen_status.in_(KITCHEN_STATES),
        Order.status.not_in((OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED)),
    )
    orders = db.execute(q).scalars().all()

    # Counts reflect the channel view but not the status filter, so each status
    # button can show how many it would reveal — the number must not drop to
    # zero just because a different status is currently selected.
    now = datetime.now()
    tickets = []
    status_counts = {KitchenStatus.PENDING: 0, KitchenStatus.PREPARING: 0, KitchenStatus.READY: 0}
    for o in orders:
        is_delivery = o.channel.channel_type == "delivery"
        if view == "dine_in" and is_delivery:
            continue
        if view == "delivery" and not is_delivery:
            continue
        status_counts[o.kitchen_status] = status_counts.get(o.kitchen_status, 0) + 1
        if kstatus != "all" and o.kitchen_status != kstatus:
            continue
        sent_at = o.sent_to_kitchen_at or o.opened_at
        elapsed = int((now - sent_at).total_seconds() // 60)
        # Colour-coded urgency (4.1.3).
        urgency = "ok" if elapsed < 10 else ("warn" if elapsed < 20 else "late")
        tickets.append({
            "order": o,
            "elapsed": elapsed,
            # Clock time the ticket reached the kitchen — the receipt's field 3.
            "sent_at": sent_at,
            "urgency": urgency,
            "is_delivery": is_delivery,
            # Field 2: who fired the order. Delivery tickets have no waiter.
            "server": o.waiter.name if o.waiter else None,
            # Field 5: the line count the expo checks the plated tray against.
            "total_items": sum(i.quantity for i in o.items),
            # Items grouped by course, each with an aggregate status so the line
            # can mark a whole course up at once (4.1.3 coursing).
            "courses": _ticket_courses(o),
            "where": (
                f"Table {o.table.number}" if o.table
                else f"{o.channel.name}"
                + (f" · {o.delivery.platform_ref}" if o.delivery and o.delivery.platform_ref else "")
            ),
        })
    tickets.sort(key=lambda t: (-t["elapsed"],))

    return render(request, "kitchen.html", {
        "db": db, "staff": staff, "tickets": tickets, "view": view,
        "kstatus": kstatus, "status_counts": status_counts,
        "title": "Kitchen display",
    })


def _ticket_courses(order: Order) -> list[dict]:
    """An order's items grouped by course for the kitchen display, in meal order.

    Only fired items (preparing/ready) reach the kitchen, so a held course
    simply doesn't appear until the waiter fires it. Each group's status is the
    least-advanced item in it — a course is 'ready' only when all its items are.
    """
    groups: dict[int, list] = {}
    for i in order.items:
        if i.kitchen_status in (KitchenStatus.PREPARING, KitchenStatus.READY):
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


@router.post("/kitchen/{order_id}/status")
def kitchen_status(
    order_id: int,
    status: str = Form(...),
    course: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("kitchen.update")),
):
    """Advance items to Pending/Preparing/Ready (4.1.3).

    course=0 moves the whole order; a specific course moves only that stage, so
    the line can mark the starters up while the mains are still cooking. The
    order/table state is then re-derived — Ready to Pay only once everything is
    up (see _recompute_kitchen).
    """
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    if status not in (KitchenStatus.PENDING, KitchenStatus.PREPARING, KitchenStatus.READY):
        raise HTTPException(400, "Invalid kitchen status.")

    targets = order.items if not course else [i for i in order.items if i.course == course]
    for item in targets:
        item.kitchen_status = status

    now = datetime.now()
    if status == KitchenStatus.PREPARING:
        order.sent_to_kitchen_at = order.sent_to_kitchen_at or now
    _recompute_kitchen(order, now)
    db.commit()
    return RedirectResponse("/kitchen", status_code=303)


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
