"""Charge settlement (Stage 2c, slice 1 — corrected per slice-1 deep review).

Turns a PROCESSOR_APPROVED PaymentAttempt into exactly one local Payment, under
the reviewer's guardrails:

* **Amount/currency invariant (#3).** For an external provider, the processor
  pre-tip base and currency must equal the attempt snapshot. A mismatch parks the
  attempt in REQUIRES_RECONCILIATION and writes NO Payment.
* **Local-snapshot invariant (slice-2a #1/#3, v4 #1/#2).** For a manual/local
  provider there is no processor evidence, so the Payment the factory books from
  freshly order-row-locked state must instead reproduce the durable attempt exactly —
  every captured component (subtotal, discount, tax, service charge, surcharge, tip),
  the provider-neutral pre-tip ``expected_total_cents`` and the ``total = expected +
  tip`` identity, and the paid-item selection. The committed attempt snapshot (not
  live venue config) is authoritative, so a tax/surcharge/price change after creation
  drifts. Any drift raises ``SettlementDrift`` BEFORE settling; the caller rolls the
  uncommitted Payment back and the attempt stays retryable. No money moves, and —
  unlike external reconciliation — nothing is parked.
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
from app.services.payments import PaymentError

SETTLED = "settled"
RECONCILED = "reconciled"


class SettlementDrift(pa.PaymentAttemptError):
    """The booked local Payment does not match the durable attempt snapshot: the
    payable amount, its components, or the paid-item selection drifted between the
    attempt being created and the locked settlement (a TOCTOU on the order).

    No money moved — the in-memory Payment is never committed — and the durable
    attempt is left retryable (it stays in its pre-settlement state, ``CREATED``,
    after the caller rolls back). Raising, rather than parking for reconciliation,
    is deliberate for local/cash: with no processor to reconcile against, a
    deterministic reject the operator can simply retry is safer than a stuck
    REQUIRES_RECONCILIATION row (slice-2a review #1/#3). The caller MUST roll back."""

    def __init__(self, reason: str):
        super().__init__(f"settlement drift: {reason}")
        self.reason = reason


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


def _is_external(attempt: PaymentAttempt) -> bool:
    from app.services.payment_providers import get_provider
    return get_provider(attempt.provider).is_external


def _external_booking_reason(attempt: PaymentAttempt, payment: Payment) -> str | None:
    """Why a booked EXTERNAL Payment cannot be settled cleanly, or None. The
    processor already captured the money and the processor-vs-attempt amount/currency
    was checked by ``_mismatch_reason``; here we confirm the LOCAL booking (from
    order-row-locked state) still reproduces the attempt's pre-tip amount and paid-item
    selection. If the order drifted after the external capture (an item was paid or
    voided in between), the local Payment cannot match — the caller must park the
    attempt for reconciliation, NOT reject/retry, because external money has moved
    (slice-2b guardrail #6/#7). The tip is the ONLY component allowed to come from
    processor evidence after intent creation, so it is intentionally NOT compared here
    (slice-2b v-fix #3); every other pre-tip component must match the snapshot exactly,
    just like the manual path — a same-total-but-drifted-components booking must NOT
    settle."""
    for field, booked, snap in (
        ("subtotal", payment.items_cents, attempt.subtotal_cents),
        ("discount", payment.discount_cents, attempt.discount_cents),
        ("tax", payment.tax_cents, attempt.tax_cents),
        ("service charge", payment.service_charge_cents, attempt.service_charge_cents),
        ("surcharge", payment.card_surcharge_cents, attempt.surcharge_cents),
    ):
        if booked != snap:
            return f"{field} {booked} != attempt {snap}"
    pre_tip = (payment.items_cents - payment.discount_cents + payment.tax_cents
               + payment.service_charge_cents + payment.card_surcharge_cents)
    if pre_tip != attempt.expected_total_cents:
        return f"local pre-tip {pre_tip} != attempt expected_total {attempt.expected_total_cents}"
    booked_sel = pa.canonical_selection([a.order_item_id for a in payment.allocations])
    if booked_sel != (attempt.line_selection or ""):
        return f"paid-item selection {booked_sel!r} != attempt {attempt.line_selection!r}"
    return None


def _local_snapshot_reason(attempt: PaymentAttempt, payment: Payment) -> str | None:
    """Why a booked LOCAL Payment does not match the durable attempt, or None.

    For a manual/cash provider there is no processor evidence, but the attempt is
    still the authoritative financial intent: the Payment the factory booked (from
    freshly order-row-locked state) must reproduce exactly the amounts and paid-item
    selection the attempt captured. If it doesn't, the payable drifted under the
    lock (an item was paid, a price/discount/service-charge changed, the selection
    moved) and this must NOT settle (slice-2a review #1 / #3). External providers
    reconcile against processor evidence instead (see ``_mismatch_reason``).

    The exact Payment.total formula this app uses (payments.pay_seat) is:
        total = items - discount + tax + tip + service_charge + card_surcharge
    where ``items`` is the pre-tip, pre-discount payable base the attempt stored as
    ``expected_total_cents``/``subtotal_cents``. We validate every attempt-captured
    component plus the internal consistency of ``total_cents`` (tax and surcharge are
    deterministic functions of the captured components + venue config, so proving the
    captured components match and the total reconciles pins the whole amount)."""
    from app.services.payment_providers import get_provider
    if get_provider(attempt.provider).is_external:
        return None
    # 1) Every immutable component captured on the attempt must reproduce exactly —
    #    tax and surcharge included: the committed attempt snapshot, NOT live venue
    #    config, is authoritative (v4 review #2). A config/order change after creation
    #    therefore drifts rather than silently settling on the new values.
    for field, booked, snap in (
        ("subtotal", payment.items_cents, attempt.subtotal_cents),
        ("discount", payment.discount_cents, attempt.discount_cents),
        ("tax", payment.tax_cents, attempt.tax_cents),
        ("service charge", payment.service_charge_cents, attempt.service_charge_cents),
        ("surcharge", payment.card_surcharge_cents, attempt.surcharge_cents),
        ("tip", payment.tip_cents, attempt.tip_cents),
    ):
        if booked != snap:
            return f"{field} {booked} != attempt {snap}"
    # 2) Arithmetic consistency against the provider-neutral field meaning:
    #    expected_total_cents is the PRE-TIP total; the final total adds the tip.
    pre_tip = (payment.items_cents - payment.discount_cents + payment.tax_cents
               + payment.service_charge_cents + payment.card_surcharge_cents)
    if pre_tip != attempt.expected_total_cents:
        return f"pre-tip total {pre_tip} != attempt expected_total {attempt.expected_total_cents}"
    if payment.total_cents != attempt.expected_total_cents + attempt.tip_cents:
        return (f"final total {payment.total_cents} != expected_total + tip "
                f"{attempt.expected_total_cents + attempt.tip_cents}")
    # 3) The paid-item selection actually booked (one allocation per covered item)
    #    must equal the selection the attempt fingerprinted.
    booked_sel = pa.canonical_selection([a.order_item_id for a in payment.allocations])
    if booked_sel != (attempt.line_selection or ""):
        return f"paid-item selection {booked_sel!r} != attempt {attempt.line_selection!r}"
    return None


def _park_reconcile(db: Session, attempt: PaymentAttempt, reason: str,
                    commit: bool) -> SettlementResult:
    """External money moved but the local settlement cannot complete cleanly. Discard
    the uncommitted (mismatched) Payment and park the attempt for reconciliation,
    preserving its provider evidence. If a concurrent settle already won, converge on
    that Payment instead. Requires the attempt's PROCESSOR_APPROVED to be committed
    (the terminal orchestrator commits the evidence before settling), so the rollback
    reverts only the Payment, not the approval."""
    db.rollback()
    db.refresh(attempt)
    if attempt.payment_id is not None:
        # A concurrent settle won — converge on its Payment.
        return SettlementResult(SETTLED, db.get(Payment, attempt.payment_id))
    if attempt.status == PaymentAttemptStatus.REQUIRES_RECONCILIATION:
        # A concurrent writer already parked it — nothing more to do.
        return SettlementResult(RECONCILED, None, reason)
    if attempt.status != PaymentAttemptStatus.PROCESSOR_APPROVED:
        # A peer moved it somewhere terminal (or back) — this settle did not win.
        raise pa.TransitionConflict(
            f"cannot park attempt {attempt.id} for reconciliation: status is {attempt.status!r}")
    pa.transition(db, attempt, PaymentAttemptStatus.REQUIRES_RECONCILIATION,
                  last_error=f"settlement drift: {reason}", commit=commit)
    return SettlementResult(RECONCILED, None, reason)


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

    is_external = _is_external(attempt)

    reason = _mismatch_reason(attempt)
    if reason is not None:
        # External processor evidence disagrees with the snapshot. Park for
        # reconciliation (durably, via the caller's commit) and write no Payment.
        # Returning — not raising — is what makes the reconciliation survive the
        # caller's outer transaction (slice-1 review #1).
        pa.transition(db, attempt, PaymentAttemptStatus.REQUIRES_RECONCILIATION,
                      last_error=f"settlement mismatch: {reason}", commit=commit)
        return SettlementResult(RECONCILED, None, reason)

    # Book the Payment from freshly order-row-locked state. For an EXTERNAL provider
    # the money already moved, so a local booking failure (the order drifted so
    # nothing is outstanding) must reconcile, never raise/retry (slice-2b #7).
    try:
        payment = payment_factory()
        db.flush()  # assign payment.id + allocations; never commit here — caller owns tx
    except PaymentError as exc:
        if is_external:
            return _park_reconcile(
                db, attempt, f"local booking failed after external capture: {exc}", commit)
        raise

    # Post-booking consistency. EXTERNAL: the local booking must reproduce the
    # attempt's pre-tip amount + selection, else the order drifted after money moved
    # -> reconcile (#6/#7). LOCAL/manual: the full snapshot must match exactly, else
    # drift -> raise so the caller rolls back and retries (no money moved).
    if is_external:
        reason = _external_booking_reason(attempt, payment)
        if reason is not None:
            return _park_reconcile(db, attempt, reason, commit)
    else:
        drift = _local_snapshot_reason(attempt, payment)
        if drift is not None:
            raise SettlementDrift(drift)

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
