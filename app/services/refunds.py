"""Post-settlement refunds — section 4.2 (manager action).

A refund reverses money to the guest after the order is closed. Unlike a void
(services/payments.void_payment), it does not touch the payment's allocations,
the order status, or the table: the item was genuinely sold and paid. It is a
separate reversing entry that reduces net revenue and, for cash, the drawer.

The sum of refunds against a payment can never exceed what that payment
collected, and only settled orders can be refunded — reversing a live order is
what void is for.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.oltp import OrderStatus, Payment, Refund, Staff


class RefundError(Exception):
    pass


def refunded_so_far(db: Session, payment_id: int) -> int:
    return db.execute(
        select(func.coalesce(func.sum(Refund.amount_cents), 0))
        .where(Refund.payment_id == payment_id)
    ).scalar_one()


def refundable_cents(db: Session, payment: Payment) -> int:
    """How much of this payment can still be refunded."""
    return max(0, payment.total_cents - refunded_so_far(db, payment.id))


def refund_payment(
    db: Session, payment: Payment, amount_cents: int, staff: Staff, reason: str = ""
) -> Refund:
    """Record a refund against a settled payment. Commits."""
    if payment.voided:
        raise RefundError("This payment was voided; there is nothing to refund.")
    order = payment.order
    if order.status not in (OrderStatus.PAID, OrderStatus.CLOSED):
        raise RefundError(
            "Only a settled order can be refunded. Use void while it is still open."
        )
    if amount_cents <= 0:
        raise RefundError("Enter a refund amount greater than zero.")
    remaining = refundable_cents(db, payment)
    if amount_cents > remaining:
        raise RefundError(
            f"That is more than remains on this payment "
            f"(at most {remaining / 100:.2f} can still be refunded)."
        )

    refund = Refund(
        payment_id=payment.id,
        order_id=order.id,
        amount_cents=amount_cents,
        method=payment.instrument.name,
        is_cash=payment.instrument.instrument_type == "cash",
        reason=reason.strip()[:200],
        approved_by_id=staff.id,
    )
    db.add(refund)
    db.commit()
    db.refresh(refund)
    return refund
