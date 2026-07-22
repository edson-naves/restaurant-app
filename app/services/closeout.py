"""End-of-day cash reconciliation — workflow 6.3 (the Z-report).

A close snapshots every non-voided payment taken since the previous close. The
window is contiguous: this close starts where the last one ended, so closes
tile the trading history with no gap and no overlap. The first-ever close, with
no predecessor, starts at the beginning of the current day so the demo history
doesn't roll six weeks of seed payments into one Z.

Voided payments are excluded — the same rule the reconciliation applies — so a
reversed tender never counts toward what the drawer should hold.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.oltp import DayClose, Payment, Refund, Staff
from app.services.money import money

CASH_TYPES = ("cash",)


@dataclass
class Pending:
    """The live figures for the window not yet closed."""
    window_start: datetime
    now: datetime
    payment_count: int = 0
    gross_sales_cents: int = 0
    discount_cents: int = 0
    tax_cents: int = 0
    tip_cents: int = 0
    total_collected_cents: int = 0
    expected_cash_cents: int = 0
    refund_cents: int = 0
    cash_refund_cents: int = 0
    by_instrument: dict[str, int] = field(default_factory=dict)

    @property
    def net_collected_cents(self) -> int:
        return self.total_collected_cents - self.refund_cents


def _window_start(db: Session, now: datetime) -> datetime:
    last = db.execute(
        select(DayClose).order_by(DayClose.closed_at.desc()).limit(1)
    ).scalars().first()
    if last is not None:
        return last.closed_at
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def compute_pending(db: Session, now: datetime | None = None) -> Pending:
    """Sum every non-voided payment since the last close."""
    now = now or datetime.now()
    start = _window_start(db, now)
    p = Pending(window_start=start, now=now)

    payments = db.execute(
        select(Payment).where(
            Payment.voided.is_(False),
            Payment.created_at >= start,
            Payment.created_at <= now,
        )
    ).scalars().all()

    for pay in payments:
        p.payment_count += 1
        p.gross_sales_cents += pay.items_cents
        p.discount_cents += pay.discount_cents
        p.tax_cents += pay.tax_cents
        p.tip_cents += pay.tip_cents
        p.total_collected_cents += pay.total_cents
        name = pay.instrument.name
        p.by_instrument[name] = p.by_instrument.get(name, 0) + pay.total_cents
        if pay.instrument.instrument_type in CASH_TYPES:
            p.expected_cash_cents += pay.total_cents

    # Refunds paid out in the window reduce net revenue; cash refunds reduce the
    # drawer, so they come off expected cash.
    refunds = db.execute(
        select(Refund).where(Refund.created_at >= start, Refund.created_at <= now)
    ).scalars().all()
    for r in refunds:
        p.refund_cents += r.amount_cents
        if r.is_cash:
            p.cash_refund_cents += r.amount_cents
    p.expected_cash_cents -= p.cash_refund_cents

    return p


def record_close(
    db: Session,
    staff: Staff,
    opening_float_cents: int,
    counted_cash_cents: int,
    notes: str = "",
) -> DayClose:
    """Freeze the pending window into a DayClose and return it. Commits."""
    now = datetime.now()
    p = compute_pending(db, now)
    variance = counted_cash_cents - opening_float_cents - p.expected_cash_cents

    close = DayClose(
        closed_by_id=staff.id,
        window_start=p.window_start,
        closed_at=now,
        opening_float_cents=opening_float_cents,
        expected_cash_cents=p.expected_cash_cents,
        counted_cash_cents=counted_cash_cents,
        variance_cents=variance,
        gross_sales_cents=p.gross_sales_cents,
        discount_cents=p.discount_cents,
        tax_cents=p.tax_cents,
        tip_cents=p.tip_cents,
        total_collected_cents=p.total_collected_cents,
        refund_cents=p.refund_cents,
        cash_refund_cents=p.cash_refund_cents,
        payment_count=p.payment_count,
        by_instrument_json=json.dumps(p.by_instrument),
        notes=notes.strip()[:300],
    )
    db.add(close)
    db.commit()
    db.refresh(close)
    return close


def by_instrument(close: DayClose) -> list[tuple[str, str]]:
    """(name, formatted amount) rows for display, largest first."""
    data = json.loads(close.by_instrument_json or "{}")
    return [(k, money(v)) for k, v in sorted(data.items(), key=lambda kv: -kv[1])]
