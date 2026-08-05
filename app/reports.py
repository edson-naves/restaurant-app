"""Reports & Analytics — section 4.3.

Every query here reads the star schema, never the OLTP tables. That is the
point of building the dimensional layer: each report is a single fact table
scanned against small dimensions, so adding a filter (date range, category,
channel, shift) is a WHERE clause rather than another join through the
operational model.

Averages are always computed as SUM/COUNT at query time. Storing a pre-computed
average in a fact would be wrong — averages are not additive, so they cannot be
rolled up across rows.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import Integer, case, func, select
from sqlalchemy.orm import Session

from app.models.oltp import Shift, Staff, SwapRequest

from app.models.star import (
    DimChannel,
    DimDate,
    DimMenuItem,
    DimPaymentInstrument,
    DimStaff,
    DimTime,
    FactOrderHeader,
    FactOrderItem,
    FactPayment,
)


def dkey(d: date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


# --------------------------------------------------------------------------
# 4.3.1  Daily sales summary
# --------------------------------------------------------------------------

def daily_sales_summary(db: Session, day: date) -> dict:
    """Revenue is keyed on the settlement date, so it ties to the cash drawer."""
    k = dkey(day)

    row = db.execute(
        select(
            func.coalesce(func.sum(FactOrderHeader.total_cents), 0),
            func.count(FactOrderHeader.order_sk),
            func.coalesce(func.sum(FactOrderHeader.tip_cents), 0),
            func.coalesce(func.sum(FactOrderHeader.discount_cents), 0),
            func.coalesce(func.sum(FactOrderHeader.guest_count), 0),
        ).where(FactOrderHeader.close_date_key == k)
    ).one()
    revenue_cents, orders, tips_cents, discounts_cents, guests = row

    # Dine-in vs delivery counts.
    channel_rows = db.execute(
        select(
            DimChannel.channel_type,
            func.count(FactOrderHeader.order_sk),
            func.coalesce(func.sum(FactOrderHeader.total_cents), 0),
        )
        .join(DimChannel, DimChannel.channel_key == FactOrderHeader.channel_key)
        .where(FactOrderHeader.close_date_key == k)
        .group_by(DimChannel.channel_type)
    ).all()
    by_type = {t: {"orders": c, "revenue_cents": r} for t, c, r in channel_rows}

    # Peak hours chart — orders by the hour they were PLACED (time_key, the
    # open time), across the orders that traded on this business day. This is
    # a demand curve for staffing, not a revenue curve.
    hour_rows = db.execute(
        select(
            DimTime.hour,
            func.count(FactOrderHeader.order_sk),
            func.coalesce(func.sum(FactOrderHeader.total_cents), 0),
        )
        .join(DimTime, DimTime.time_key == FactOrderHeader.time_key)
        .where(FactOrderHeader.close_date_key == k)
        .group_by(DimTime.hour)
        .order_by(DimTime.hour)
    ).all()

    return {
        "date": day,
        "revenue_cents": revenue_cents,
        "orders": orders,
        "tips_cents": tips_cents,
        "discounts_cents": discounts_cents,
        "guests": guests,
        "avg_ticket_cents": (revenue_cents // orders) if orders else 0,
        "dine_in_orders": by_type.get("dine_in", {}).get("orders", 0),
        "delivery_orders": by_type.get("delivery", {}).get("orders", 0),
        "dine_in_revenue_cents": by_type.get("dine_in", {}).get("revenue_cents", 0),
        "delivery_revenue_cents": by_type.get("delivery", {}).get("revenue_cents", 0),
        "by_hour": [
            {"hour": h, "orders": c, "revenue_cents": r} for h, c, r in hour_rows
        ],
    }


def revenue_trend(db: Session, start: date, end: date) -> list[dict]:
    rows = db.execute(
        select(
            DimDate.full_date,
            DimDate.day_name,
            func.coalesce(func.sum(FactOrderHeader.total_cents), 0),
            func.count(FactOrderHeader.order_sk),
        )
        .join(DimDate, DimDate.date_key == FactOrderHeader.close_date_key)
        .where(FactOrderHeader.close_date_key.between(dkey(start), dkey(end)))
        .group_by(DimDate.full_date, DimDate.day_name)
        .order_by(DimDate.full_date)
    ).all()
    return [
        {"date": d, "day_name": dn, "revenue_cents": r, "orders": o}
        for d, dn, r, o in rows
    ]


# --------------------------------------------------------------------------
# 4.3.2  Best selling items
# --------------------------------------------------------------------------

def best_sellers(
    db: Session,
    start: date,
    end: date,
    category: str | None = None,
    channel_type: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Ranked by volume and revenue, filterable by date, category and channel."""
    q = (
        select(
            DimMenuItem.item_name,
            DimMenuItem.category_name,
            func.sum(FactOrderItem.quantity).label("qty"),
            func.sum(FactOrderItem.net_cents).label("revenue"),
            func.count(FactOrderItem.order_item_sk).label("times_ordered"),
        )
        .join(DimMenuItem, DimMenuItem.item_key == FactOrderItem.item_key)
        .join(DimChannel, DimChannel.channel_key == FactOrderItem.channel_key)
        .where(FactOrderItem.date_key.between(dkey(start), dkey(end)))
        .group_by(DimMenuItem.item_name, DimMenuItem.category_name)
        .order_by(func.sum(FactOrderItem.quantity).desc())
        .limit(limit)
    )
    if category:
        q = q.where(DimMenuItem.category_name == category)
    if channel_type:
        q = q.where(DimChannel.channel_type == channel_type)

    return [
        {
            "item_name": n,
            "category": c,
            "quantity": int(qty),
            "revenue_cents": int(rev),
            "times_ordered": int(t),
        }
        for n, c, qty, rev, t in db.execute(q).all()
    ]


# --------------------------------------------------------------------------
# 4.3.3  Payment breakdown
# --------------------------------------------------------------------------

def payment_breakdown(db: Session, start: date, end: date) -> dict:
    """Revenue by method, with Visa/Mastercard/Amex detail (4.3.3)."""
    method_rows = db.execute(
        select(
            DimPaymentInstrument.method_group,
            func.coalesce(func.sum(FactPayment.total_cents), 0),
            func.coalesce(func.sum(FactPayment.tip_cents), 0),
            func.count(func.distinct(FactPayment.payment_id)),
        )
        .join(
            DimPaymentInstrument,
            DimPaymentInstrument.instrument_key == FactPayment.instrument_key,
        )
        .where(FactPayment.date_key.between(dkey(start), dkey(end)))
        .group_by(DimPaymentInstrument.method_group)
        .order_by(func.sum(FactPayment.total_cents).desc())
    ).all()

    card_rows = db.execute(
        select(
            DimPaymentInstrument.card_brand,
            func.coalesce(func.sum(FactPayment.total_cents), 0),
            func.count(func.distinct(FactPayment.payment_id)),
        )
        .join(
            DimPaymentInstrument,
            DimPaymentInstrument.instrument_key == FactPayment.instrument_key,
        )
        .where(
            FactPayment.date_key.between(dkey(start), dkey(end)),
            DimPaymentInstrument.is_card.is_(True),
        )
        .group_by(DimPaymentInstrument.card_brand)
        .order_by(func.sum(FactPayment.total_cents).desc())
    ).all()

    instrument_rows = db.execute(
        select(
            DimPaymentInstrument.instrument_name,
            DimPaymentInstrument.instrument_type,
            func.coalesce(func.sum(FactPayment.total_cents), 0),
            func.count(func.distinct(FactPayment.payment_id)),
        )
        .join(
            DimPaymentInstrument,
            DimPaymentInstrument.instrument_key == FactPayment.instrument_key,
        )
        .where(FactPayment.date_key.between(dkey(start), dkey(end)))
        .group_by(DimPaymentInstrument.instrument_name, DimPaymentInstrument.instrument_type)
        .order_by(func.sum(FactPayment.total_cents).desc())
    ).all()

    total = sum(r[1] for r in method_rows)
    return {
        "total_cents": total,
        "by_method": [
            {
                "method": m,
                "revenue_cents": rev,
                "tips_cents": tips,
                "payments": n,
                "share_pct": round(rev * 100 / total, 1) if total else 0.0,
            }
            for m, rev, tips, n in method_rows
        ],
        "by_card_brand": [
            {"brand": b, "revenue_cents": rev, "payments": n} for b, rev, n in card_rows
        ],
        "by_instrument": [
            {"instrument": i, "type": t, "revenue_cents": rev, "payments": n}
            for i, t, rev, n in instrument_rows
        ],
    }


def payment_history_rows(db: Session, start: date, end: date) -> list[tuple]:
    """Flat rows for the accounting CSV export (4.3.3 / end-of-day step 5)."""
    return db.execute(
        select(
            DimDate.full_date,
            DimTime.hour_label,
            FactPayment.order_code,
            FactPayment.payment_id,
            DimChannel.channel_name,
            DimMenuItem.item_name,
            DimPaymentInstrument.instrument_name,
            DimPaymentInstrument.card_brand,
            FactPayment.card_last4,
            DimStaff.name,
            FactPayment.seat_number,
            FactPayment.amount_cents,
            FactPayment.discount_cents,
            FactPayment.tip_cents,
            FactPayment.total_cents,
            FactPayment.is_partial_close,
        )
        .join(DimDate, DimDate.date_key == FactPayment.date_key)
        .join(DimTime, DimTime.time_key == FactPayment.time_key)
        .join(DimChannel, DimChannel.channel_key == FactPayment.channel_key)
        .join(DimMenuItem, DimMenuItem.item_key == FactPayment.item_key)
        .join(
            DimPaymentInstrument,
            DimPaymentInstrument.instrument_key == FactPayment.instrument_key,
        )
        .join(DimStaff, DimStaff.staff_key == FactPayment.staff_key)
        .where(FactPayment.date_key.between(dkey(start), dkey(end)))
        .order_by(DimDate.full_date, FactPayment.payment_id)
    ).all()


# --------------------------------------------------------------------------
# 4.3.4  Staff performance
# --------------------------------------------------------------------------

def staff_performance(
    db: Session, start: date, end: date, shift: str | None = None
) -> list[dict]:
    """Orders, revenue and tips per staff member, filterable by date and shift.

    All three come from the header fact, keyed by the order's server: the tip
    belongs to whoever served the table, not whoever happened to run the card.
    Each order's tips are already rolled up onto its header row (tip_cents), so
    a server who took the order but had a manager close it still gets the tip.
    """
    hq = (
        select(
            DimStaff.staff_key,
            DimStaff.name,
            DimStaff.role,
            func.count(FactOrderHeader.order_sk).label("orders"),
            func.coalesce(func.sum(FactOrderHeader.total_cents), 0).label("revenue"),
            func.coalesce(func.sum(FactOrderHeader.guest_count), 0).label("guests"),
            func.coalesce(func.sum(FactOrderHeader.tip_cents), 0).label("tips"),
        )
        .join(DimStaff, DimStaff.staff_key == FactOrderHeader.staff_key)
        .join(DimTime, DimTime.time_key == FactOrderHeader.time_key)
        .where(
            FactOrderHeader.close_date_key.between(dkey(start), dkey(end)),
            DimStaff.staff_key != -1,
        )
        .group_by(DimStaff.staff_key, DimStaff.name, DimStaff.role)
    )
    if shift:
        hq = hq.where(DimTime.shift == shift)

    out = [
        {
            "name": h.name,
            "role": h.role,
            "orders": h.orders,
            "revenue_cents": h.revenue,
            "guests": h.guests,
            "tips_cents": h.tips,
            "avg_ticket_cents": (h.revenue // h.orders) if h.orders else 0,
        }
        for h in db.execute(hq).all()
    ]
    out.sort(key=lambda r: r["revenue_cents"], reverse=True)
    return out


# --------------------------------------------------------------------------
# 4.3.5  Delivery vs dine-in comparison
# --------------------------------------------------------------------------

def channel_comparison(db: Session, start: date, end: date) -> dict:
    """Side-by-side revenue/orders/avg ticket, plus platform breakdown."""
    type_rows = db.execute(
        select(
            DimChannel.channel_type,
            func.count(FactOrderHeader.order_sk),
            func.coalesce(func.sum(FactOrderHeader.total_cents), 0),
            func.coalesce(func.sum(FactOrderHeader.tip_cents), 0),
        )
        .join(DimChannel, DimChannel.channel_key == FactOrderHeader.channel_key)
        .where(FactOrderHeader.close_date_key.between(dkey(start), dkey(end)))
        .group_by(DimChannel.channel_type)
    ).all()

    platform_rows = db.execute(
        select(
            DimChannel.channel_name,
            DimChannel.is_third_party,
            func.count(FactOrderHeader.order_sk),
            func.coalesce(func.sum(FactOrderHeader.total_cents), 0),
        )
        .join(DimChannel, DimChannel.channel_key == FactOrderHeader.channel_key)
        .where(FactOrderHeader.close_date_key.between(dkey(start), dkey(end)))
        .group_by(DimChannel.channel_name, DimChannel.is_third_party)
        .order_by(func.sum(FactOrderHeader.total_cents).desc())
    ).all()

    def pack(rows):
        return [
            {
                "label": r[0],
                "orders": r[1],
                "revenue_cents": r[2],
                "avg_ticket_cents": (r[2] // r[1]) if r[1] else 0,
                **({"tips_cents": r[3]} if len(r) > 3 else {}),
            }
            for r in rows
        ]

    return {
        "by_type": pack(type_rows),
        "by_platform": [
            {
                "label": n,
                "is_third_party": bool(tp),
                "orders": o,
                "revenue_cents": r,
                "avg_ticket_cents": (r // o) if o else 0,
            }
            for n, tp, o, r in platform_rows
        ],
    }


# --------------------------------------------------------------------------
# Dashboard helper
# --------------------------------------------------------------------------

def date_bounds(db: Session) -> tuple[date, date]:
    """The span of loaded facts, so screens can default to a range with data.

    Spans settlement dates AND payment dates. Deriving the window from order
    dates alone truncates payments taken after midnight on the last day, which
    silently drops real revenue from the default view of every report.
    """
    h_lo, h_hi = db.execute(
        select(
            func.min(FactOrderHeader.close_date_key),
            func.max(FactOrderHeader.close_date_key),
        )
    ).one()
    p_lo, p_hi = db.execute(
        select(func.min(FactPayment.date_key), func.max(FactPayment.date_key))
    ).one()

    keys_lo = [k for k in (h_lo, p_lo) if k]
    keys_hi = [k for k in (h_hi, p_hi) if k]
    today = date.today()
    if not keys_lo or not keys_hi:
        return (today - timedelta(days=30), today)

    def undo(k: int) -> date:
        return date(k // 10000, (k // 100) % 100, k % 100)

    return undo(min(keys_lo)), undo(max(keys_hi))


def busiest_day(db: Session, start: date, end: date) -> dict | None:
    row = db.execute(
        select(
            DimDate.full_date,
            func.coalesce(func.sum(FactOrderHeader.total_cents), 0).label("rev"),
        )
        .join(DimDate, DimDate.date_key == FactOrderHeader.close_date_key)
        .where(FactOrderHeader.close_date_key.between(dkey(start), dkey(end)))
        .group_by(DimDate.full_date)
        .order_by(func.sum(FactOrderHeader.total_cents).desc())
        .limit(1)
    ).first()
    return {"date": row[0], "revenue_cents": row[1]} if row else None


# --------------------------------------------------------------------------
# Scheduling reports (read live from the OLTP, not the star schema)
# --------------------------------------------------------------------------

def _dt_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """[start 00:00, end+1 00:00) — an inclusive day range as datetimes."""
    lo = datetime(start.year, start.month, start.day)
    hi = datetime(end.year, end.month, end.day) + timedelta(days=1)
    return lo, hi


def swap_requests_by_staff(db: Session, start: date, end: date) -> list[dict]:
    """How many shift swaps each person asked for in the range, most first."""
    lo, hi = _dt_bounds(start, end)
    rows = db.execute(
        select(Staff.name, func.count(SwapRequest.id))
        .join(SwapRequest, SwapRequest.requested_by_id == Staff.id)
        .where(SwapRequest.created_at >= lo, SwapRequest.created_at < hi)
        .group_by(Staff.id)
        .order_by(func.count(SwapRequest.id).desc(), Staff.name)
    ).all()
    return [{"name": n, "count": c} for n, c in rows]


def missed_shifts_by_staff(db: Session, start: date, end: date) -> list[dict]:
    """How many shifts each person was scheduled for but never clocked into
    (the shift has already ended) — a no-show proxy — most first."""
    lo, hi = _dt_bounds(start, end)
    rows = db.execute(
        select(Staff.name, func.count(Shift.id))
        .join(Shift, Shift.staff_id == Staff.id)
        .where(
            Shift.starts_at >= lo, Shift.starts_at < hi,
            Shift.ends_at < datetime.now(),
            Shift.clock_in_at.is_(None),
        )
        .group_by(Staff.id)
        .order_by(func.count(Shift.id).desc(), Staff.name)
    ).all()
    return [{"name": n, "count": c} for n, c in rows]
