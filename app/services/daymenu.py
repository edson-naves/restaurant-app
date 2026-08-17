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

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.oltp import (
    DayMenu, KitchenStatus, MenuItem, Order, OrderItem, OrderItemOption, Seat,
    build_allergens,
)
from app.services.money import distribute


class DayMenuError(Exception):
    pass


def resolve_for(db: Session, d: date, now: datetime | None = None) -> DayMenu | None:
    """The active day menu for date `d` at time `now` (default: current time).

    A menu tied to this specific date wins over a recurring weekday menu.
    Within each group only menus whose timeframe currently applies are eligible
    (menus with no window are always eligible), and a time-boxed menu — e.g. a
    happy hour — is preferred over an all-day one while its window is open."""
    now = now or datetime.now()
    t = now.time()

    def pick(menus: list[DayMenu]) -> DayMenu | None:
        avail = [m for m in menus if m.available_at(t)]
        if not avail:
            return None
        # A menu with an explicit window is the more specific offer right now.
        avail.sort(key=lambda m: 0 if m.start_time and m.end_time else 1)
        return avail[0]

    dated = db.execute(
        select(DayMenu).where(DayMenu.is_active.is_(True), DayMenu.menu_date == d)
    ).scalars().all()
    weekday = db.execute(
        select(DayMenu).where(DayMenu.is_active.is_(True), DayMenu.weekday == d.weekday())
    ).scalars().all()
    return pick(dated) or pick(weekday)


def resolve_all_for(db: Session, d: date, now: datetime | None = None) -> list[DayMenu]:
    """Every day menu orderable on date `d` at time `now` — so a regular all-day
    prix fixe and a concurrent time-boxed offer (e.g. a happy hour) both show,
    rather than the single most-specific one `resolve_for` returns.

    Both the dated and recurring-weekday menus that are available right now are
    included (deduped by id). Menus with an explicit window sort after all-day
    ones so the standing day menu leads and the happy hour follows."""
    now = now or datetime.now()
    t = now.time()
    dated = db.execute(
        select(DayMenu).where(DayMenu.is_active.is_(True), DayMenu.menu_date == d)
    ).scalars().all()
    weekday = db.execute(
        select(DayMenu).where(DayMenu.is_active.is_(True), DayMenu.weekday == d.weekday())
    ).scalars().all()
    out: list[DayMenu] = []
    seen: set[int] = set()
    for m in [*dated, *weekday]:
        if m.id not in seen and m.available_at(t):
            out.append(m)
            seen.add(m.id)
    out.sort(key=lambda m: (0 if not (m.start_time and m.end_time) else 1, m.id))
    return out


def active_percent_deals(
    db: Session, d: date, now: datetime | None = None
) -> dict[int, DayMenu]:
    """{menu_item_id: DayMenu} for items covered by a *percent* (happy-hour) day
    menu that is orderable right now — the signal the order screen uses to badge
    a menu row and to offer the deal price when that item is added. Fixed-price
    combos are excluded on purpose (they only make sense as a full combo, never
    per item). If several active deals cover one item, the bigger discount wins.
    """
    best: dict[int, DayMenu] = {}
    for m in resolve_all_for(db, d, now):
        if not m.is_percent:
            continue
        pct = m.discount_percent or 0
        for c in m.choices:
            cur = best.get(c.menu_item_id)
            if cur is None or (cur.discount_percent or 0) < pct:
                best[c.menu_item_id] = m
    return best


def effective_price_cents(day_menu: DayMenu, items: list[MenuItem]) -> int:
    """What the guest pays for this combo of chosen dishes: the menu's fixed
    price, or the discounted à-la-carte total for a percent (happy-hour) deal."""
    if day_menu.is_percent:
        gross = sum(max(0, mi.price_cents) for mi in items)
        pct = max(0, min(100, day_menu.discount_percent or 0))
        return round(gross * (100 - pct) / 100)
    return day_menu.price_cents


def _apply_custom(oi: OrderItem, mi: MenuItem, cz: dict) -> None:
    """Apply a guest's customisation to one combo dish: allergies, a note, and
    any chosen FREE (zero-price) modifier options. Priced add-ons are ignored on
    purpose so the combo stays at its fixed price."""
    oi.allergens = build_allergens(cz.get("allergens") or [], cz.get("allergen_other") or "")
    oi.notes = (cz.get("notes") or "").strip()
    chosen = {int(x) for x in (cz.get("option_ids") or []) if str(x).isdigit()}
    if not chosen:
        return
    for g in mi.modifier_groups:
        for o in g.options:
            if o.id in chosen and o.price_delta_cents == 0:
                oi.options.append(OrderItemOption(
                    option_id=o.id, group_name=g.name, label=o.name, price_delta_cents=0,
                ))


def add_combo_to_order(
    db: Session, order: Order, day_menu: DayMenu,
    chosen_item_ids: list[int], seat: Seat | None,
    custom: dict[int, dict] | None = None,
) -> list[OrderItem]:
    """Add the guest's chosen components as one fixed-price combo.

    `chosen_item_ids` is one MenuItem id per course the guest picked; only ids
    that are genuine choices on this menu are honoured. `custom` maps a chosen
    item id to its per-dish customisation (allergies, note, free options).
    Returns the created lines. Raises DayMenuError if nothing valid was chosen.
    """
    custom = custom or {}
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

    # Spread the combo's price (fixed, or the discounted à-la-carte total for a
    # happy-hour deal) across the components, weighted by à-la-carte price, so a
    # $30 steak carries more of it than a $6 soup and the parts total exactly.
    weights = [max(1, mi.price_cents) for mi in items]
    prices = distribute(effective_price_cents(day_menu, items), weights)

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

    # Group the lines (combo_id = the first line's id; only the header names it)
    # and apply each dish's customisation.
    combo_id = created[0].id
    for i, (oi, mi) in enumerate(zip(created, items)):
        oi.combo_id = combo_id
        oi.combo_name = day_menu.name if i == 0 else ""
        if mi.id in custom:
            _apply_custom(oi, mi, custom[mi.id])
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
