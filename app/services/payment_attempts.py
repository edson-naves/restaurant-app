"""Durable payment-attempt lifecycle (audit findings #1–#5, Stage 2a).

A ``PaymentAttempt`` is the crash-safe spine of a card charge: it is written and
committed *before* Square is contacted, then walked through an explicit state
machine as the processor responds. This module owns creation and every state
transition; nothing else should mutate ``attempt.status`` directly.

Stage 2a provides the record and its guarantees (idempotent create, legal
transitions, one-Payment-per-attempt). Wiring it into the live Square terminal
flow and settlement is Stage 2b–2c.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.oltp import (
    PAYMENT_ATTEMPT_TRANSITIONS,
    PaymentAttempt,
    PaymentAttemptStatus,
)


class PaymentAttemptError(RuntimeError):
    """Raised on an illegal state transition or a settlement invariant breach."""


def new_idempotency_key() -> str:
    """A fresh, unguessable idempotency key for a processor request."""
    return secrets.token_hex(24)


def create_attempt(
    db: Session,
    *,
    order_id: int,
    staff_id: int,
    expected_total_cents: int,
    seat_id: int | None = None,
    subtotal_cents: int = 0,
    tax_cents: int = 0,
    tip_cents: int = 0,
    service_charge_cents: int = 0,
    discount_cents: int = 0,
    surcharge_cents: int = 0,
    currency: str = "CAD",
    provider: str = "square",
    idempotency_key: str | None = None,
) -> PaymentAttempt:
    """Persist a CREATED attempt from an already-locked payable snapshot. Commits.

    Idempotent: if ``idempotency_key`` is given and an attempt with it already
    exists, the existing row is returned unchanged — a retried request never
    creates a second attempt (and therefore never a second charge).
    """
    if idempotency_key:
        existing = db.execute(
            select(PaymentAttempt).where(
                PaymentAttempt.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    else:
        idempotency_key = new_idempotency_key()

    if expected_total_cents < 0:
        raise PaymentAttemptError("expected_total_cents cannot be negative.")

    attempt = PaymentAttempt(
        order_id=order_id,
        seat_id=seat_id,
        staff_id=staff_id,
        provider=provider,
        idempotency_key=idempotency_key,
        subtotal_cents=subtotal_cents,
        tax_cents=tax_cents,
        tip_cents=tip_cents,
        service_charge_cents=service_charge_cents,
        discount_cents=discount_cents,
        surcharge_cents=surcharge_cents,
        expected_total_cents=expected_total_cents,
        currency=currency,
        status=PaymentAttemptStatus.CREATED,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt


def transition(
    db: Session,
    attempt: PaymentAttempt,
    new_status: str,
    *,
    provider_checkout_id: str | None = None,
    provider_payment_id: str | None = None,
    provider_refund_id: str | None = None,
    payment_id: int | None = None,
    last_error: str | None = None,
    commit: bool = True,
) -> PaymentAttempt:
    """Move ``attempt`` to ``new_status`` if the transition is allowed.

    Raises ``PaymentAttemptError`` on an illegal transition. Provider identifiers
    are only ever set (write-once); an attempt to overwrite an existing, differing
    identifier is rejected so processor traceability cannot be silently rewritten.
    """
    allowed = PAYMENT_ATTEMPT_TRANSITIONS.get(attempt.status, set())
    if new_status not in allowed:
        raise PaymentAttemptError(
            f"illegal transition {attempt.status} -> {new_status} "
            f"(allowed: {sorted(allowed) or 'none — terminal state'})"
        )

    _set_once(attempt, "provider_checkout_id", provider_checkout_id)
    _set_once(attempt, "provider_payment_id", provider_payment_id)
    _set_once(attempt, "provider_refund_id", provider_refund_id)

    if new_status == PaymentAttemptStatus.SETTLED:
        if payment_id is None:
            raise PaymentAttemptError("settling an attempt requires a payment_id.")
        _set_once(attempt, "payment_id", payment_id)
    elif payment_id is not None:
        _set_once(attempt, "payment_id", payment_id)

    if last_error is not None:
        attempt.last_error = last_error

    attempt.status = new_status
    if commit:
        db.commit()
        db.refresh(attempt)
    return attempt


def _set_once(attempt: PaymentAttempt, field: str, value) -> None:
    """Write ``value`` into a write-once field, or no-op if unchanged. Rejects an
    attempt to change an already-set, differing value."""
    if value is None:
        return
    current = getattr(attempt, field)
    if current in (None, "", 0):
        setattr(attempt, field, value)
    elif current != value:
        raise PaymentAttemptError(
            f"{field} is already set to {current!r}; refusing to overwrite with {value!r}."
        )


def requires_reconciliation(db: Session) -> list[PaymentAttempt]:
    """Attempts a recovery worker/human must resolve — either explicitly flagged,
    or approved by the processor but never settled locally (the classic
    'Square charged, app lost it' case). Stage 2d consumes this."""
    stuck = {
        PaymentAttemptStatus.REQUIRES_RECONCILIATION,
        PaymentAttemptStatus.PROCESSOR_APPROVED,
        PaymentAttemptStatus.REFUND_PENDING,
    }
    return list(
        db.execute(
            select(PaymentAttempt)
            .where(PaymentAttempt.status.in_(stuck))
            .order_by(PaymentAttempt.created_at)
        ).scalars()
    )
