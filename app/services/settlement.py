"""Charge settlement (Stage 2c, slice 1).

Turns a PROCESSOR_APPROVED PaymentAttempt into exactly one local Payment, under
two guardrails the reviewer requires before live wiring:

* **Amount/currency invariant (guardrail #3).** For an external provider, the
  processor-confirmed pre-tip base and currency must match the attempt's
  immutable snapshot (``expected_total_cents`` / ``currency``). A mismatch does
  NOT settle — the attempt is parked in REQUIRES_RECONCILIATION and no Payment is
  written. The order/payment is never silently adjusted to match the processor.

* **Idempotent local ledger (guardrail #6).** At most one Payment per attempt: a
  settled attempt already carries ``payment_id`` (write-once, unique), so a retry
  after a processor-success + local-failure converges on the existing Payment
  instead of creating a second one.

This module is provider-neutral and does not itself build a Payment — the caller
passes a ``payment_factory`` that performs the venue's real Payment creation
(``pay_seat`` etc.). Concurrency is serialized by the caller's order-row
``SELECT ... FOR UPDATE`` (slice 2); this service adds the attempt-level guards.
"""
from __future__ import annotations

from typing import Callable

from sqlalchemy.orm import Session

from app.config import venue_currency
from app.models.oltp import Payment, PaymentAttempt, PaymentAttemptStatus
from app.services import payment_attempts as pa


class SettlementMismatch(pa.PaymentAttemptError):
    """Processor evidence disagrees with the attempt's snapshot — do not settle."""


def _mismatch_reason(attempt: PaymentAttempt) -> str | None:
    """Why an external attempt must not settle, or None if it may. Manual/local
    providers have no external evidence to reconcile against."""
    from app.services.payment_providers import get_provider
    if not get_provider(attempt.provider).is_external:
        return None
    want_cur = (attempt.currency or venue_currency()).upper()
    got_cur = (attempt.processor_currency or "").upper()
    if got_cur != want_cur:
        return f"currency {got_cur or '<none>'} != expected {want_cur}"
    if attempt.processor_amount_cents is None:
        return "no processor amount recorded"
    if attempt.processor_amount_cents != attempt.expected_total_cents:
        return (f"processor base {attempt.processor_amount_cents} != expected "
                f"{attempt.expected_total_cents}")
    return None


def settle_charge(
    db: Session,
    attempt: PaymentAttempt,
    *,
    payment_factory: Callable[[], Payment],
    commit: bool = True,
) -> Payment:
    """Settle an approved attempt into exactly one Payment. Idempotent.

    Returns the existing Payment on a retry. Raises ``SettlementMismatch`` (after
    parking the attempt in REQUIRES_RECONCILIATION) when processor evidence does
    not match the snapshot — no Payment is created in that case.
    """
    # Idempotent: already settled -> return the one Payment, create nothing.
    if attempt.payment_id is not None:
        return db.get(Payment, attempt.payment_id)

    if attempt.status != PaymentAttemptStatus.PROCESSOR_APPROVED:
        raise pa.PaymentAttemptError(
            f"cannot settle an attempt in status {attempt.status!r}; "
            "only a PROCESSOR_APPROVED attempt settles.")

    reason = _mismatch_reason(attempt)
    if reason is not None:
        # Do not settle a disagreeing charge — park it, write no Payment.
        pa.transition(db, attempt, PaymentAttemptStatus.REQUIRES_RECONCILIATION,
                      last_error=f"settlement mismatch: {reason}", commit=commit)
        raise SettlementMismatch(reason)

    payment = payment_factory()
    db.flush()  # assign payment.id
    try:
        pa.transition(db, attempt, PaymentAttemptStatus.SETTLED,
                      payment_id=payment.id, commit=commit)
    except pa.TransitionConflict:
        # A concurrent settle won (same order lock normally prevents this). Roll
        # back our Payment and converge on the winner's.
        db.rollback()
        db.refresh(attempt)
        return db.get(Payment, attempt.payment_id)
    return payment
