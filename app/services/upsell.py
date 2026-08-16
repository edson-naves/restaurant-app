"""Upsell suggestions — a data-driven "smart add-on" for the order screen.

Given what's already on an order, surface 1-2 menu items to offer the guest,
learned from THIS restaurant's own order history (items frequently ordered
together), with a popular/category-gap fallback for cold-start. No LLM:
deterministic, fast, offline, and grounded in real sales — the differentiator is
that it learns from what actually sells here.

The suggestion is **category-aware**: scoped to the category the waiter is
currently browsing, it shows "the top add-on in THIS section for this table"
(e.g. in Desserts, the dessert most often ordered alongside the current items).
Passing ``category_id=None`` gives an order-level suggestion across the menu.

Suggestions are a nudge only: never auto-added, and always filtered to items on
the menu and in stock. Items that need a choice (a pizza size, a scoop flavour)
are suggested too — the order screen opens the configurator for them instead of
a one-tap add, so configurator-only categories aren't left blank.
"""
from __future__ import annotations

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.models.oltp import MenuItem, Order, OrderItem


def _orderable(mi: MenuItem) -> bool:
    """Suggestable: on the menu and in stock. Items that need a choice (size,
    etc.) are fine — the order screen routes them to the configurator rather
    than adding in one tap, so whole configurator-only categories (Pizza, Ice
    Cream) still get suggestions instead of showing nothing."""
    return bool(mi.is_active and mi.available)


def _in_category(category_id: int | None):
    """A subquery of menu-item ids in ``category_id``, or None to not filter."""
    if category_id is None:
        return None
    return select(MenuItem.id).where(MenuItem.category_id == category_id).scalar_subquery()


def _cooccurrence_ids(db: Session, order: Order, current_ids: set[int], fetch: int,
                      category_id: int | None = None) -> list[int]:
    """Menu-item ids most often ordered ALONGSIDE the current items, across past
    orders (this order excluded), best first — optionally restricted to one
    category. Empty for an empty order or no history."""
    if not current_ids:
        return []
    past_orders = (
        select(OrderItem.order_id)
        .where(OrderItem.menu_item_id.in_(current_ids), OrderItem.order_id != order.id)
        .scalar_subquery()
    )
    together = func.count(distinct(OrderItem.order_id))
    q = (
        select(OrderItem.menu_item_id)
        .where(OrderItem.order_id.in_(past_orders), OrderItem.menu_item_id.not_in(current_ids))
    )
    cat = _in_category(category_id)
    if cat is not None:
        q = q.where(OrderItem.menu_item_id.in_(cat))
    q = q.group_by(OrderItem.menu_item_id).order_by(together.desc()).limit(fetch)
    return [r[0] for r in db.execute(q).all()]


def _popular_ids(db: Session, fetch: int, category_id: int | None = None) -> list[int]:
    """Most-ordered items overall (cold-start / backfill), optionally within one
    category, best first."""
    n = func.count()
    q = select(OrderItem.menu_item_id)
    cat = _in_category(category_id)
    if cat is not None:
        q = q.where(OrderItem.menu_item_id.in_(cat))
    q = q.group_by(OrderItem.menu_item_id).order_by(n.desc()).limit(fetch)
    return [r[0] for r in db.execute(q).all()]


def suggest_upsells(db: Session, order: Order, limit: int = 2,
                    category_id: int | None = None) -> list[MenuItem]:
    """Up to ``limit`` menu items to offer as add-ons for this order.

    With ``category_id`` set, suggestions are scoped to that category (the section
    the waiter is browsing). Without it, they span the menu and a suggestion that
    fills a course/category the order lacks is preferred. Ranking: co-occurrence
    (what pairs with the current items here) first, then popular as backfill.
    """
    if order.status in ("paid", "closed", "cancelled"):
        return []
    current_ids = {i.menu_item_id for i in order.items}

    def _orderable_items(ids: list[int], exclude: set[int]) -> list[MenuItem]:
        ids = [i for i in ids if i not in exclude]
        if not ids:
            return []
        by_id = {mi.id: mi for mi in db.execute(
            select(MenuItem).where(MenuItem.id.in_(ids))
        ).scalars()}
        return [by_id[i] for i in ids if i in by_id and _orderable(by_id[i])]

    # Co-occurrence first — the smart signal. Only fall back to the whole-table
    # "popular" query when it doesn't fill up (keeps the hot order screen cheap).
    ranked = _orderable_items(
        _cooccurrence_ids(db, order, current_ids, limit * 5, category_id), current_ids)
    if len(ranked) < limit:
        have = current_ids | {mi.id for mi in ranked}
        ranked += _orderable_items(_popular_ids(db, limit * 10, category_id), have)
    if not ranked:
        return []

    if category_id is not None:
        return ranked[:limit]        # already scoped to the browsed category

    # Order-level: prefer a suggestion that fills a course the table doesn't have yet.
    have_cats = {i.menu_item.category_id for i in order.items if i.menu_item}
    gap = [mi for mi in ranked if mi.category_id not in have_cats]
    rest = [mi for mi in ranked if mi.category_id in have_cats]
    return (gap + rest)[:limit]
