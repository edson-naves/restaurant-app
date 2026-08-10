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

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import venue_currency
from app.models.oltp import (
    REFUND_ATTEMPT_TRANSITIONS,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    RefundAttempt,
    RefundAttemptStatus,
)
from app.services.payment_attempts import (
    IdempotencyConflict,
    PaymentAttemptError,
    TransitionConflict,
    _validate_provider,
    is_lock_conflict,
)


def refund_intent_fingerprint(
    *, payment_id: int, charge_attempt_id: int | None, provider: str,
    amount_cents: int, currency: str,
) -> str:
    """Stable hash of the immutable refund intent behind an idempotency key."""
    canonical = "|".join(str(x) for x in (
        payment_id, charge_attempt_id, provider, amount_cents, currency.upper()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:64]


def _resolve_charge_attempt(
    db: Session, *, payment_id: int, charge_attempt_id: int | None, provider: str,
) -> PaymentAttempt | None:
    """Tie the refund to its charge attempt and validate the relationship (#7).

    Prefers deriving the charge attempt from ``payment_id`` (a Payment backs at
    most one settled attempt) rather than trusting a caller-supplied id. When a
    charge attempt is found it must match on payment, be SETTLED, and share the
    refund's provider. Returns the validated PaymentAttempt, or None for a legacy
    payment with no attempt — in which case the provider is derived from the
    Payment's instrument and a caller mismatch is rejected (#4).
    """
    derived = db.execute(
        select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id)
    ).scalar_one_or_none()

    if charge_attempt_id is not None:
        supplied = db.get(PaymentAttempt, charge_attempt_id)
        if supplied is None:
            raise PaymentAttemptError(f"charge attempt {charge_attempt_id} does not exist.")
        if derived is not None and derived.id != supplied.id:
            raise PaymentAttemptError(
                f"charge attempt {charge_attempt_id} does not back payment {payment_id}.")
        attempt = supplied
    else:
        attempt = derived

    if attempt is None:
        # Legacy payment predating PaymentAttempt: derive the canonical provider
        # from the original Payment's instrument and reject a caller mismatch —
        # never trust the caller to say what a historical payment was (#4).
        payment = db.get(Payment, payment_id)
        if payment is None:
            raise PaymentAttemptError(f"payment {payment_id} does not exist.")
        inst_provider = payment.instrument.provider if payment.instrument else None
        if inst_provider and inst_provider != provider:
            raise PaymentAttemptError(
                f"refund provider {provider!r} != payment instrument provider "
                f"{inst_provider!r} (legacy payment {payment_id}).")
        return None

    if attempt.payment_id != payment_id:
        raise PaymentAttemptError(
            f"charge attempt {attempt.id} is not for payment {payment_id}.")
    if attempt.status != PaymentAttemptStatus.SETTLED:
        raise PaymentAttemptError(
            f"cannot refund against a non-settled charge attempt (status {attempt.status!r}).")
    if attempt.provider != provider:
        raise PaymentAttemptError(
            f"refund provider {provider!r} != charge provider {attempt.provider!r}.")
    return attempt


def _validate_refund_currency(currency: str, charge: PaymentAttempt | None) -> None:
    """A refund must be in the same currency as the money it reverses (#5).

    Attempt-backed: match the charge attempt's currency and, when present, its
    processor-confirmed currency. Legacy (no attempt): match the venue currency,
    the best authoritative record when a Payment carries no per-row currency."""
    want = currency.upper()
    if charge is not None:
        if want != charge.currency.upper():
            raise PaymentAttemptError(
                f"refund currency {want} != charge currency {charge.currency.upper()}.")
        if charge.processor_currency and want != charge.processor_currency.upper():
            raise PaymentAttemptError(
                f"refund currency {want} != processor currency {charge.processor_currency.upper()}.")
    else:
        venue = venue_currency()
        if want != venue:
            raise PaymentAttemptError(
                f"refund currency {want} != venue currency {venue} (legacy payment).")


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
    on the idempotency key: a repeat with the same intent returns the same row; a
    repeat with a *different* intent raises ``IdempotencyConflict`` (#3). The
    charge-attempt linkage is validated (#7)."""
    _validate_provider(provider)
    if amount_cents <= 0:
        raise PaymentAttemptError("refund amount must be positive.")

    charge = _resolve_charge_attempt(
        db, payment_id=payment_id, charge_attempt_id=charge_attempt_id, provider=provider)
    _validate_refund_currency(currency, charge)
    charge_attempt_id = charge.id if charge is not None else None
    fingerprint = refund_intent_fingerprint(
        payment_id=payment_id, charge_attempt_id=charge_attempt_id, provider=provider,
        amount_cents=amount_cents, currency=currency)

    if idempotency_key:
        existing = _by_key(db, idempotency_key)
        if existing is not None:
            _assert_same_intent(existing, fingerprint)
            return existing
    else:
        idempotency_key = new_idempotency_key()

    refund = RefundAttempt(
        payment_id=payment_id, charge_attempt_id=charge_attempt_id,
        staff_id=staff_id, provider=provider, amount_cents=amount_cents,
        currency=currency, idempotency_key=idempotency_key,
        intent_fingerprint=fingerprint, status=RefundAttemptStatus.CREATED,
    )
    db.add(refund)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _by_key(db, idempotency_key)
        if existing is None:
            raise
        _assert_same_intent(existing, fingerprint)
        return existing
    db.refresh(refund)
    return refund


def _assert_same_intent(existing: RefundAttempt, fingerprint: str) -> None:
    if existing.intent_fingerprint and existing.intent_fingerprint != fingerprint:
        raise IdempotencyConflict(
            f"refund idempotency key {existing.idempotency_key!r} was already used for a "
            f"different refund intent (refund attempt {existing.id}).")


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
    except IntegrityError as exc:
        db.rollback()
        raise TransitionConflict(
            f"refund transition {expected} -> {new_status} conflicted "
            f"(uniqueness): {getattr(exc, 'orig', exc)}"
        ) from exc
    except OperationalError as exc:
        db.rollback()
        if is_lock_conflict(exc):
            raise TransitionConflict(
                f"refund transition {expected} -> {new_status} lost a lock/deadlock race: "
                f"{getattr(exc, 'orig', exc)}"
            ) from exc
        raise  # genuine infrastructure failure — propagate
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
