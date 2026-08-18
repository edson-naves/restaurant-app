"""Happy-hour engine (feat/happy-hour) — time-boxed automatic per-item discounts.

Covers the window logic (weekday, date range, inclusive both ends, overnight),
item-over-category precedence, bigger-discount-wins, add-time snapshot pricing,
and the grace-period revert (Model A). Isolated throwaway-SQLite; no dev DB.
Run: python tests/test_happyhour.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import oltp  # noqa: F401
from app.models.oltp import (
    Channel, HappyHour, HappyHourCategory, HappyHourItem, KitchenStatus,
    MenuCategory, MenuItem, Order, OrderItem,
)
from app.services import happyhour as hh

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _db():
    path = os.path.join(tempfile.gettempdir(), f"hh_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _item(db, name="Beer", cents=1000, cat=None):
    if cat is None:
        cat = MenuCategory(name=f"Beer{uuid.uuid4().hex[:4]}")
        db.add(cat)
        db.flush()
    mi = MenuItem(category_id=cat.id, name=name, price_cents=cents)
    db.add(mi)
    db.flush()
    return mi, cat


def _hh(db, *, start="15:00", end="17:00", mask=127, date_from=None, date_to=None,
        grace=10, item=None, item_pct=None, cat=None, cat_pct=None):
    h = HappyHour(name="HH", start_time=start, end_time=end, weekday_mask=mask,
                  date_from=date_from, date_to=date_to, grace_minutes=grace)
    db.add(h)
    db.flush()
    if item is not None:
        db.add(HappyHourItem(happy_hour_id=h.id, menu_item_id=item.id, discount_percent=item_pct))
    if cat is not None:
        db.add(HappyHourCategory(happy_hour_id=h.id, category_id=cat.id, discount_percent=cat_pct))
    db.flush()
    return h


# --- window logic (model methods, no DB needed) ---------------------------

def test_window_inclusive_both_ends():
    h = HappyHour(name="x", start_time="16:00", end_time="18:00", weekday_mask=127)
    check(h.covers_time(datetime(2026, 8, 19, 16, 0).time()), "start (16:00) is inside — inclusive")
    check(h.covers_time(datetime(2026, 8, 19, 18, 0).time()), "end (18:00) is inside — inclusive")
    check(not h.covers_time(datetime(2026, 8, 19, 15, 59).time()), "15:59 is outside")
    check(not h.covers_time(datetime(2026, 8, 19, 18, 1).time()), "18:01 is outside")


def test_overnight_window():
    h = HappyHour(name="x", start_time="22:00", end_time="01:00", weekday_mask=127)
    check(h.covers_time(datetime(2026, 8, 19, 23, 0).time()), "23:00 is inside a 22:00–01:00 window")
    check(h.covers_time(datetime(2026, 8, 19, 0, 30).time()), "00:30 is inside (past midnight)")
    check(h.covers_time(datetime(2026, 8, 19, 1, 0).time()), "01:00 (end) is inside — inclusive")
    check(not h.covers_time(datetime(2026, 8, 19, 2, 0).time()), "02:00 is outside")
    check(not h.covers_time(datetime(2026, 8, 19, 21, 0).time()), "21:00 is outside")


def test_weekday_and_date_range():
    wed = datetime(2026, 8, 19).date()   # Wednesday
    h = HappyHour(name="x", start_time="00:00", end_time="23:59", weekday_mask=(1 << 2))  # Wed only
    check(h.runs_on(wed), "runs on its weekday (Wed)")
    check(not h.runs_on(datetime(2026, 8, 20).date()), "does not run on Thu")
    h2 = HappyHour(name="x", start_time="00:00", end_time="23:59", weekday_mask=127,
                   date_from=datetime(2026, 8, 1).date(), date_to=datetime(2026, 8, 10).date())
    check(not h2.runs_on(wed), "outside the date range does not run")
    check(h2.runs_on(datetime(2026, 8, 5).date()), "inside the date range runs")


# --- engine (DB) ----------------------------------------------------------

def test_item_discount_applies_in_window():
    db = _db()
    beer, _ = _item(db, "Beer", 1000)
    _hh(db, start="15:00", end="17:00", item=beer, item_pct=20)
    db.commit()
    now = datetime(2026, 8, 19, 16, 0)   # Wed, in window
    ld = hh.price_for(db, beer, now)
    check(ld is not None and ld.unit_price_cents == 800, "20% off 1000 = 800 in the window")
    check(ld is not None and ld.percent == 20 and ld.full_cents == 1000, "snapshot carries % and full base")
    db.close()


def test_no_discount_outside_window():
    db = _db()
    beer, _ = _item(db, "Beer", 1000)
    _hh(db, start="15:00", end="17:00", item=beer, item_pct=20)
    db.commit()
    check(hh.price_for(db, beer, datetime(2026, 8, 19, 14, 59)) is None, "14:59 -> full price (None)")
    check(hh.price_for(db, beer, datetime(2026, 8, 19, 17, 1)) is None, "17:01 -> full price (None)")
    db.close()


def test_item_overrides_category():
    db = _db()
    beer, cat = _item(db, "Beer", 1000)
    # category 10% off, but this item has its own 30% — item wins.
    _hh(db, start="15:00", end="17:00", cat=cat, cat_pct=10, item=beer, item_pct=30)
    db.commit()
    ld = hh.price_for(db, beer, datetime(2026, 8, 19, 16, 0))
    check(ld is not None and ld.percent == 30, "item target (30%) overrides its category (10%)")
    db.close()


def test_category_covers_its_items():
    db = _db()
    beer, cat = _item(db, "Beer", 1000)
    wine, _ = _item(db, "Wine", 2000, cat=cat)   # same category, no item target
    _hh(db, start="15:00", end="17:00", cat=cat, cat_pct=15)
    db.commit()
    now = datetime(2026, 8, 19, 16, 0)
    check(hh.price_for(db, wine, now).unit_price_cents == 1700, "category 15% off reaches Wine (2000->1700)")
    check(hh.price_for(db, beer, now).unit_price_cents == 850, "and Beer (1000->850)")
    db.close()


def test_bigger_discount_wins():
    db = _db()
    beer, _ = _item(db, "Beer", 1000)
    _hh(db, start="15:00", end="17:00", item=beer, item_pct=10)
    _hh(db, start="15:00", end="17:00", item=beer, item_pct=25)
    db.commit()
    ld = hh.price_for(db, beer, datetime(2026, 8, 19, 16, 0))
    check(ld.percent == 25, "two overlapping happy hours -> bigger discount (25%) wins")
    db.close()


def test_hold_until_is_end_plus_grace():
    db = _db()
    beer, _ = _item(db, "Beer", 1000)
    _hh(db, start="15:00", end="17:00", grace=10, item=beer, item_pct=20)
    db.commit()
    ld = hh.price_for(db, beer, datetime(2026, 8, 19, 16, 0))
    check(ld.hold_until == datetime(2026, 8, 19, 17, 10), "hold = window end (17:00) + 10 min grace")
    db.close()


def _order(db):
    ch = Channel(code=f"c{uuid.uuid4().hex[:5]}", name="Dine", channel_type="dine_in")
    db.add(ch)
    db.flush()
    o = Order(code=f"O{uuid.uuid4().hex[:8]}", channel_id=ch.id, status="open")
    db.add(o)
    db.flush()
    return o


def test_revert_expired_pending_only():
    db = _db()
    beer, _ = _item(db, "Beer", 1000)
    h = _hh(db, item=beer, item_pct=20)
    o = _order(db)
    hold = datetime(2026, 8, 19, 17, 10)
    # a pending discounted line, and a fired discounted line, both past their hold
    pend = OrderItem(order_id=o.id, menu_item_id=beer.id, quantity=1, unit_price_cents=800,
                     hh_id=h.id, hh_percent=20, hh_full_cents=1000, hh_hold_until=hold,
                     kitchen_status=KitchenStatus.PENDING)
    fired = OrderItem(order_id=o.id, menu_item_id=beer.id, quantity=1, unit_price_cents=800,
                      hh_id=h.id, hh_percent=20, hh_full_cents=1000, hh_hold_until=hold,
                      kitchen_status=KitchenStatus.PREPARING)
    db.add_all([pend, fired])
    db.commit()
    reverted = hh.revert_expired(db, o, now=datetime(2026, 8, 19, 17, 20))  # 10 min past hold
    db.commit()
    check(len(reverted) == 1 and pend.unit_price_cents == 1000 and pend.hh_reverted,
          "pending line past grace reverts to full price (800->1000)")
    check(fired.unit_price_cents == 800 and not fired.hh_reverted,
          "a fired line is locked — never reverts")
    db.close()


def test_no_revert_within_hold():
    db = _db()
    beer, _ = _item(db, "Beer", 1000)
    h = _hh(db, item=beer, item_pct=20)
    o = _order(db)
    line = OrderItem(order_id=o.id, menu_item_id=beer.id, quantity=1, unit_price_cents=800,
                     hh_id=h.id, hh_percent=20, hh_full_cents=1000,
                     hh_hold_until=datetime(2026, 8, 19, 17, 10),
                     kitchen_status=KitchenStatus.PENDING)
    db.add(line)
    db.commit()
    reverted = hh.revert_expired(db, o, now=datetime(2026, 8, 19, 17, 5))  # still within grace
    check(reverted == [] and line.unit_price_cents == 800, "within grace: keeps the happy-hour price")
    db.close()


if __name__ == "__main__":
    for fn in (test_window_inclusive_both_ends, test_overnight_window,
               test_weekday_and_date_range, test_item_discount_applies_in_window,
               test_no_discount_outside_window, test_item_overrides_category,
               test_category_covers_its_items, test_bigger_discount_wins,
               test_hold_until_is_end_plus_grace, test_revert_expired_pending_only,
               test_no_revert_within_hold):
        print(f"- {fn.__name__}")
        fn()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall happy-hour tests passed")
