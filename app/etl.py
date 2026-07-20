"""ETL: normalized OLTP -> dimensional star schema.

Runs after service (or on demand from the Reports screen). Three stages:

  1. Calendar dimensions  — generated, not sourced
  2. Conformed dimensions — SCD Type 1 and Type 2 loads from operational tables
  3. Facts                — closed orders transformed to the declared grains

Idempotent: re-running loads only orders that are not already in the facts, so
it is safe to run repeatedly. run_etl(full_refresh=True) rebuilds from scratch.

Type 2 lookups are *as-of* the transaction time, not "current". That is the
whole point of keeping history: a sale made while Marie was a waiter must stay
attributed to waiter-Marie even after she is promoted to manager.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.oltp import (
    Channel,
    Discount,
    MenuItem,
    Order,
    OrderStatus,
    Payment,
    RestaurantTable,
    SeatStatus,
    Staff,
)
from app.models.star import (
    UNKNOWN_KEY,
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
from app.models.oltp import PaymentInstrument
from app.services.money import distribute

# Initial-load effective date. New natural keys start here rather than "now",
# so historical facts can resolve their as-of dimension version.
EPOCH = datetime(2000, 1, 1)
UNKNOWN_DATE = date(1900, 1, 1)


# --------------------------------------------------------------------------
# Stage 1 — calendar dimensions
# --------------------------------------------------------------------------

def date_key_of(d: date | datetime) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def time_key_of(t: datetime) -> int:
    return t.hour * 100 + t.minute


def _service_period(hour: int) -> str:
    if hour < 11:
        return "Breakfast"
    if hour < 15:
        return "Lunch"
    if hour < 17:
        return "Afternoon"
    if hour < 22:
        return "Dinner"
    return "Late night"


def _shift(hour: int) -> str:
    """Section 4.3.4 — staff performance filterable by shift."""
    if hour < 12:
        return "Morning"
    if hour < 17:
        return "Afternoon"
    if hour < 23:
        return "Evening"
    return "Late"


def build_dim_date(db: Session, start: date, end: date) -> int:
    existing = {k for (k,) in db.execute(select(DimDate.date_key)).all()}

    if UNKNOWN_KEY not in existing:
        db.add(
            DimDate(
                date_key=UNKNOWN_KEY, full_date=UNKNOWN_DATE, year=1900, quarter=1,
                month=1, month_name="Unknown", day=1, day_of_week=1,
                day_name="Unknown", week_of_year=1, is_weekend=False,
            )
        )
        existing.add(UNKNOWN_KEY)

    added = 0
    cur = start
    while cur <= end:
        key = date_key_of(cur)
        if key not in existing:
            iso = cur.isocalendar()
            db.add(
                DimDate(
                    date_key=key,
                    full_date=cur,
                    year=cur.year,
                    quarter=(cur.month - 1) // 3 + 1,
                    month=cur.month,
                    month_name=cur.strftime("%B"),
                    day=cur.day,
                    day_of_week=cur.isoweekday(),
                    day_name=cur.strftime("%A"),
                    week_of_year=iso.week,
                    is_weekend=cur.isoweekday() >= 6,
                )
            )
            added += 1
        cur += timedelta(days=1)
    db.flush()
    return added


def build_dim_time(db: Session) -> int:
    """1,440 rows at minute grain, plus the unknown member."""
    existing = {k for (k,) in db.execute(select(DimTime.time_key)).all()}

    if UNKNOWN_KEY not in existing:
        db.add(
            DimTime(
                time_key=UNKNOWN_KEY, hour=0, minute=0, hour_label="Unknown",
                service_period="Unknown", shift="Unknown",
            )
        )

    added = 0
    for hour in range(24):
        for minute in range(60):
            key = hour * 100 + minute
            if key in existing:
                continue
            db.add(
                DimTime(
                    time_key=key,
                    hour=hour,
                    minute=minute,
                    hour_label=f"{hour:02d}:00",
                    service_period=_service_period(hour),
                    shift=_shift(hour),
                )
            )
            added += 1
    db.flush()
    return added


# --------------------------------------------------------------------------
# Stage 2 — conformed dimensions
# --------------------------------------------------------------------------

def _ensure_unknown(db: Session, model, **fields) -> None:
    pk = list(model.__table__.primary_key.columns)[0]
    exists = db.execute(select(model).where(pk == UNKNOWN_KEY)).scalar_one_or_none()
    if exists is None:
        db.add(model(**{pk.name: UNKNOWN_KEY}, **fields))
        db.flush()


def load_dim_staff(db: Session, now: datetime | None = None) -> tuple[int, int]:
    """Type 2. Returns (inserted, versioned)."""
    now = now or datetime.now()
    _ensure_unknown(
        db, DimStaff, staff_id=UNKNOWN_KEY, name="Unknown / Not applicable",
        role="unknown", is_active=False, valid_from=EPOCH, valid_to=None, is_current=True,
    )

    inserted = versioned = 0
    for staff in db.execute(select(Staff)).scalars():
        current = db.execute(
            select(DimStaff).where(
                DimStaff.staff_id == staff.id, DimStaff.is_current.is_(True)
            )
        ).scalar_one_or_none()

        if current is None:
            db.add(
                DimStaff(
                    staff_id=staff.id, name=staff.name, role=staff.role,
                    is_active=staff.is_active, valid_from=EPOCH, is_current=True,
                )
            )
            inserted += 1
            continue

        changed = (
            current.name != staff.name
            or current.role != staff.role
            or current.is_active != staff.is_active
        )
        if changed:
            current.valid_to = now
            current.is_current = False
            db.add(
                DimStaff(
                    staff_id=staff.id, name=staff.name, role=staff.role,
                    is_active=staff.is_active, valid_from=now, is_current=True,
                )
            )
            versioned += 1
    db.flush()
    return inserted, versioned


def load_dim_menu_item(db: Session, now: datetime | None = None) -> tuple[int, int]:
    """Type 2 — a repriced item must not rewrite last month's revenue."""
    now = now or datetime.now()
    _ensure_unknown(
        db, DimMenuItem, menu_item_id=UNKNOWN_KEY, item_name="Unknown",
        category_name="Unknown", price_cents=0, is_shareable=False,
        valid_from=EPOCH, valid_to=None, is_current=True,
    )

    inserted = versioned = 0
    rows = db.execute(select(MenuItem)).scalars().all()
    for item in rows:
        category_name = item.category.name if item.category else "Uncategorized"
        current = db.execute(
            select(DimMenuItem).where(
                DimMenuItem.menu_item_id == item.id, DimMenuItem.is_current.is_(True)
            )
        ).scalar_one_or_none()

        if current is None:
            db.add(
                DimMenuItem(
                    menu_item_id=item.id, item_name=item.name,
                    category_name=category_name, price_cents=item.price_cents,
                    is_shareable=item.is_shareable, valid_from=EPOCH, is_current=True,
                )
            )
            inserted += 1
            continue

        changed = (
            current.item_name != item.name
            or current.category_name != category_name
            or current.price_cents != item.price_cents
        )
        if changed:
            current.valid_to = now
            current.is_current = False
            db.add(
                DimMenuItem(
                    menu_item_id=item.id, item_name=item.name,
                    category_name=category_name, price_cents=item.price_cents,
                    is_shareable=item.is_shareable, valid_from=now, is_current=True,
                )
            )
            versioned += 1
    db.flush()
    return inserted, versioned


def load_dim_table(db: Session) -> int:
    """Type 1 — overwrite in place."""
    _ensure_unknown(
        db, DimTable, table_id=UNKNOWN_KEY, table_number=0,
        zone="Not applicable", capacity=0,
    )
    n = 0
    for t in db.execute(select(RestaurantTable)).scalars():
        dim = db.execute(
            select(DimTable).where(DimTable.table_id == t.id)
        ).scalar_one_or_none()
        if dim is None:
            db.add(
                DimTable(table_id=t.id, table_number=t.number, zone=t.zone, capacity=t.capacity)
            )
            n += 1
        else:
            dim.table_number, dim.zone, dim.capacity = t.number, t.zone, t.capacity
    db.flush()
    return n


def load_dim_channel(db: Session) -> int:
    _ensure_unknown(
        db, DimChannel, channel_id=UNKNOWN_KEY, channel_code="unknown",
        channel_name="Unknown", channel_type="unknown", is_third_party=False,
    )
    n = 0
    for c in db.execute(select(Channel)).scalars():
        dim = db.execute(
            select(DimChannel).where(DimChannel.channel_id == c.id)
        ).scalar_one_or_none()
        if dim is None:
            db.add(
                DimChannel(
                    channel_id=c.id, channel_code=c.code, channel_name=c.name,
                    channel_type=c.channel_type, is_third_party=c.is_third_party,
                )
            )
            n += 1
        else:
            dim.channel_code, dim.channel_name = c.code, c.name
            dim.channel_type, dim.is_third_party = c.channel_type, c.is_third_party
    db.flush()
    return n


def _method_group(instr: PaymentInstrument) -> str:
    """Pre-computed grouping for the 4.3.3 breakdown: Card/Cash/E-transfer/platform."""
    if instr.instrument_type in ("card", "contactless"):
        return "Card"
    if instr.instrument_type == "cash":
        return "Cash"
    if instr.instrument_type == "etransfer":
        return "E-transfer"
    return instr.name  # UberEats / DoorDash reported by name


def load_dim_payment_instrument(db: Session) -> int:
    _ensure_unknown(
        db, DimPaymentInstrument, instrument_id=UNKNOWN_KEY, instrument_code="unknown",
        instrument_name="Unknown", instrument_type="unknown", card_brand="N/A",
        method_group="Unknown", is_card=False, is_third_party=False,
    )
    n = 0
    for i in db.execute(select(PaymentInstrument)).scalars():
        dim = db.execute(
            select(DimPaymentInstrument).where(
                DimPaymentInstrument.instrument_id == i.id
            )
        ).scalar_one_or_none()
        payload = dict(
            instrument_code=i.code,
            instrument_name=i.name,
            instrument_type=i.instrument_type,
            card_brand=i.card_brand or "N/A",
            method_group=_method_group(i),
            is_card=i.instrument_type in ("card", "contactless"),
            is_third_party=i.is_third_party,
        )
        if dim is None:
            db.add(DimPaymentInstrument(instrument_id=i.id, **payload))
            n += 1
        else:
            for k, v in payload.items():
                setattr(dim, k, v)
    db.flush()
    return n


# --------------------------------------------------------------------------
# As-of dimension lookups
# --------------------------------------------------------------------------

def staff_key_asof(db: Session, staff_id: int | None, at: datetime) -> int:
    if staff_id is None:
        return UNKNOWN_KEY
    row = db.execute(
        select(DimStaff.staff_key).where(
            DimStaff.staff_id == staff_id,
            DimStaff.valid_from <= at,
            (DimStaff.valid_to.is_(None)) | (DimStaff.valid_to > at),
        )
    ).scalar_one_or_none()
    return row if row is not None else UNKNOWN_KEY


def item_key_asof(db: Session, menu_item_id: int, at: datetime) -> int:
    row = db.execute(
        select(DimMenuItem.item_key).where(
            DimMenuItem.menu_item_id == menu_item_id,
            DimMenuItem.valid_from <= at,
            (DimMenuItem.valid_to.is_(None)) | (DimMenuItem.valid_to > at),
        )
    ).scalar_one_or_none()
    return row if row is not None else UNKNOWN_KEY


def table_key_of(db: Session, table_id: int | None) -> int:
    if table_id is None:
        return UNKNOWN_KEY
    row = db.execute(
        select(DimTable.table_key).where(DimTable.table_id == table_id)
    ).scalar_one_or_none()
    return row if row is not None else UNKNOWN_KEY


def channel_key_of(db: Session, channel_id: int | None) -> int:
    if channel_id is None:
        return UNKNOWN_KEY
    row = db.execute(
        select(DimChannel.channel_key).where(DimChannel.channel_id == channel_id)
    ).scalar_one_or_none()
    return row if row is not None else UNKNOWN_KEY


def instrument_key_of(db: Session, instrument_id: int | None) -> int:
    if instrument_id is None:
        return UNKNOWN_KEY
    row = db.execute(
        select(DimPaymentInstrument.instrument_key).where(
            DimPaymentInstrument.instrument_id == instrument_id
        )
    ).scalar_one_or_none()
    return row if row is not None else UNKNOWN_KEY


# --------------------------------------------------------------------------
# Stage 3 — facts
# --------------------------------------------------------------------------

CLOSED_STATUSES = (OrderStatus.PAID, OrderStatus.CLOSED)


def _order_discount_total(db: Session, order_id: int) -> int:
    return db.execute(
        select(func.coalesce(func.sum(Discount.amount_cents), 0)).where(
            Discount.order_id == order_id
        )
    ).scalar_one()


def load_facts_for_order(db: Session, order: Order) -> tuple[int, int]:
    """Transform one closed order into all three facts. Returns (items, payments)."""
    opened = order.opened_at
    d_key = date_key_of(opened)
    t_key = time_key_of(opened)
    ch_key = channel_key_of(db, order.channel_id)
    tbl_key = table_key_of(db, order.table_id)
    staff_key = staff_key_asof(db, order.waiter_id, opened)

    # ---- fact_order_item -------------------------------------------------
    # Order-level discounts are pushed down to items in proportion to line
    # value so that net_cents stays additive and reconciles to the header.
    items = list(order.items)
    gross_by_item = [i.line_total_cents for i in items]
    disc_total = _order_discount_total(db, order.id)
    disc_by_item = distribute(disc_total, gross_by_item) if items else []

    item_rows = 0
    for idx, item in enumerate(items):
        gross = gross_by_item[idx]
        disc = disc_by_item[idx] if disc_by_item else 0
        db.add(
            FactOrderItem(
                date_key=d_key,
                time_key=t_key,
                item_key=item_key_asof(db, item.menu_item_id, opened),
                staff_key=staff_key,
                table_key=tbl_key,
                channel_key=ch_key,
                order_id=order.id,
                order_code=order.code,
                order_item_id=item.id,
                seat_number=item.seat.seat_number if item.seat else None,
                quantity=item.quantity,
                unit_price_cents=item.unit_price_cents,
                modifier_cents=item.modifier_total_cents * item.quantity,
                gross_cents=gross,
                discount_cents=disc,
                net_cents=gross - disc,
                is_shared=item.is_shared,
            )
        )
        item_rows += 1

    # ---- fact_payment ----------------------------------------------------
    # Grain is the allocation, so tip and discount (recorded on the payment)
    # are pushed down proportionally to keep them additive at this grain.
    pay_rows = 0
    for payment in order.payments:
        allocs = list(payment.allocations)
        if not allocs:
            continue
        amounts = [a.amount_cents for a in allocs]
        tips = distribute(payment.tip_cents, amounts)
        discounts = distribute(payment.discount_cents, amounts)

        p_date = date_key_of(payment.created_at)
        p_time = time_key_of(payment.created_at)
        p_staff = staff_key_asof(db, payment.staff_id, payment.created_at)
        i_key = instrument_key_of(db, payment.instrument_id)

        for j, alloc in enumerate(allocs):
            item = alloc.order_item
            db.add(
                FactPayment(
                    date_key=p_date,
                    time_key=p_time,
                    instrument_key=i_key,
                    staff_key=p_staff,
                    channel_key=ch_key,
                    item_key=item_key_asof(db, item.menu_item_id, opened),
                    table_key=tbl_key,
                    order_id=order.id,
                    order_code=order.code,
                    payment_id=payment.id,
                    order_item_id=alloc.order_item_id,
                    seat_number=(
                        alloc.seat.seat_number if getattr(alloc, "seat", None) else None
                    ),
                    card_last4=payment.card_last4,
                    amount_cents=alloc.amount_cents,
                    tip_cents=tips[j],
                    discount_cents=discounts[j],
                    total_cents=alloc.amount_cents - discounts[j] + tips[j],
                    is_partial_close=payment.is_partial_close,
                )
            )
            pay_rows += 1

    # ---- fact_order_header ----------------------------------------------
    subtotal = sum(gross_by_item)
    tip_total = sum(p.tip_cents for p in order.payments)
    total = sum(p.total_cents for p in order.payments)
    closed = order.closed_at or opened
    duration = max(0, int((closed - opened).total_seconds() // 60))

    db.add(
        FactOrderHeader(
            date_key=d_key,
            time_key=t_key,
            close_date_key=date_key_of(closed),
            close_time_key=time_key_of(closed),
            staff_key=staff_key,
            table_key=tbl_key,
            channel_key=ch_key,
            order_id=order.id,
            order_code=order.code,
            guest_count=order.guest_count,
            item_count=len(items),
            seats_paid=sum(
                1 for s in order.seats
                if s.status in (SeatStatus.PAID, SeatStatus.PAID_PARTIAL)
            ),
            payment_count=len(order.payments),
            distinct_instruments=len({p.instrument_id for p in order.payments}),
            had_partial_close=any(p.is_partial_close for p in order.payments),
            subtotal_cents=subtotal,
            discount_cents=disc_total,
            tip_cents=tip_total,
            total_cents=total,
            duration_minutes=duration,
        )
    )
    return item_rows, pay_rows


def run_etl(db: Session, full_refresh: bool = False, verbose: bool = True) -> dict:
    """Run the full pipeline. Safe to call repeatedly."""
    started = datetime.now()

    if full_refresh:
        db.execute(delete(FactPayment))
        db.execute(delete(FactOrderItem))
        db.execute(delete(FactOrderHeader))
        db.flush()

    # Stage 1 — calendar. Span the actual order history, padded a little.
    first = db.execute(select(func.min(Order.opened_at))).scalar_one_or_none()
    last = db.execute(select(func.max(Order.opened_at))).scalar_one_or_none()
    start = (first.date() if first else date.today()) - timedelta(days=1)
    end = (last.date() if last else date.today()) + timedelta(days=365)
    dates_added = build_dim_date(db, start, end)
    times_added = build_dim_time(db)

    # Stage 2 — dimensions.
    staff_ins, staff_ver = load_dim_staff(db)
    item_ins, item_ver = load_dim_menu_item(db)
    tables_added = load_dim_table(db)
    channels_added = load_dim_channel(db)
    instruments_added = load_dim_payment_instrument(db)

    # Stage 3 — facts for closed orders not yet loaded.
    already = {
        oid for (oid,) in db.execute(select(FactOrderHeader.order_id)).all()
    }
    orders = db.execute(
        select(Order).where(Order.status.in_(CLOSED_STATUSES)).order_by(Order.id)
    ).scalars().all()
    pending = [o for o in orders if o.id not in already]

    total_items = total_pays = 0
    for order in pending:
        i, p = load_facts_for_order(db, order)
        total_items += i
        total_pays += p

    wm = db.execute(
        select(EtlWatermark).where(EtlWatermark.process_name == "star_load")
    ).scalar_one_or_none()
    if wm is None:
        wm = EtlWatermark(process_name="star_load", last_run_at=started)
        db.add(wm)
    wm.last_run_at = started
    wm.last_order_id = max([o.id for o in pending], default=wm.last_order_id or 0)
    wm.rows_loaded = (wm.rows_loaded or 0) + total_items + total_pays

    db.commit()

    stats = {
        "orders_loaded": len(pending),
        "fact_order_item_rows": total_items,
        "fact_payment_rows": total_pays,
        "dim_date_added": dates_added,
        "dim_time_added": times_added,
        "dim_staff_inserted": staff_ins,
        "dim_staff_versioned": staff_ver,
        "dim_menu_item_inserted": item_ins,
        "dim_menu_item_versioned": item_ver,
        "dim_table_added": tables_added,
        "dim_channel_added": channels_added,
        "dim_payment_instrument_added": instruments_added,
        "elapsed_seconds": round((datetime.now() - started).total_seconds(), 2),
    }
    if verbose:
        print("ETL complete:")
        for k, v in stats.items():
            print(f"  {k:32} {v}")
    return stats
