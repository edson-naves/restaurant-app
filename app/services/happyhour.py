"""Happy hour — time-boxed automatic per-item discounts (Model A).

The manager defines windows (weekdays and/or a date range, a start/end time
inclusive on both ends, wrapping past midnight when end < start) that discount
whole categories and/or individual items by a percent. An item target overrides
its category; when two active windows cover the same item, the bigger discount
wins (guest-friendly, and normally there is only one).

Pricing is decided at the moment the waiter taps Add and **snapshotted onto the
order line** — the percent, the full base it was taken from, and a hold deadline
(window end + grace). It never changes once the line is fired to the kitchen.
A line added inside the window but left un-fired past its hold deadline reverts
to full price (``revert_expired``), and the order screen tells the waiter.

The percent applies to the item's **base price only** — paid modifiers are never
discounted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.oltp import HappyHour, KitchenStatus, MenuItem, Order, OrderItem


def deal_price_cents(base_cents: int, percent: int) -> int:
    """Base price after a happy-hour percent (base only; rounded to the cent)."""
    pct = max(0, min(100, percent))
    return round(max(0, base_cents) * (100 - pct) / 100)


def window_hold_until(hh: HappyHour, now: datetime) -> datetime:
    """When a line added at `now` under `hh` stops holding its discount: the end
    of the window occurrence that is open at `now`, plus the grace. Handles the
    overnight (end < start) case so the deadline lands on the right calendar day."""
    eh, em = (int(x) for x in hh.end_time.split(":"))
    end_dt = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if hh.start_time > hh.end_time:                      # overnight window
        hm = f"{now.hour:02d}:{now.minute:02d}"
        if hm >= hh.start_time:                          # evening part → ends tomorrow
            end_dt += timedelta(days=1)
    return end_dt + timedelta(minutes=max(0, hh.grace_minutes))


@dataclass
class ActiveDiscounts:
    """The winning discount per item / category across every happy hour open now."""
    item_pct: dict[int, int] = field(default_factory=dict)     # menu_item_id -> percent
    cat_pct: dict[int, int] = field(default_factory=dict)      # category_id -> percent
    hh_by_item: dict[int, HappyHour] = field(default_factory=dict)
    hh_by_cat: dict[int, HappyHour] = field(default_factory=dict)

    def any(self) -> bool:
        return bool(self.item_pct or self.cat_pct)

    def for_item(self, mi: MenuItem) -> tuple[int, HappyHour] | None:
        """(percent, happy_hour) for this item right now — an item target beats
        its category — or None if nothing covers it."""
        if mi.id in self.item_pct:
            return self.item_pct[mi.id], self.hh_by_item[mi.id]
        if mi.category_id in self.cat_pct:
            return self.cat_pct[mi.category_id], self.hh_by_cat[mi.category_id]
        return None


def load_active(db: Session, now: datetime | None = None) -> ActiveDiscounts:
    """Every discount in force at `now` (default: current time), best-per-target."""
    now = now or datetime.now()
    t = now.time()
    hm = f"{now.hour:02d}:{now.minute:02d}"
    ad = ActiveDiscounts()
    hhs = db.execute(select(HappyHour).where(HappyHour.is_active.is_(True))).scalars().all()
    for h in hhs:
        if not h.covers_time(t):
            continue
        # The weekday/date range applies to the day the occurrence *started*. For
        # the after-midnight tail of an overnight window (end < start, and we're at
        # or before end_time), that's the previous calendar day — so a Monday
        # 22:00–01:00 deal is live Tuesday 00:30 but not Monday 00:30 (which belongs
        # to Sunday's occurrence).
        occ_date = now.date()
        if h.start_time > h.end_time and hm <= h.end_time:
            occ_date -= timedelta(days=1)
        if not h.runs_on(occ_date):
            continue
        for it in h.items:
            if it.discount_percent > ad.item_pct.get(it.menu_item_id, -1):
                ad.item_pct[it.menu_item_id] = it.discount_percent
                ad.hh_by_item[it.menu_item_id] = h
        for c in h.categories:
            if c.discount_percent > ad.cat_pct.get(c.category_id, -1):
                ad.cat_pct[c.category_id] = c.discount_percent
                ad.hh_by_cat[c.category_id] = h
    return ad


@dataclass
class LineDiscount:
    """What to stamp on a new order line for an active happy-hour price."""
    unit_price_cents: int
    hh_id: int
    percent: int
    full_cents: int
    hold_until: datetime


def price_for(db: Session, mi: MenuItem, now: datetime | None = None,
              active: ActiveDiscounts | None = None) -> LineDiscount | None:
    """The happy-hour line pricing for adding `mi` at `now`, or None at full price.
    `active` may be passed to reuse a single load_active per request."""
    now = now or datetime.now()
    ad = active if active is not None else load_active(db, now)
    deal = ad.for_item(mi)
    if deal is None:
        return None
    percent, hh = deal
    return LineDiscount(
        unit_price_cents=deal_price_cents(mi.price_cents, percent),
        hh_id=hh.id, percent=percent, full_cents=mi.price_cents,
        hold_until=window_hold_until(hh, now),
    )


def revert_expired(db: Session, order: Order, now: datetime | None = None) -> list[OrderItem]:
    """Put back to full price any still-pending line whose happy-hour hold has
    passed (window ended + grace) without being fired. Fired lines are locked and
    never touched. Returns the reverted lines (for the 'happy hour ended' notice).
    The caller commits."""
    now = now or datetime.now()
    reverted: list[OrderItem] = []
    for it in order.items:
        if (it.hh_id and not it.hh_reverted and it.hh_hold_until
                and it.kitchen_status == KitchenStatus.PENDING
                and now > it.hh_hold_until):
            it.unit_price_cents = it.hh_full_cents if it.hh_full_cents is not None else it.unit_price_cents
            it.hh_reverted = True
            reverted.append(it)
    return reverted
