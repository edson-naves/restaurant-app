"""Day menus (prix fixe) at order time — section 4.1.

Resolving which fixed-price menu is on for a given day, and dropping a chosen
combo onto an order. The guest picks one item per course; the menu's single
fixed price is spread across those component lines (weighted by each dish's
à-la-carte price) using the same largest-remainder split the rest of the system
uses — so the lines sum to *exactly* the fixed price, never the sum of the
dishes, and payment, tax and reconciliation keep working per line.

The component lines are tied together by a shared combo_id (the header line also
carries combo_name), which the order screen, pay screen and receipt use to show
the combo as one entry at its fixed price.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.oltp import (
    DayMenu, KitchenStatus, MenuItem, Order, OrderItem, Seat,
)
from app.services.money import distribute


class DayMenuError(Exception):
    pass


def resolve_for(db: Session, d: date) -> DayMenu | None:
    """The active day menu for date `d`. A menu tied to this specific date wins
    over a recurring weekday menu; otherwise the weekday menu (if any) applies."""
    dated = db.execute(
        select(DayMenu).where(DayMenu.is_active.is_(True), DayMenu.menu_date == d)
    ).scalars().first()
    if dated is not None:
        return dated
    return db.execute(
        select(DayMenu).where(DayMenu.is_active.is_(True), DayMenu.weekday == d.weekday())
    ).scalars().first()


def add_combo_to_order(
    db: Session, order: Order, day_menu: DayMenu,
    chosen_item_ids: list[int], seat: Seat | None,
) -> list[OrderItem]:
    """Add the guest's chosen components as one fixed-price combo.

    `chosen_item_ids` is one MenuItem id per course the guest picked; only ids
    that are genuine choices on this menu are honoured. Returns the created
    lines. Raises DayMenuError if nothing valid was chosen.
    """
    valid = {c.menu_item_id for c in day_menu.choices}
    items: list[MenuItem] = []
    seen: set[int] = set()
    for iid in chosen_item_ids:
        if iid in valid and iid not in seen:
            mi = db.get(MenuItem, iid)
            if mi is not None and mi.is_active and mi.available:
                items.append(mi)
                seen.add(iid)
    if not items:
        raise DayMenuError("Pick at least one course for the day menu.")

    # Spread the fixed price across the components, weighted by à-la-carte price,
    # so a $30 steak carries more of it than a $6 soup and the parts total the
    # fixed price exactly.
    weights = [max(1, mi.price_cents) for mi in items]
    prices = distribute(day_menu.price_cents, weights)

    created: list[OrderItem] = []
    for mi, price in zip(items, prices):
        oi = OrderItem(
            order_id=order.id, menu_item_id=mi.id,
            seat_id=seat.id if seat else None, quantity=1,
            unit_price_cents=price, course=mi.default_course,
            kitchen_status=KitchenStatus.PENDING,
        )
        db.add(oi)
        created.append(oi)
    db.flush()

    # Group the lines: combo_id = the first line's id; only the header names it.
    combo_id = created[0].id
    for i, oi in enumerate(created):
        oi.combo_id = combo_id
        oi.combo_name = day_menu.name if i == 0 else ""
    db.flush()
    return created


def combo_totals(order: Order) -> dict[int, int]:
    """{combo_id: total_cents} for every combo on the order — the fixed price to
    show against the combo header line."""
    totals: dict[int, int] = {}
    for i in order.items:
        if i.combo_id is not None:
            totals[i.combo_id] = totals.get(i.combo_id, 0) + i.line_total_cents
    return totals
