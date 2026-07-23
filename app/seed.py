"""Seed reference data and generate realistic trading history.

The history is shaped, not uniform: lunch and dinner peaks, weekend uplift,
channel mix across dine-in / own drivers / UberEats / DoorDash, and a
realistic spread of payment instruments. Reports built on flat random data
look plausible but tell you nothing, and cannot reveal a broken GROUP BY.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models.oltp import (
    Channel,
    DeliveryOrder,
    DeliveryStatus,
    Discount,
    KitchenStatus,
    MenuCategory,
    MenuItem,
    Modifier,
    ModifierGroup,
    ModifierOption,
    Order,
    OrderItem,
    OrderItemModifier,
    OrderStatus,
    Payment,
    PaymentAllocation,
    PaymentInstrument,
    Receipt,
    RestaurantTable,
    Role,
    Seat,
    SeatStatus,
    SharedItemShare,
    Staff,
    TableStatus,
)
from app.models.star import (
    DimChannel,
    DimDate,
    DimMenuItem,
    DimPaymentInstrument,
    DimStaff,
    DimTable,
    DimTime,
    EtlWatermark,
    FactOrderHeader,
    FactOrderItem,
    FactPayment,
)
from app.services.money import distribute, pct

rng = random.Random(20260716)

WEEKS_OF_HISTORY = 6
N_TABLES = 40  # section 2: 26-50 tables


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------

CHANNELS = [
    ("dine_in", "Dine-in", "dine_in", False),
    ("own_delivery", "Own driver", "delivery", False),
    ("ubereats", "UberEats", "delivery", True),
    ("doordash", "DoorDash", "delivery", True),
]

# Section 4.2.1
INSTRUMENTS = [
    ("visa", "Visa", "card", "Visa", False, False),
    ("mastercard", "Mastercard", "card", "Mastercard", False, False),
    ("amex", "American Express", "card", "Amex", False, False),
    ("cash", "Cash", "cash", None, False, False),
    ("contactless", "Contactless / Tap", "contactless", "Contactless", False, False),
    ("etransfer", "E-transfer / Interac", "etransfer", None, False, False),
    ("ubereats", "UberEats", "platform", None, True, True),
    ("doordash", "DoorDash", "platform", None, True, True),
]

STAFF = [
    ("Elena Rossi", Role.OWNER, "1000"),
    ("Marcus Chen", Role.MANAGER, "2000"),
    ("Sofia Martins", Role.WAITER, "3001"),
    ("Liam O'Brien", Role.WAITER, "3002"),
    ("Priya Sharma", Role.WAITER, "3003"),
    ("Diego Alvarez", Role.WAITER, "3004"),
    ("Emma Wilson", Role.WAITER, "3005"),
    ("Antoine Dubois", Role.KITCHEN, "4001"),
    ("Kenji Tanaka", Role.KITCHEN, "4002"),
    ("Fatima Haddad", Role.DELIVERY_COORDINATOR, "5001"),
    ("Carlos Mendes", Role.DELIVERY_COORDINATOR, "5002"),   # own driver
    ("Nina Petrova", Role.DELIVERY_COORDINATOR, "5003"),    # own driver
]

# (name, price_cents, shareable, popularity weight)
MENU = {
    "Starters": [
        ("Garlic Bread", 795, True, 9),
        ("Bruschetta", 950, True, 6),
        ("Crispy Calamari", 1450, True, 7),
        ("Soup of the Day", 850, False, 4),
        ("Caesar Salad", 1250, False, 6),
    ],
    "Mains": [
        ("Ribeye Steak", 3450, False, 8),
        ("Grilled Salmon", 2790, False, 7),
        ("Chicken Parmesan", 2350, False, 8),
        ("Margherita Pizza", 1850, True, 9),
        ("Mushroom Risotto", 2100, False, 5),
        ("Beef Burger", 1950, False, 10),
        ("Pad Thai", 1990, False, 5),
        ("Lamb Chops", 3200, False, 4),
    ],
    "Sides": [
        ("Truffle Fries", 850, True, 8),
        ("Mashed Potatoes", 650, False, 4),
        ("Seasonal Vegetables", 700, False, 3),
        ("Side Salad", 600, False, 3),
    ],
    "Desserts": [
        ("Tiramisu", 950, False, 6),
        ("New York Cheesecake", 900, False, 5),
        ("Chocolate Lava Cake", 1050, False, 6),
        ("Gelato (2 scoops)", 700, False, 4),
    ],
    "Drinks": [
        ("House Red (glass)", 1100, False, 8),
        ("House White (glass)", 1100, False, 7),
        ("Craft Beer", 900, False, 8),
        ("Soft Drink", 450, False, 9),
        ("Espresso", 400, False, 6),
        ("Sparkling Water", 500, False, 5),
    ],
}

MODIFIERS = [
    ("Extra cheese", 200),
    ("No onions", 0),
    ("Gluten-free base", 250),
    ("Well done", 0),
    ("Medium rare", 0),
    ("Extra sauce", 150),
    ("Side of aioli", 100),
    ("Spicy", 0),
]

# 4.1.2 — grouped, rule-based modifiers per item:
#   item -> [(group, required, min_select, max_select, [(option, price_cents)])]
MODIFIER_GROUPS = {
    "Lamb Chops": [
        ("Cooking level", True, 1, 1,
         [("Rare", 0), ("Medium Rare", 0), ("Medium", 0), ("Medium Well", 0), ("Well Done", 0)]),
        ("Sauce", False, 0, 1,
         [("None", 0), ("Peppercorn", 150), ("Béarnaise", 150), ("Mint jelly", 0)]),
        ("Add-ons", False, 0, 0,
         [("Extra chop", 600), ("Grilled mushrooms", 200), ("Truffle butter", 250)]),
    ],
    "Chicken Parmesan": [
        ("Pasta side", True, 1, 1,
         [("Spaghetti", 0), ("Penne", 0), ("Side salad instead", 0)]),
        ("Add-ons", False, 0, 0,
         [("Extra cheese", 200), ("Extra sauce", 100), ("Chilli flakes", 0)]),
    ],
}

SPECIAL_NOTES = [
    "", "", "", "", "",
    "Allergy: nuts", "No coriander please", "Birthday - candle on dessert",
    "Serve starters first", "Sauce on the side",
]

ZONES = ["Main", "Window", "Patio", "Bar"]

CUSTOMERS = [
    "J. Almeida", "R. Kaur", "T. Nguyen", "M. Silva", "A. Johnson", "K. Ivanov",
    "L. Dubois", "S. Okafor", "H. Yamada", "P. Novak", "C. Ferreira", "D. Murphy",
]

STREETS = [
    "142 King St W", "88 Queen St E", "23 Rue Sainte-Catherine", "1075 Bay St",
    "56 Dundas Ave", "310 Bloor St W", "77 Front St", "912 College St",
]


def reset_database() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def seed_reference(db: Session) -> dict:
    channels = {}
    for code, name, ctype, third in CHANNELS:
        c = Channel(code=code, name=name, channel_type=ctype, is_third_party=third)
        db.add(c)
        channels[code] = c

    instruments = {}
    for code, name, itype, brand, third, delivery_only in INSTRUMENTS:
        i = PaymentInstrument(
            code=code, name=name, instrument_type=itype, card_brand=brand,
            is_third_party=third, delivery_only=delivery_only,
        )
        db.add(i)
        instruments[code] = i

    staff = []
    for name, role, pin in STAFF:
        s = Staff(name=name, role=role, pin_code=pin, is_active=True)
        db.add(s)
        staff.append(s)

    tables = []
    for n in range(1, N_TABLES + 1):
        zone = ZONES[(n - 1) // 10] if (n - 1) // 10 < len(ZONES) else "Main"
        capacity = rng.choice([2, 2, 4, 4, 4, 6, 8])
        tables.append(
            RestaurantTable(
                number=n, zone=zone, capacity=capacity,
                status=TableStatus.FREE,
                pos_x=(n - 1) % 8, pos_y=(n - 1) // 8,
            )
        )
        db.add(tables[-1])

    items = []
    for sort_order, (cat_name, entries) in enumerate(MENU.items()):
        cat = MenuCategory(name=cat_name, sort_order=sort_order)
        db.add(cat)
        db.flush()
        for item_name, price, shareable, weight in entries:
            mi = MenuItem(
                category_id=cat.id, name=item_name, description="",
                price_cents=price, is_shareable=shareable, is_active=True,
            )
            db.add(mi)
            db.flush()
            items.append((mi, weight))

    modifiers = []
    for name, delta in MODIFIERS:
        m = Modifier(name=name, price_delta_cents=delta)
        db.add(m)
        modifiers.append(m)

    # 4.1.2 — grouped, rule-based modifiers on a couple of demo items. Kept off
    # the items the e2e suite orders, which add them without options.
    by_name = {mi.name: mi for mi, _ in items}
    for item_name, groups in MODIFIER_GROUPS.items():
        mi = by_name.get(item_name)
        if mi is None:
            continue
        for gi, (gname, req, mn, mx, opts) in enumerate(groups):
            g = ModifierGroup(menu_item_id=mi.id, name=gname, required=req,
                              min_select=mn, max_select=mx, sort_order=gi)
            db.add(g)
            db.flush()
            for oi, (oname, price) in enumerate(opts):
                db.add(ModifierOption(group_id=g.id, name=oname,
                                      price_delta_cents=price, sort_order=oi))

    db.flush()
    return {
        "channels": channels,
        "instruments": instruments,
        "staff": staff,
        "tables": tables,
        "items": items,
        "modifiers": modifiers,
    }


# --------------------------------------------------------------------------
# History generation
# --------------------------------------------------------------------------

def _weighted_items(items, k):
    pool = [mi for mi, w in items]
    weights = [w for _, w in items]
    return rng.choices(pool, weights=weights, k=k)


def _order_time(day: date) -> datetime:
    """Draw a plausible service time: lunch and dinner peaks (section 4.3.1)."""
    bucket = rng.choices(
        ["lunch", "afternoon", "dinner", "late"],
        weights=[30, 8, 55, 7],
    )[0]
    if bucket == "lunch":
        hour = rng.choices([11, 12, 13, 14], weights=[2, 5, 5, 2])[0]
    elif bucket == "afternoon":
        hour = rng.choice([15, 16, 17])
    elif bucket == "dinner":
        hour = rng.choices([18, 19, 20, 21], weights=[4, 6, 6, 3])[0]
    else:
        hour = rng.choice([22, 23])
    return datetime(day.year, day.month, day.day, hour, rng.randint(0, 59))


def _orders_for_day(day: date) -> int:
    base = rng.randint(26, 38)
    if day.isoweekday() in (5, 6):       # Friday, Saturday
        base = int(base * 1.45)
    elif day.isoweekday() == 7:          # Sunday
        base = int(base * 1.15)
    elif day.isoweekday() == 1:          # Monday
        base = int(base * 0.75)
    return base


def _pick_instrument(instruments, channel_code):
    """Third-party delivery settles on its own platform (section 4.2.1)."""
    if channel_code == "ubereats":
        return instruments["ubereats"]
    if channel_code == "doordash":
        return instruments["doordash"]
    if channel_code == "own_delivery":
        return rng.choices(
            [instruments["visa"], instruments["mastercard"], instruments["cash"],
             instruments["contactless"], instruments["etransfer"]],
            weights=[30, 20, 15, 25, 10],
        )[0]
    return rng.choices(
        [instruments["visa"], instruments["mastercard"], instruments["amex"],
         instruments["cash"], instruments["contactless"], instruments["etransfer"]],
        weights=[32, 20, 8, 12, 24, 4],
    )[0]


def _last4() -> str:
    return f"{rng.randint(0, 9999):04d}"


def _add_items(db, order, ref, count, seats):
    """Attach items to an order, some assigned to seats, some shared."""
    created = []
    for mi in _weighted_items(ref["items"], count):
        seat = rng.choice(seats) if seats else None
        item = OrderItem(
            order_id=order.id,
            menu_item_id=mi.id,
            seat_id=seat.id if seat else None,
            quantity=1,
            unit_price_cents=mi.price_cents,
            notes=rng.choice(SPECIAL_NOTES),
            kitchen_status=KitchenStatus.READY,
            is_shared=False,
        )
        db.add(item)
        db.flush()

        if rng.random() < 0.18:
            mod = rng.choice(ref["modifiers"])
            db.add(
                OrderItemModifier(
                    order_item_id=item.id, modifier_id=mod.id,
                    price_delta_cents=mod.price_delta_cents,
                )
            )
            db.flush()
        created.append((item, mi))
    return created


def _make_shared(db, order, created, seats):
    """Section 4.2.4 — split a shareable starter across the whole table."""
    if len(seats) < 2:
        return
    candidates = [item for item, mi in created if mi.is_shareable]
    if not candidates:
        return
    item = rng.choice(candidates)
    db.refresh(item)
    seat_ids = [s.id for s in seats]
    amounts = distribute(item.line_total_cents, [1] * len(seat_ids))
    item.is_shared = True
    item.seat_id = None
    # Append through the relationship so item.shares stays in sync — see
    # set_shared_item_shares() in services/payments.py.
    for sid, amt in zip(seat_ids, amounts):
        item.shares.append(SharedItemShare(seat_id=sid, share_cents=amt))
    db.flush()


def _settle_order(db, order, ref, when, seat_mode: str) -> None:
    """Create payments + allocations for a finished order."""
    db.refresh(order)
    instruments = ref["instruments"]
    channel_code = order.channel.code
    manager = next(s for s in ref["staff"] if s.role == Role.MANAGER)
    staff = order.waiter or next(s for s in ref["staff"] if s.role == Role.WAITER)

    # Outstanding value per item.
    claims: dict[int, list[tuple[int | None, int]]] = {}  # item_id -> [(seat_id, cents)]
    for item in order.items:
        if item.is_shared:
            if not item.shares:
                raise AssertionError(
                    f"Item {item.id} is flagged shared but carries no shares; "
                    "its value would be dropped from settlement."
                )
            claims[item.id] = [(sh.seat_id, sh.share_cents) for sh in item.shares]
        else:
            claims[item.id] = [(item.seat_id, item.line_total_cents)]

    if seat_mode == "single" or not order.seats:
        # One tender for the whole order.
        total_items = sum(amt for parts in claims.values() for _, amt in parts)
        if total_items == 0:
            return
        instrument = _pick_instrument(instruments, channel_code)
        tip = 0
        if channel_code == "dine_in":
            tip = pct(total_items, rng.choice([0, 15, 15, 18, 18, 20, 20]))
        elif channel_code == "own_delivery":
            tip = pct(total_items, rng.choice([0, 0, 10, 15]))

        discount_cents = 0
        if rng.random() < 0.05:
            discount_cents = pct(total_items, rng.choice([5, 10]))

        payment = Payment(
            order_id=order.id, seat_id=None, instrument_id=instrument.id,
            staff_id=staff.id, items_cents=total_items, tip_cents=tip,
            discount_cents=discount_cents,
            total_cents=total_items - discount_cents + tip,
            card_brand=instrument.card_brand,
            card_last4=_last4() if instrument.instrument_type in ("card", "contactless") else None,
            is_partial_close=False, created_at=when,
        )
        db.add(payment)
        db.flush()
        for item_id, parts in claims.items():
            for seat_id, amt in parts:
                db.add(
                    PaymentAllocation(
                        payment_id=payment.id, order_item_id=item_id,
                        seat_id=seat_id, amount_cents=amt,
                    )
                )
        if discount_cents:
            db.add(
                Discount(
                    order_id=order.id, kind="percent", value=10,
                    amount_cents=discount_cents, reason="Manager comp",
                    approved_by_id=manager.id, created_at=when,
                )
            )
        for s in order.seats:
            s.status = SeatStatus.PAID
        db.flush()
        return

    # Section 4.2.4 — every seat pays for its own items, own instrument.
    by_seat: dict[int, list[tuple[int, int]]] = {}
    for item_id, parts in claims.items():
        for seat_id, amt in parts:
            if seat_id is None:
                continue
            by_seat.setdefault(seat_id, []).append((item_id, amt))

    # Section 4.2.5 — occasionally one guest leaves early and closes their part.
    partial_seat_id = None
    if seat_mode == "seats" and len(by_seat) >= 3 and rng.random() < 0.18:
        partial_seat_id = rng.choice(list(by_seat))

    for offset, (seat_id, lines) in enumerate(by_seat.items()):
        items_cents = sum(amt for _, amt in lines)
        if items_cents == 0:
            continue
        instrument = _pick_instrument(instruments, channel_code)
        tip = pct(items_cents, rng.choice([0, 15, 15, 18, 18, 20, 20]))
        paid_at = when - timedelta(minutes=25) if seat_id == partial_seat_id else when + timedelta(minutes=offset)

        payment = Payment(
            order_id=order.id, seat_id=seat_id, instrument_id=instrument.id,
            staff_id=staff.id, items_cents=items_cents, tip_cents=tip,
            discount_cents=0, total_cents=items_cents + tip,
            card_brand=instrument.card_brand,
            card_last4=_last4() if instrument.instrument_type in ("card", "contactless") else None,
            is_partial_close=(seat_id == partial_seat_id),
            created_at=paid_at,
        )
        db.add(payment)
        db.flush()
        for item_id, amt in lines:
            db.add(
                PaymentAllocation(
                    payment_id=payment.id, order_item_id=item_id,
                    seat_id=seat_id, amount_cents=amt,
                )
            )
        seat = db.get(Seat, seat_id)
        seat.status = SeatStatus.PAID_PARTIAL if seat_id == partial_seat_id else SeatStatus.PAID
    db.flush()


def generate_history(db: Session, ref: dict) -> int:
    waiters = [s for s in ref["staff"] if s.role == Role.WAITER]
    drivers = [s for s in ref["staff"] if s.role == Role.DELIVERY_COORDINATOR][1:]
    channels = ref["channels"]
    tables = ref["tables"]

    today = date.today()
    start = today - timedelta(days=WEEKS_OF_HISTORY * 7)
    seq = 0
    made = 0

    day = start
    while day < today:
        for _ in range(_orders_for_day(day)):
            seq += 1
            when = _order_time(day)
            channel_code = rng.choices(
                ["dine_in", "ubereats", "doordash", "own_delivery"],
                weights=[58, 16, 15, 11],
            )[0]
            channel = channels[channel_code]
            is_dine_in = channel_code == "dine_in"

            waiter = rng.choice(waiters) if is_dine_in else None
            table = rng.choice(tables) if is_dine_in else None
            guests = rng.choices([1, 2, 2, 3, 4, 4, 5, 6], weights=[6, 22, 18, 14, 16, 10, 8, 6])[0] if is_dine_in else 1

            order = Order(
                code=f"ORD-{day.strftime('%y%m%d')}-{seq:05d}",
                table_id=table.id if table else None,
                channel_id=channel.id,
                waiter_id=waiter.id if waiter else None,
                status=OrderStatus.PAID,
                kitchen_status=KitchenStatus.READY,
                guest_count=guests,
                opened_at=when,
                sent_to_kitchen_at=when + timedelta(minutes=rng.randint(2, 8)),
                ready_at=when + timedelta(minutes=rng.randint(12, 30)),
                closed_at=when + timedelta(minutes=rng.randint(40, 95)),
            )
            db.add(order)
            db.flush()

            seats = []
            if is_dine_in:
                for n in range(1, guests + 1):
                    s = Seat(order_id=order.id, seat_number=n, label=f"Seat {n}")
                    db.add(s)
                    seats.append(s)
                db.flush()

            n_items = max(1, int(rng.gauss(guests * 2.4, 1.2))) if is_dine_in else rng.randint(1, 4)
            created = _add_items(db, order, ref, n_items, seats)

            if is_dine_in and rng.random() < 0.35:
                _make_shared(db, order, created, seats)

            if not is_dine_in:
                delivered = order.closed_at
                d = DeliveryOrder(
                    order_id=order.id,
                    platform_ref=(
                        f"{'UE' if channel_code == 'ubereats' else 'DD'}-{rng.randint(100000, 999999)}"
                        if channel.is_third_party else None
                    ),
                    driver_id=rng.choice(drivers).id if channel_code == "own_delivery" else None,
                    status=DeliveryStatus.DELIVERED,
                    customer_name=rng.choice(CUSTOMERS),
                    customer_phone=f"+1 {rng.randint(200,999)}-{rng.randint(200,999)}-{rng.randint(1000,9999)}",
                    address=rng.choice(STREETS),
                    assigned_at=order.ready_at,
                    delivered_at=delivered,
                )
                db.add(d)
                db.flush()

            # Dine-in tables mostly settle per seat (4.2.4); solo diners and
            # delivery settle with a single tender.
            if is_dine_in and guests >= 2:
                mode = "seats" if rng.random() < 0.55 else "single"
            else:
                mode = "single"

            _settle_order(db, order, ref, order.closed_at, mode)
            made += 1
        db.commit()
        day += timedelta(days=1)

    db.commit()
    return made


def create_live_service(db: Session, ref: dict) -> None:
    """Leave a few tables mid-service so the floor plan isn't empty on first run."""
    waiters = [s for s in ref["staff"] if s.role == Role.WAITER]
    channels = ref["channels"]
    tables = ref["tables"]
    now = datetime.now()
    chosen = rng.sample(tables, 6)

    for idx, table in enumerate(chosen):
        waiter = rng.choice(waiters)
        guests = rng.choice([2, 3, 4, 4, 5])
        opened = now - timedelta(minutes=rng.randint(8, 55))
        order = Order(
            code=f"ORD-LIVE-{idx + 1:03d}",
            table_id=table.id,
            channel_id=channels["dine_in"].id,
            waiter_id=waiter.id,
            status=OrderStatus.OPEN,
            guest_count=guests,
            opened_at=opened,
        )
        db.add(order)
        db.flush()

        seats = []
        for n in range(1, guests + 1):
            s = Seat(order_id=order.id, seat_number=n, label=f"Seat {n}")
            db.add(s)
            seats.append(s)
        db.flush()

        created = _add_items(db, order, ref, guests * 2, seats)
        if rng.random() < 0.5:
            _make_shared(db, order, created, seats)

        # Spread the table across the kitchen workflow (4.1.3).
        stage = idx % 3
        if stage == 0:
            order.kitchen_status = KitchenStatus.PENDING
            table.status = TableStatus.OCCUPIED
            for item, _ in created:
                item.kitchen_status = KitchenStatus.PENDING
        elif stage == 1:
            order.kitchen_status = KitchenStatus.PREPARING
            order.sent_to_kitchen_at = opened + timedelta(minutes=4)
            order.status = OrderStatus.PREPARING
            table.status = TableStatus.OCCUPIED
            for item, _ in created:
                item.kitchen_status = KitchenStatus.PREPARING
        else:
            order.kitchen_status = KitchenStatus.READY
            order.sent_to_kitchen_at = opened + timedelta(minutes=4)
            order.ready_at = opened + timedelta(minutes=18)
            order.status = OrderStatus.READY
            table.status = TableStatus.READY_TO_PAY
            for item, _ in created:
                item.kitchen_status = KitchenStatus.READY

        table.current_waiter_id = waiter.id
        db.flush()

    # A couple of live delivery orders in the queue (4.1.4).
    drivers = [s for s in ref["staff"] if s.role == Role.DELIVERY_COORDINATOR][1:]
    for idx, code in enumerate(["ubereats", "doordash", "own_delivery"]):
        ch = channels[code]
        opened = now - timedelta(minutes=rng.randint(3, 25))
        order = Order(
            code=f"ORD-DLV-{idx + 1:03d}",
            channel_id=ch.id,
            status=OrderStatus.PREPARING,
            kitchen_status=KitchenStatus.PREPARING if idx else KitchenStatus.PENDING,
            guest_count=1,
            opened_at=opened,
            sent_to_kitchen_at=opened + timedelta(minutes=2),
        )
        db.add(order)
        db.flush()
        _add_items(db, order, ref, rng.randint(2, 4), [])
        db.add(
            DeliveryOrder(
                order_id=order.id,
                platform_ref=(
                    f"{'UE' if code == 'ubereats' else 'DD'}-{rng.randint(100000, 999999)}"
                    if ch.is_third_party else None
                ),
                driver_id=rng.choice(drivers).id if code == "own_delivery" else None,
                status=DeliveryStatus.PREPARING if idx else DeliveryStatus.PENDING,
                customer_name=rng.choice(CUSTOMERS),
                customer_phone=f"+1 {rng.randint(200,999)}-{rng.randint(200,999)}-{rng.randint(1000,9999)}",
                address=rng.choice(STREETS),
            )
        )
        db.flush()
    db.commit()


def main(reset: bool = True) -> None:
    if reset:
        print("Resetting database ...")
        reset_database()

    db = SessionLocal()
    try:
        print("Seeding reference data ...")
        ref = seed_reference(db)
        db.commit()
        print(
            f"  {len(ref['tables'])} tables, {len(ref['staff'])} staff, "
            f"{len(ref['items'])} menu items, {len(ref['instruments'])} payment instruments"
        )

        print(f"Generating {WEEKS_OF_HISTORY} weeks of trading history ...")
        n = generate_history(db, ref)
        print(f"  {n} closed orders")

        print("Opening live service ...")
        create_live_service(db, ref)
        print("  6 tables in service, 3 delivery orders in queue")
    finally:
        db.close()


if __name__ == "__main__":
    main()
