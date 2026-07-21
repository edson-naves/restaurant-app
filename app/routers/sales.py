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
    Channel,
    DeliveryOrder,
    DeliveryStatus,
    KitchenStatus,
    MenuCategory,
    MenuItem,
    Modifier,
    Order,
    OrderItem,
    OrderItemModifier,
    OrderStatus,
    RestaurantTable,
    Role,
    Seat,
    Staff,
    TableStatus,
)
from app.services.payments import balance_panel, ensure_seats

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
    db.commit()
    return RedirectResponse(f"/orders/{order.id}", status_code=303)


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
# 4.1.2  Order creation
# --------------------------------------------------------------------------

@router.get("/orders/{order_id}")
def order_screen(
    order_id: int,
    request: Request,
    category: int | None = None,
    db: Session = Depends(get_db),
    staff: Staff = Depends(current_staff),
):
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")

    categories = db.execute(
        select(MenuCategory).order_by(MenuCategory.sort_order)
    ).scalars().all()
    active_cat = category or (categories[0].id if categories else None)
    items = db.execute(
        select(MenuItem).where(
            MenuItem.category_id == active_cat, MenuItem.is_active.is_(True)
        ).order_by(MenuItem.name)
    ).scalars().all()

    modifiers = db.execute(select(Modifier).order_by(Modifier.name)).scalars().all()
    panel = balance_panel(db, order)

    return render(request, "order.html", {
        "db": db, "staff": staff, "order": order, "categories": categories,
        "active_cat": active_cat, "menu_items": items, "modifiers": modifiers,
        "panel": panel, "subtotal": sum(i.line_total_cents for i in order.items),
        "title": f"Order {order.code}",
    })


@router.post("/orders/{order_id}/items")
def add_item(
    order_id: int,
    menu_item_id: int = Form(...),
    seat_number: int = Form(0),
    quantity: int = Form(1),
    notes: str = Form(""),
    modifier_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """4.1.2 — add items with modifiers and special instructions."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    if order.status in (OrderStatus.PAID, OrderStatus.CLOSED):
        raise HTTPException(400, "This order is closed.")

    mi = db.get(MenuItem, menu_item_id)
    if mi is None:
        raise HTTPException(404, "Menu item not found")

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


@router.post("/orders/{order_id}/send")
def send_to_kitchen(
    order_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("orders.manage")),
):
    """4.1.2 / workflow 6.1 step 4 — one tap, kitchen display updates."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    pending = [i for i in order.items if i.kitchen_status == KitchenStatus.PENDING]
    if not pending:
        raise HTTPException(400, "Nothing new to send to the kitchen.")

    now = datetime.now()
    for item in pending:
        item.kitchen_status = KitchenStatus.PREPARING
    order.kitchen_status = KitchenStatus.PREPARING
    order.status = OrderStatus.PREPARING
    order.sent_to_kitchen_at = order.sent_to_kitchen_at or now
    db.commit()
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


# --------------------------------------------------------------------------
# 4.1.3  Kitchen display
# --------------------------------------------------------------------------

@router.get("/kitchen")
def kitchen_display(
    request: Request,
    view: str = "all",
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("kitchen.view")),
):
    """Real-time feed with colour-coded urgency by elapsed time."""
    q = select(Order).where(
        Order.kitchen_status.in_((KitchenStatus.PENDING, KitchenStatus.PREPARING, KitchenStatus.READY)),
        Order.status.not_in((OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED)),
    )
    orders = db.execute(q).scalars().all()

    now = datetime.now()
    tickets = []
    for o in orders:
        is_delivery = o.channel.channel_type == "delivery"
        if view == "dine_in" and is_delivery:
            continue
        if view == "delivery" and not is_delivery:
            continue
        elapsed = int((now - (o.sent_to_kitchen_at or o.opened_at)).total_seconds() // 60)
        # Colour-coded urgency (4.1.3).
        urgency = "ok" if elapsed < 10 else ("warn" if elapsed < 20 else "late")
        tickets.append({
            "order": o,
            "elapsed": elapsed,
            "urgency": urgency,
            "is_delivery": is_delivery,
            "where": (
                f"Table {o.table.number}" if o.table
                else f"{o.channel.name}"
                + (f" · {o.delivery.platform_ref}" if o.delivery and o.delivery.platform_ref else "")
            ),
        })
    tickets.sort(key=lambda t: (-t["elapsed"],))

    return render(request, "kitchen.html", {
        "db": db, "staff": staff, "tickets": tickets, "view": view,
        "title": "Kitchen display",
    })


@router.post("/kitchen/{order_id}/status")
def kitchen_status(
    order_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("kitchen.update")),
):
    """Pending -> Preparing -> Ready (4.1.3); Ready flips the table to Ready to Pay."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    if status not in (KitchenStatus.PENDING, KitchenStatus.PREPARING, KitchenStatus.READY):
        raise HTTPException(400, "Invalid kitchen status.")

    order.kitchen_status = status
    for item in order.items:
        item.kitchen_status = status

    if status == KitchenStatus.PREPARING:
        order.status = OrderStatus.PREPARING
        order.sent_to_kitchen_at = order.sent_to_kitchen_at or datetime.now()
    elif status == KitchenStatus.READY:
        # Workflow 6.1 step 5 — waiter notified, table becomes Ready to Pay.
        order.status = OrderStatus.READY
        order.ready_at = datetime.now()
        if order.table:
            order.table.status = TableStatus.READY_TO_PAY
        if order.delivery:
            order.delivery.status = DeliveryStatus.READY
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


@router.post("/staff/switch")
def switch_staff(staff_id: int = Form(...), next_url: str = Form("/")):
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie("staff_id", str(staff_id), max_age=60 * 60 * 12)
    return resp
