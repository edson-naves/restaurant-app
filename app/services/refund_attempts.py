"""Durable refund-attempt lifecycle (finding #6).

A Payment can accumulate many refunds — partial, repeated, and retried — so each
is its own ``RefundAttempt`` with an independent idempotency key, provider refund
id, amount, and status. Mirrors payment_attempts' concurrency guarantees: atomic
idempotent create and compare-and-swap transitions.

The refundable-balance invariant (the sum of a payment's refunds never exceeds
what it captured) is enforced transactionally where refunds are initiated in
Stage 2c; this module provides the durable records and the running total.
"""
from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.oltp import (
    REFUND_ATTEMPT_TRANSITIONS,
    RefundAttempt,
    RefundAttemptStatus,
)
from app.services.payment_attempts import (
    PaymentAttemptError,
    TransitionConflict,
    _validate_provider,
)


def new_idempotency_key() -> str:
    return secrets.token_hex(24)


# Refund states that still count against the refundable balance (money that has
# gone back or is on its way). REJECTED/FAILED free the amount up again.
COUNTS_AGAINST_BALANCE = (
    RefundAttemptStatus.CREATED,
    RefundAttemptStatus.PROCESSOR_PENDING,
    RefundAttemptStatus.COMPLETED,
    RefundAttemptStatus.REQUIRES_RECONCILIATION,
)


def refunded_and_pending_cents(db: Session, payment_id: int) -> int:
    """Sum of a payment's refunds that are completed or still in flight, used to
    protect the refundable balance before creating another refund."""
    return db.execute(
        select(func.coalesce(func.sum(RefundAttempt.amount_cents), 0)).where(
            RefundAttempt.payment_id == payment_id,
            RefundAttempt.status.in_(COUNTS_AGAINST_BALANCE),
        )
    ).scalar_one()


def create_refund_attempt(
    db: Session,
    *,
    payment_id: int,
    staff_id: int,
    provider: str,
    amount_cents: int,
    currency: str = "CAD",
    charge_attempt_id: int | None = None,
    idempotency_key: str | None = None,
) -> RefundAttempt:
    """Persist a CREATED refund attempt. Commits. Idempotent and concurrency-safe
    on the idempotency key (a repeat resolves to the same row)."""
    _validate_provider(provider)
    if amount_cents <= 0:
        raise PaymentAttemptError("refund amount must be positive.")

    if idempotency_key:
        existing = _by_key(db, idempotency_key)
        if existing is not None:
            return existing
    else:
        idempotency_key = new_idempotency_key()

    refund = RefundAttempt(
        payment_id=payment_id, charge_attempt_id=charge_attempt_id,
        staff_id=staff_id, provider=provider, amount_cents=amount_cents,
        currency=currency, idempotency_key=idempotency_key,
        status=RefundAttemptStatus.CREATED,
    )
    db.add(refund)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _by_key(db, idempotency_key)
        if existing is None:
            raise
        return existing
    db.refresh(refund)
    return refund


def _by_key(db: Session, key: str) -> RefundAttempt | None:
    return db.execute(
        select(RefundAttempt).where(RefundAttempt.idempotency_key == key)
    ).scalar_one_or_none()


def transition_refund(
    db: Session,
    refund: RefundAttempt,
    new_status: str,
    *,
    provider_refund_id: str | None = None,
    refund_id: int | None = None,
    last_error: str | None = None,
    commit: bool = True,
) -> RefundAttempt:
    """Concurrency-safe compare-and-swap transition for a refund attempt."""
    allowed = REFUND_ATTEMPT_TRANSITIONS.get(refund.status, set())
    if new_status not in allowed:
        raise PaymentAttemptError(
            f"illegal refund transition {refund.status} -> {new_status} "
            f"(allowed: {sorted(allowed) or 'none — terminal state'})"
        )
    expected = refund.status
    values: dict = {"status": new_status, "updated_at": datetime.now()}
    if last_error is not None:
        values["last_error"] = last_error

    conds = [RefundAttempt.id == refund.id, RefundAttempt.status == expected]
    for field, val in (("provider_refund_id", provider_refund_id), ("refund_id", refund_id)):
        if val is not None:
            values[field] = val
            col = getattr(RefundAttempt, field)
            conds.append(or_(col.is_(None), col == val))  # write-once

    try:
        applied = db.execute(
            update(RefundAttempt).where(and_(*conds)).values(**values)
        ).rowcount
        if applied == 1 and commit:
            db.commit()
    except (IntegrityError, OperationalError) as exc:
        db.rollback()
        raise TransitionConflict(
            f"refund transition {expected} -> {new_status} conflicted "
            f"(uniqueness or lock): {getattr(exc, 'orig', exc)}"
        ) from exc
    if applied != 1:
        db.rollback()
        try:
            fresh = db.get(RefundAttempt, refund.id)
            actual = fresh.status if fresh else "<deleted>"
        except Exception:  # noqa: BLE001
            actual = "<unknown>"
        raise TransitionConflict(
            f"refund transition {expected} -> {new_status} did not apply "
            f"(current status is {actual!r}, or a write-once id differs)."
        )
    db.refresh(refund)
    return refund
