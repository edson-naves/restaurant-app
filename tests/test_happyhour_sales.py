"""Happy-hour integration checks through the real sales add/fire flow.

Uses an isolated SQLite database and a fixed clock. Run directly:
    python tests/test_happyhour_sales.py
"""
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import oltp  # noqa: F401
from app.models.oltp import (
    Channel, HappyHour, HappyHourItem, KitchenStatus, MenuCategory, MenuItem,
    Modifier, Order, OrderItem, Role, Seat, Staff,
)
from app.routers import sales
from app.services import happyhour

INSIDE = datetime(2026, 8, 19, 16, 0)
OUTSIDE = datetime(2026, 8, 19, 18, 1)
AFTER_HOLD = datetime(2026, 8, 19, 17, 11)


class FixedDatetime(datetime):
    current = INSIDE

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        return value if tz is None else value.astimezone(tz)


@contextmanager
def sales_case():
    handle, path = tempfile.mkstemp(prefix="hh_sales_", suffix=".db")
    os.close(handle)
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _record):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    try:
        category = MenuCategory(name="Beer")
        db.add(category)
        db.flush()
        item = MenuItem(category_id=category.id, name="Canadian", price_cents=650)
        modifier = Modifier(name="Large", price_delta_cents=150, category_id=category.id)
        channel = Channel(code="dine", name="Dine in", channel_type="dine_in")
        staff = Staff(name="Owner", role=Role.OWNER, pin_code="1000", is_active=True)
        order = Order(code="HH-TEST", channel=channel, status="open", guest_count=1)
        db.add_all([item, modifier, channel, staff, order])
        db.flush()
        seat = Seat(order_id=order.id, seat_number=1, label="Seat 1")
        offer = HappyHour(
            name="Beers", start_time="15:00", end_time="17:00",
            weekday_mask=127, grace_minutes=10,
        )
        db.add_all([seat, offer])
        db.flush()
        db.add(HappyHourItem(
            happy_hour_id=offer.id, menu_item_id=item.id, discount_percent=30,
        ))
        db.commit()
        yield db, order, item, modifier, staff, offer
    finally:
        db.close()
        engine.dispose()
        os.unlink(path)


def add_canadian(db, order, item, modifier, staff, now, with_modifier=True):
    FixedDatetime.current = now
    with patch.object(sales, "datetime", FixedDatetime):
        sales.add_item(
            order.id, menu_item_id=item.id, seat_number=1, quantity=1,
            notes="", course=0, category=0, hh=0,
            modifier_ids=[modifier.id] if with_modifier else [], option_ids=[],
            allergens=[], allergen_other="", db=db, staff=staff,
        )
    db.expire_all()
    return db.execute(select(OrderItem).order_by(OrderItem.id)).scalars().all()


def test_auto_applies_snapshot_on_add():
    with sales_case() as (db, order, item, modifier, staff, offer):
        line = add_canadian(db, order, item, modifier, staff, INSIDE, False)[0]
        assert line.unit_price_cents == 455
        assert line.hh_id == offer.id
        assert line.hh_percent == 30
        assert line.hh_full_cents == 650
        assert line.hh_hold_until == datetime(2026, 8, 19, 17, 10)


def test_paid_modifier_is_not_discounted():
    with sales_case() as (db, order, item, modifier, staff, _offer):
        line = add_canadian(db, order, item, modifier, staff, INSIDE)[0]
        assert line.unit_price_cents == 455
        assert line.modifier_total_cents == 150
        assert line.line_total_cents == 605


def test_discounted_and_full_price_lines_do_not_merge():
    with sales_case() as (db, order, item, modifier, staff, _offer):
        add_canadian(db, order, item, modifier, staff, INSIDE)
        lines = add_canadian(db, order, item, modifier, staff, OUTSIDE)
        assert len(lines) == 2
        assert [(line.unit_price_cents, line.quantity, line.hh_id is not None)
                for line in lines] == [(455, 1, True), (650, 1, False)]


def test_fired_line_keeps_happy_hour_price_after_hold():
    with sales_case() as (db, order, item, modifier, staff, _offer):
        line = add_canadian(db, order, item, modifier, staff, INSIDE, False)[0]
        FixedDatetime.current = INSIDE
        with patch.object(sales, "datetime", FixedDatetime):
            sales.send_to_kitchen(order.id, course=0, db=db, staff=staff)
        happyhour.revert_expired(db, db.get(Order, order.id), AFTER_HOLD)
        db.commit()
        db.refresh(line)
        assert line.kitchen_status == KitchenStatus.PREPARING
        assert line.unit_price_cents == 455
        assert not line.hh_reverted


def test_pending_line_reverts_after_hold():
    with sales_case() as (db, order, item, modifier, staff, _offer):
        line = add_canadian(db, order, item, modifier, staff, INSIDE, False)[0]
        reverted = happyhour.revert_expired(db, db.get(Order, order.id), AFTER_HOLD)
        db.commit()
        db.refresh(line)
        assert [changed.id for changed in reverted] == [line.id]
        assert line.kitchen_status == KitchenStatus.PENDING
        assert line.unit_price_cents == 650
        assert line.hh_reverted


if __name__ == "__main__":
    checks = [
        ("(a) auto-applies snapshot on Add", test_auto_applies_snapshot_on_add),
        ("(b) paid modifier remains full price", test_paid_modifier_is_not_discounted),
        ("(c) discounted/full-price lines do not merge", test_discounted_and_full_price_lines_do_not_merge),
        ("(d) fired line remains locked", test_fired_line_keeps_happy_hour_price_after_hold),
        ("(e) pending line reverts after hold", test_pending_line_reverts_after_hold),
    ]
    failed = []
    for label, check in checks:
        try:
            check()
            print(f"PASS  {label}")
        except Exception as error:  # keep running to report every contract check
            failed.append(label)
            print(f"FAIL  {label}: {error}")
    if failed:
        raise SystemExit(1)
    print("all happy-hour sales-flow tests passed")
