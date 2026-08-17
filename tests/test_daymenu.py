"""Happy-hour nudge detection (feat/happy-hour-nudge).

Covers active_percent_deals: which menu items a percent (happy-hour) day menu
covers *right now*, respecting the time window, and never surfacing a fixed-price
combo. Isolated throwaway-SQLite (no dev DB).
Run: python tests/test_daymenu.py
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
from app.models import oltp  # noqa: F401 — register tables
from app.models.oltp import DayMenu, DayMenuChoice, MenuCategory, MenuItem
from app.services import daymenu

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _db():
    path = os.path.join(tempfile.gettempdir(), f"daymenu_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _item(db, name, cents):
    cat = MenuCategory(name=f"Drinks{uuid.uuid4().hex[:4]}")
    db.add(cat)
    db.flush()
    mi = MenuItem(category_id=cat.id, name=name, price_cents=cents)
    db.add(mi)
    db.flush()
    return mi


def _deal(db, mi, *, discount_type="percent", pct=None, price=0,
          weekday=None, start=None, end=None):
    dm = DayMenu(name="Happy Hour", price_cents=price, discount_type=discount_type,
                 discount_percent=pct, weekday=weekday, start_time=start, end_time=end)
    db.add(dm)
    db.flush()
    db.add(DayMenuChoice(day_menu_id=dm.id, course=1, menu_item_id=mi.id))
    db.flush()
    return dm


def test_percent_deal_in_window_is_detected():
    db = _db()
    beer = _item(db, "Beer", 900)
    wed = datetime(2026, 8, 19, 16, 0)   # a Wednesday, 4pm
    _deal(db, beer, pct=10, weekday=wed.weekday(), start="15:00", end="17:00")
    db.commit()
    deals = daymenu.active_percent_deals(db, wed.date(), wed)
    check(beer.id in deals, "item in an active happy-hour window is detected")
    check((deals.get(beer.id).discount_percent if beer.id in deals else None) == 10,
          "the deal's percent is carried")
    check(daymenu.effective_price_cents(deals[beer.id], [beer]) == 810,
          "single-item deal price = 10% off (900 -> 810)")
    db.close()


def test_outside_window_not_detected():
    db = _db()
    beer = _item(db, "Beer", 900)
    wed = datetime(2026, 8, 19, 18, 0)   # 6pm — after the 3-5pm window
    _deal(db, beer, pct=10, weekday=wed.weekday(), start="15:00", end="17:00")
    db.commit()
    deals = daymenu.active_percent_deals(db, wed.date(), wed)
    check(beer.id not in deals, "item is NOT flagged once the window has closed")
    db.close()


def test_fixed_price_combo_never_surfaced():
    db = _db()
    steak = _item(db, "Steak", 3000)
    wed = datetime(2026, 8, 19, 19, 0)
    _deal(db, steak, discount_type="fixed", price=2500, weekday=wed.weekday())  # all-day fixed
    db.commit()
    deals = daymenu.active_percent_deals(db, wed.date(), wed)
    check(steak.id not in deals, "a fixed-price combo is never offered as a per-item nudge")
    db.close()


def test_bigger_discount_wins():
    db = _db()
    beer = _item(db, "Beer", 1000)
    wed = datetime(2026, 8, 19, 16, 0)
    _deal(db, beer, pct=10, weekday=wed.weekday(), start="15:00", end="17:00")
    _deal(db, beer, pct=25, weekday=wed.weekday(), start="15:00", end="17:00")
    db.commit()
    deals = daymenu.active_percent_deals(db, wed.date(), wed)
    check(deals[beer.id].discount_percent == 25,
          "when two deals overlap, the bigger discount wins")
    db.close()


if __name__ == "__main__":
    for fn in (test_percent_deal_in_window_is_detected,
               test_outside_window_not_detected,
               test_fixed_price_combo_never_surfaced,
               test_bigger_discount_wins):
        print(f"- {fn.__name__}")
        fn()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall day-menu tests passed")
