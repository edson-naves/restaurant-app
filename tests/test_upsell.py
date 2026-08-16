"""Upsell suggestion engine (feat/upsell) — data-driven add-ons.

Isolated throwaway-SQLite scenarios (no dependency on the dev DB). Covers the
co-occurrence signal, the cold-start/popular fallback, and the guardrails
(exclude on-order / 86'd / requires-a-choice / paid orders).
Run: python tests/test_upsell.py
"""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import oltp  # noqa: F401 — register tables
from app.models.oltp import Channel, MenuCategory, MenuItem, ModifierGroup, Order, OrderItem
from app.services import upsell

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _db():
    path = os.path.join(tempfile.gettempdir(), f"upsell_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _menu(db):
    ch = Channel(code=f"c{uuid.uuid4().hex[:5]}", name="Dine", channel_type="dine_in")
    mains = MenuCategory(name=f"Mains{uuid.uuid4().hex[:4]}")
    drinks = MenuCategory(name=f"Drinks{uuid.uuid4().hex[:4]}")
    desserts = MenuCategory(name=f"Desserts{uuid.uuid4().hex[:4]}")
    db.add_all([ch, mains, drinks, desserts])
    db.flush()
    items = {}
    for cat, name in ((mains, "Burger"), (mains, "Steak"), (drinks, "Cola"),
                      (drinks, "Beer"), (desserts, "Cake")):
        m = MenuItem(category_id=cat.id, name=name, price_cents=1000)
        db.add(m)
        db.flush()
        items[name] = m
    return ch, items


def _order(db, ch, item_ids, status="open"):
    o = Order(code=f"O{uuid.uuid4().hex[:8]}", channel_id=ch.id, status=status)
    db.add(o)
    db.flush()
    for iid in item_ids:
        db.add(OrderItem(order_id=o.id, menu_item_id=iid, quantity=1, unit_price_cents=1000))
    db.flush()
    return o


def test_cooccurrence_learned_from_history():
    db = _db()
    ch, it = _menu(db)
    for _ in range(5):
        _order(db, ch, [it["Burger"].id, it["Cola"].id], status="closed")
    for _ in range(4):
        _order(db, ch, [it["Steak"].id, it["Beer"].id], status="closed")
    db.commit()
    cur = _order(db, ch, [it["Burger"].id])
    db.commit()
    names = [m.name for m in upsell.suggest_upsells(db, cur, limit=2)]
    check("Cola" in names, "item co-ordered with Burger (Cola) is suggested (#cooccurrence)")
    check("Burger" not in names, "item already on the order is not suggested")
    db.close()


def test_excludes_86_but_offers_configurable():
    db = _db()
    ch, it = _menu(db)
    for _ in range(5):
        _order(db, ch, [it["Burger"].id, it["Cola"].id, it["Cake"].id], status="closed")
    it["Cola"].available = False                       # 86'd
    db.add(ModifierGroup(menu_item_id=it["Cake"].id, name="Size", required=True))  # needs a choice
    db.commit()
    cur = _order(db, ch, [it["Burger"].id])
    db.commit()
    names = [m.name for m in upsell.suggest_upsells(db, cur, limit=4)]
    check("Cola" not in names, "an 86'd item is never suggested")
    check("Cake" in names, "a configurator item is still suggested (chip opens the configurator, so a "
                           "Pizza-like section is never blank)")
    db.close()


def test_cold_start_falls_back_to_popular():
    db = _db()
    ch, it = _menu(db)
    for _ in range(3):
        _order(db, ch, [it["Steak"].id, it["Cake"].id], status="closed")
    db.commit()
    cur = _order(db, ch, [])                            # empty order -> no co-occurrence
    db.commit()
    res = upsell.suggest_upsells(db, cur, limit=2)
    check(len(res) >= 1, "cold-start / empty order still returns popular suggestions")
    db.close()


def test_category_scoped_suggestions():
    db = _db()
    ch, it = _menu(db)
    # Burger is co-ordered with a drink (Cola) AND a dessert (Cake).
    for _ in range(5):
        _order(db, ch, [it["Burger"].id, it["Cola"].id, it["Cake"].id], status="closed")
    db.commit()
    cur = _order(db, ch, [it["Burger"].id])
    db.commit()
    desserts = it["Cake"].category_id
    drinks = it["Cola"].category_id
    d_names = [m.name for m in upsell.suggest_upsells(db, cur, limit=2, category_id=desserts)]
    check(d_names == ["Cake"], "in the Desserts section only a dessert is suggested (Cake, not Cola)")
    k_names = [m.name for m in upsell.suggest_upsells(db, cur, limit=2, category_id=drinks)]
    check("Cola" in k_names and "Cake" not in k_names, "in the Drinks section only a drink is suggested")
    db.close()


def test_no_suggestions_on_settled_order():
    db = _db()
    ch, it = _menu(db)
    cur = _order(db, ch, [it["Burger"].id], status="paid")
    db.commit()
    check(upsell.suggest_upsells(db, cur) == [], "no suggestions on a paid/closed order")
    db.close()


if __name__ == "__main__":
    for fn in (test_cooccurrence_learned_from_history,
               test_excludes_86_but_offers_configurable,
               test_cold_start_falls_back_to_popular,
               test_category_scoped_suggestions,
               test_no_suggestions_on_settled_order):
        print(f"- {fn.__name__}")
        fn()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall upsell tests passed")
