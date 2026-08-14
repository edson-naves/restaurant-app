"""Charge orchestration (Stage 2c, slice 2a — manual/cash path).

Wires a live charge through the durable PaymentAttempt lifecycle + the settlement
service, so a cash/manual payment gets the same crash-safe, idempotent path as a
card charge — without any external processor id (slice-2 guardrail #14). Layering:

* ``pay_seat`` (payments.py) is the **no-commit core**: it locks the order row,
  re-validates the payable from current state, and mutates Payment + allocations +
  seat status with ``flush`` only (never commit/rollback).
* ``settle_charge`` (settlement.py) turns the approved attempt into exactly one
  Payment via that core and links it write-once, after checking the booked Payment
  reproduces the attempt snapshot exactly (the local-snapshot invariant).
* ``settle_manual_charge`` here snapshots the full money breakdown onto a durable
  attempt (committed first), then settles atomically — Payment + allocations + seat
  state + attempt→SETTLED in one outer commit (guardrail #6).

**Attempt money semantics (provider-neutral, per v4 review #1).** The attempt stores
the component snapshot — ``subtotal_cents``, ``discount_cents``, ``tax_cents``,
``service_charge_cents``, ``surcharge_cents``, ``tip_cents`` — and
``expected_total_cents`` is the **pre-tip** amount
``subtotal - discount + tax + service_charge + surcharge`` (what a processor
authorizes), identical in meaning to the external/Square path. The final charged
amount is ``expected_total_cents + tip_cents``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session

from app.config import venue_currency
from app.models.oltp import PaymentAttempt, PaymentAttemptStatus as S, PaymentInstrument
from app.services import payment_attempts as pa
from app.services import payments as payments_svc
from app.services import settlement as settle
from app.services import square
from app.services.money import pct
from app.services.payment_providers import ChargeResult, get_provider

# Back-compat alias (this module referred to the status class as PaymentAttemptStatus).
PaymentAttemptStatus = S

# Bounded transport-ambiguity escalation (slice-2b v-fix #6). A terminal checkout has
# a ~5-minute Square deadline; a poll still PENDING (including transient transport
# errors mapped to PENDING) past this age stops polling forever and hands the attempt
# to reconciliation / the recovery worker rather than staying pending indefinitely.
TERMINAL_PENDING_DEADLINE_S = 360


def settle_manual_charge(
    db: Session,
    order,
    seat,
    *,
    staff_id: int,
    instrument_id: int,
    base_cents: int,
    selected_item_ids: list[int],
    payment_factory: Callable,
    tip_cents: int = 0,
    service_charge_cents: int = 0,
    discount_cents: int = 0,
    card_surcharge_rate: float = 0.0,
    idempotency_key: str | None = None,
    commit: bool = True,
) -> tuple[settle.SettlementResult, PaymentAttempt]:
    """Create a durable manual PaymentAttempt (instant-approved, no external id)
    and settle it into exactly one Payment via ``payment_factory`` (the no-commit
    ``pay_seat`` core). Idempotent on the idempotency key + paid-item selection.

    The full money breakdown (from ``base_cents`` = the payable subtotal) is snapshot
    onto the attempt using the same ``compute_breakdown`` that ``pay_seat`` books, so
    ``expected_total_cents`` is the pre-tip total and every component (tax and
    surcharge included) is captured. The attempt is committed first (durable intent);
    the settlement — Payment + allocations + seat state + attempt→SETTLED — commits
    atomically in one outer transaction. Returns (result, attempt).
    """
    instrument = db.get(PaymentInstrument, instrument_id)
    if instrument is None:
        raise payments_svc.PaymentError("Unknown payment instrument.")
    bd = payments_svc.compute_breakdown(
        db, instrument, items_cents=base_cents, tip_cents=tip_cents,
        service_charge_cents=service_charge_cents, card_surcharge_rate=card_surcharge_rate,
        discount_cents=discount_cents,
    )
    attempt = pa.create_attempt(
        db, provider="manual", order_id=order.id, seat_id=seat.id, staff_id=staff_id,
        expected_total_cents=bd.expected_total_cents, subtotal_cents=bd.items_cents,
        tax_cents=bd.tax_cents, tip_cents=bd.tip_cents,
        service_charge_cents=bd.service_charge_cents, discount_cents=bd.discount_cents,
        surcharge_cents=bd.card_surcharge_cents, currency=venue_currency(),
        item_ids=selected_item_ids, idempotency_key=idempotency_key,
    )
    # Manual = instant approval; a settled attempt short-circuits in settle_charge
    # (idempotent retry -> the existing Payment).
    if attempt.payment_id is None:
        if attempt.status == PaymentAttemptStatus.CREATED:
            pa.transition(db, attempt, PaymentAttemptStatus.PROCESSOR_PENDING, commit=False)
        if attempt.status == PaymentAttemptStatus.PROCESSOR_PENDING:
            pa.transition(db, attempt, PaymentAttemptStatus.PROCESSOR_APPROVED, commit=False)
    try:
        result = settle.settle_charge(db, attempt, payment_factory=payment_factory, commit=False)
    except settle.SettlementDrift:
        # The payable/selection drifted under the order lock: no money moved. Discard
        # the uncommitted Payment and the in-memory approval transitions; the durable
        # attempt reverts to CREATED (committed earlier) and stays retryable. Re-raise
        # for the caller to surface as a conflict.
        db.rollback()
        raise
    if commit:
        db.commit()
    return result, attempt


# --------------------------------------------------------------------------
# Square terminal (Stage 2c, slice 2b — external card-present path)
#
# The durable attempt is the crash-safe spine: it is committed BEFORE any Square
# call (#1), keyed by the request idempotency token so a retry reuses one attempt
# and cannot double-charge (#2), and NO order-row lock is held while the customer
# taps + tips (#3). On completion the processor evidence (pre-tip base, currency,
# tip, payment id) is committed as PROCESSOR_APPROVED BEFORE local settlement (#8),
# then settled with the terminal-confirmed tip (#5). Because external money has
# moved, a local inconsistency (order drift after capture, amount/currency mismatch)
# parks REQUIRES_RECONCILIATION — never a manual-style reject/retry (#6/#7).
# --------------------------------------------------------------------------


def start_terminal_attempt(
    db: Session,
    order,
    seat,
    *,
    staff_id: int,
    instrument_id: int,
    item_ids: list[int] | None = None,
    service_charge_rate: float = 0.0,
    card_surcharge_rate: float = 0.0,
    idempotency_key: str,
    reference: str = "",
    note: str = "",
    commit: bool = True,
) -> tuple[PaymentAttempt, ChargeResult]:
    """Snapshot the seat's payable under a SHORT phase-1 order-row lock, create +
    commit the durable attempt from that locked snapshot, RELEASE the lock, then send
    the pre-tip amount to the terminal using the attempt's own idempotency key
    (slice-2b v-fix #1). No order lock is held across Square I/O. Idempotent on the
    request token: a double-submit reuses the attempt and, if a checkout already
    exists, does NOT open a second one. Returns (attempt, ChargeResult)."""
    provider = get_provider("square_terminal")
    instrument = db.get(PaymentInstrument, instrument_id)
    if instrument is None:
        raise payments_svc.PaymentError("Unknown payment instrument.")
    currency = square.currency()

    # --- PHASE 1: locked snapshot -> create attempt -> commit (releases the lock) ---
    selected, base_cents = payments_svc.locked_seat_payable(db, order, seat, item_ids=item_ids)
    service_charge_cents = pct(base_cents, service_charge_rate)
    bd = payments_svc.compute_breakdown(
        db, instrument, items_cents=base_cents, service_charge_cents=service_charge_cents,
        card_surcharge_rate=card_surcharge_rate,
    )
    attempt = pa.create_attempt(
        db, provider="square_terminal", order_id=order.id, seat_id=seat.id, staff_id=staff_id,
        expected_total_cents=bd.expected_total_cents, subtotal_cents=bd.items_cents,
        tax_cents=bd.tax_cents, service_charge_cents=bd.service_charge_cents,
        discount_cents=bd.discount_cents, surcharge_cents=bd.card_surcharge_cents, tip_cents=0,
        currency=currency, item_ids=selected, idempotency_key=idempotency_key,
    )
    # create_attempt commits a NEW attempt (releasing the lock). On an idempotent hit
    # it returns WITHOUT committing, so commit here to guarantee the phase-1 lock is
    # released before any Square I/O (guardrail #3).
    db.commit()

    # Idempotent double-submit: the intent already progressed (a checkout exists or it
    # already resolved). Do not contact Square again — return its current state.
    if attempt.provider_checkout_id is not None or attempt.status != S.CREATED:
        return attempt, ChargeResult(status=attempt.status,
                                     provider_checkout_id=attempt.provider_checkout_id,
                                     provider_payment_id=attempt.provider_payment_id)

    # --- PHASE 2: Square interaction, NO order lock held ---
    result = provider.charge(
        amount_cents=bd.expected_total_cents, currency=currency,
        idempotency_key=attempt.idempotency_key, reference=reference, note=note,
    )
    if result.status == S.PROCESSOR_PENDING:
        pa.transition(db, attempt, S.PROCESSOR_PENDING,
                      provider_checkout_id=result.provider_checkout_id, commit=commit)
    elif result.status == S.FAILED:
        pa.transition(db, attempt, S.FAILED, last_error=result.error, commit=commit)
    elif result.status == S.REQUIRES_RECONCILIATION:
        pa.transition(db, attempt, S.REQUIRES_RECONCILIATION, last_error=result.error,
                      provider_checkout_id=result.provider_checkout_id, commit=commit)
    return attempt, result


def _park_terminal(db: Session, attempt: PaymentAttempt, reason: str, *, commit: bool) -> dict:
    """Park an approved-but-unsettleable terminal attempt for reconciliation and
    return the poll status dict. Only moves an attempt still in PROCESSOR_APPROVED."""
    if attempt.status == S.PROCESSOR_APPROVED:
        pa.transition(db, attempt, S.REQUIRES_RECONCILIATION, last_error=reason, commit=commit)
    return {"state": "reconciling", "message": reason}


def advance_terminal_attempt(
    db: Session,
    order,
    seat,
    attempt: PaymentAttempt,
    result: ChargeResult,
    *,
    staff_id: int,
    instrument_id: int,
    card_surcharge_rate: float = 0.0,
    is_partial: bool = False,
    commit: bool = True,
) -> dict:
    """Advance an in-flight terminal attempt from a fresh poll ``ChargeResult``.

    On PROCESSOR_APPROVED, the processor evidence is committed durably BEFORE local
    settlement (guardrail #8: a Square-success/local-failure is then recoverable by a
    worker), then the attempt settles into one Payment using the terminal-confirmed
    tip (#5) via the no-commit ``pay_seat`` core (which reacquires the order-row lock
    and re-validates, #6). A local drift after capture, or an amount/currency mismatch,
    parks REQUIRES_RECONCILIATION (#7). Returns a status dict for the poll endpoint.
    """
    if attempt.status == S.SETTLED:
        return {"state": "done", "payment_id": attempt.payment_id}

    st = result.status
    if st == S.CANCELLED:
        if attempt.status in (S.CREATED, S.PROCESSOR_PENDING):
            pa.transition(db, attempt, S.CANCELLED, commit=commit)
        return {"state": "canceled"}

    if st == S.REQUIRES_RECONCILIATION:
        # Ambiguous/incoherent processor outcome (transport, no payment id, bad
        # evidence). Preserve any provider payment id and park (#9).
        if attempt.status in (S.CREATED, S.PROCESSOR_PENDING):
            pa.transition(db, attempt, S.REQUIRES_RECONCILIATION, last_error=result.error,
                          provider_payment_id=result.provider_payment_id, commit=commit)
        return {"state": "reconciling", "message": result.error}

    if st != S.PROCESSOR_APPROVED:
        # Still on the machine, or a transient transport error the provider mapped to
        # PENDING. Bounded escalation (#6): past the terminal deadline, stop polling
        # forever and hand it to reconciliation instead of pending indefinitely.
        if attempt.status in (S.CREATED, S.PROCESSOR_PENDING):
            age = (datetime.now() - attempt.created_at).total_seconds()
            if age > TERMINAL_PENDING_DEADLINE_S:
                pa.transition(db, attempt, S.REQUIRES_RECONCILIATION,
                              last_error=f"terminal pending past deadline ({int(age)}s): "
                                         f"{result.error or 'no completion'}", commit=commit)
                return {"state": "reconciling",
                        "message": "The terminal did not complete in time — the payment "
                                   "is being verified before any retry."}
        return {"state": "pending", "status": result.error or ""}

    # Approved: record the authoritative evidence — INCLUDING the terminal tip —
    # durably BEFORE settling (#8/#2), so a crash/restart recovers the real tip
    # rather than inferring zero. The tip is write-once processor evidence.
    if attempt.status == S.PROCESSOR_PENDING:
        pa.transition(db, attempt, S.PROCESSOR_APPROVED,
                      provider_payment_id=result.provider_payment_id,
                      processor_amount_cents=result.processor_amount_cents,
                      processor_currency=result.processor_currency,
                      processor_tip_cents=result.tip_cents, commit=True)
    elif attempt.status == S.PROCESSOR_APPROVED:
        # Recovery / duplicate poll: the tip is already durable. A fresh poll that
        # disagrees is conflicting evidence — reconcile, never overwrite (fail closed).
        if (attempt.processor_tip_cents is not None and result.tip_cents is not None
                and result.tip_cents != attempt.processor_tip_cents):
            return _park_terminal(
                db, attempt,
                f"conflicting tip evidence {result.tip_cents} != recorded "
                f"{attempt.processor_tip_cents}", commit=commit)

    # Settle from the DURABLE tip evidence, never a transient/inferred value (#2).
    tip = attempt.processor_tip_cents
    if tip is None:
        return _park_terminal(
            db, attempt, "no durable processor tip evidence on the approved attempt",
            commit=commit)

    selected = [int(x) for x in (attempt.line_selection or "").split(",") if x]

    def factory():
        return payments_svc.pay_seat(
            db, order, seat, instrument_id=instrument_id, staff_id=staff_id,
            tip_cents=tip, service_charge_cents=attempt.service_charge_cents,
            card_surcharge_rate=card_surcharge_rate, item_ids=selected,
            card_last4=result.card_last4, card_brand=result.card_brand,
            is_partial_close=is_partial,
        )

    res = settle.settle_charge(db, attempt, payment_factory=factory, commit=commit)
    if res.is_settled:
        return {"state": "done", "payment_id": res.payment.id, "tip_cents": tip}
    return {"state": "reconciling", "message": res.reason}
