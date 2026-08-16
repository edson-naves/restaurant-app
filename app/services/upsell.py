"""Upsell suggestions — a data-driven "smart add-on" for the order screen.

Given what's already on an order, surface 1-2 menu items to offer the guest,
learned from THIS restaurant's own order history (items frequently ordered
together), with a category-gap fallback for cold-start (a new venue, or a still
mostly-empty order). No LLM: deterministic, fast, offline, and grounded in real
sales — the differentiator is that it learns from what actually sells here.

Suggestions are a nudge only: never auto-added, and always filtered to items
that are on the menu, in stock, and addable in one tap (no required configurator
choice), so tapping the suggestion just adds it. Allergen-aware filtering is a
later enhancement — the menu has no per-item allergen tags yet.
"""
from __future__ import annotations

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.models.oltp import MenuItem, Order, OrderItem


def _orderable(mi: MenuItem) -> bool:
    """Addable in one tap: on the menu, in stock, and needs no required choice."""
    return bool(mi.is_active and mi.available and not mi.requires_choice)


def _cooccurrence_ids(db: Session, order: Order, current_ids: set[int], fetch: int) -> list[int]:
    """Menu-item ids most often ordered ALONGSIDE the current items, across past
    orders (this order excluded), best first. Empty for an empty order or no
    history."""
    if not current_ids:
        return []
    past_orders = (
        select(OrderItem.order_id)
        .where(OrderItem.menu_item_id.in_(current_ids), OrderItem.order_id != order.id)
        .scalar_subquery()
    )
    together = func.count(distinct(OrderItem.order_id))
    rows = db.execute(
        select(OrderItem.menu_item_id)
        .where(OrderItem.order_id.in_(past_orders), OrderItem.menu_item_id.not_in(current_ids))
        .group_by(OrderItem.menu_item_id)
        .order_by(together.desc())
        .limit(fetch)
    ).all()
    return [r[0] for r in rows]


def _popular_ids(db: Session, fetch: int) -> list[int]:
    """Most-ordered items overall (cold-start / backfill), best first."""
    n = func.count()
    rows = db.execute(
        select(OrderItem.menu_item_id)
        .group_by(OrderItem.menu_item_id)
        .order_by(n.desc())
        .limit(fetch)
    ).all()
    return [r[0] for r in rows]


def suggest_upsells(db: Session, order: Order, limit: int = 2) -> list[MenuItem]:
    """Up to ``limit`` menu items to offer as add-ons for this order.

    Ranking: co-occurrence (what pairs with the current items here) first, then
    popular items as backfill; suggestions that fill a *category the order doesn't
    have yet* (a drink, a dessert, a side) are preferred, to nudge a complete meal.
    """
    if order.status in ("paid", "closed", "cancelled"):
        return []
    current_ids = {i.menu_item_id for i in order.items}
    have_cats = {i.menu_item.category_id for i in order.items if i.menu_item}

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
    ranked = _orderable_items(_cooccurrence_ids(db, order, current_ids, limit * 5), current_ids)
    if len(ranked) < limit:
        have = current_ids | {mi.id for mi in ranked}
        ranked += _orderable_items(_popular_ids(db, limit * 10), have)
    if not ranked:
        return []

    # Prefer a suggestion that fills a course/category the table doesn't have yet.
    gap = [mi for mi in ranked if mi.category_id not in have_cats]
    rest = [mi for mi in ranked if mi.category_id in have_cats]
    return (gap + rest)[:limit]
