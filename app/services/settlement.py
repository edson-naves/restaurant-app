"""Charge settlement (Stage 2c, slice 1 — corrected per slice-1 deep review).

Turns a PROCESSOR_APPROVED PaymentAttempt into exactly one local Payment, under
the reviewer's guardrails:

* **Amount/currency invariant (#3).** For an external provider, the processor
  pre-tip base and currency must equal the attempt snapshot. A mismatch parks the
  attempt in REQUIRES_RECONCILIATION and writes NO Payment.
* **Idempotent local ledger (#6).** At most one Payment per attempt (write-once,
  unique ``payment_id``); a retry converges on the existing Payment.

**Transaction contract (slice-1 review #1, #3).** ``settle_charge`` never uses an
exception to control the transaction: a mismatch is reported as a structured
``SettlementResult`` so the *caller* commits the reconciliation transition
intentionally (in slice 2 the live route owns the order-row lock and a single
commit). ``payment_factory`` MUST use the same Session and MUST NOT commit or
rollback — it mutates + flushes only, so a lost CAS race rolls the Payment back
with the attempt transition. (Slice 2 refactors ``pay_seat`` into a no-commit
core to honour this.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.config import venue_currency
from app.models.oltp import Payment, PaymentAttempt, PaymentAttemptStatus
from app.services import payment_attempts as pa

SETTLED = "settled"
RECONCILED = "reconciled"


@dataclass
class SettlementResult:
    """Outcome of a settlement attempt. ``settled`` carries the one Payment;
    ``reconciled`` means processor evidence disagreed and the attempt was parked
    (no Payment). No exception is used to drive the caller's transaction."""
    status: str
    payment: Payment | None = None
    reason: str = ""

    @property
    def is_settled(self) -> bool:
        return self.status == SETTLED


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
) -> SettlementResult:
    """Settle an approved attempt into exactly one Payment. Idempotent.

    Returns a ``SettlementResult`` — never raises to signal a mismatch, so the
    caller's transaction (which owns the reconciliation transition) is committed
    intentionally rather than rolled back by an escaping exception.
    """
    # Idempotent: already settled -> return the one Payment, create nothing.
    if attempt.payment_id is not None:
        return SettlementResult(SETTLED, db.get(Payment, attempt.payment_id))

    if attempt.status != PaymentAttemptStatus.PROCESSOR_APPROVED:
        raise pa.PaymentAttemptError(
            f"cannot settle an attempt in status {attempt.status!r}; "
            "only a PROCESSOR_APPROVED attempt settles.")

    reason = _mismatch_reason(attempt)
    if reason is not None:
        # Park for reconciliation (durably, via the caller's commit) and write no
        # Payment. Returning — not raising — is what makes the reconciliation
        # survive the caller's outer transaction (slice-1 review #1).
        pa.transition(db, attempt, PaymentAttemptStatus.REQUIRES_RECONCILIATION,
                      last_error=f"settlement mismatch: {reason}", commit=commit)
        return SettlementResult(RECONCILED, None, reason)

    payment = payment_factory()
    db.flush()  # assign payment.id; never commit here — the caller owns the tx
    try:
        pa.transition(db, attempt, PaymentAttemptStatus.SETTLED,
                      payment_id=payment.id, commit=commit)
    except pa.TransitionConflict:
        # A CAS loss is NOT automatically "someone settled". Roll our Payment back
        # and converge ONLY on a proven winner: the DB truth must show SETTLED +
        # an existing payment_id. Anything else (moved to reconciliation, etc.) is
        # a real conflict and is re-raised (slice-1 review #2).
        db.rollback()
        db.refresh(attempt)
        winner = db.get(Payment, attempt.payment_id) if attempt.payment_id is not None else None
        if attempt.status == PaymentAttemptStatus.SETTLED and winner is not None:
            return SettlementResult(SETTLED, winner)
        raise
    return SettlementResult(SETTLED, payment)
