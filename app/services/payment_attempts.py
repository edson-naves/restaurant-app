"""Durable payment-attempt lifecycle (audit findings #1–#5, hardened per review).

A ``PaymentAttempt`` is the crash-safe spine of a charge: written and committed
*before* the processor is contacted, then walked through an explicit state machine
via concurrency-safe transitions. This module owns creation and every transition;
nothing else mutates ``attempt.status`` directly.

Concurrency/idempotency guarantees (Postgres and SQLite):

* **Atomic create** — concurrent requests reusing one idempotency key resolve to
  the *same* attempt; the DB unique constraint is caught and re-read, never
  surfaced as an ``IntegrityError`` (finding #2).
* **Intent fingerprint** — reusing a key with a *different* order/amount/currency
  is an explicit conflict, not a silent wrong-attempt hit (finding #3).
* **Compare-and-swap transitions** — a transition is a single guarded UPDATE
  conditioned on the expected current status, so two conflicting transitions
  cannot both win (finding #4).
* **Reconciliation resolution** — leaving REQUIRES_RECONCILIATION demands
  evidence and goes through ``resolve_reconciliation``, never a bare transition
  (finding #12).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.oltp import (
    PAYMENT_ATTEMPT_TRANSITIONS,
    PaymentAttempt,
    PaymentAttemptStatus,
)


class PaymentAttemptError(RuntimeError):
    """Illegal transition or a settlement invariant breach."""


class IdempotencyConflict(PaymentAttemptError):
    """An idempotency key was reused for a materially different intent."""


class TransitionConflict(PaymentAttemptError):
    """A concurrent writer moved the attempt out from under this transition."""


# Postgres SQLSTATEs that mean "a concurrency race", not "infrastructure is down":
# deadlock_detected, serialization_failure, lock_not_available.
_LOCK_SQLSTATES = frozenset({"40P01", "40001", "55P03"})


def is_lock_conflict(exc: Exception) -> bool:
    """True when an OperationalError is a lock/deadlock race (a transition
    conflict) rather than a genuine infrastructure failure (DB down, connection
    reset). Infra failures must propagate, not masquerade as a conflict (#12)."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate in _LOCK_SQLSTATES:
        return True
    msg = str(orig or exc).lower()
    return ("deadlock" in msg or "database is locked" in msg
            or "database table is locked" in msg)


def new_idempotency_key() -> str:
    return secrets.token_hex(24)


def intent_fingerprint(
    *,
    provider: str,
    order_id: int,
    seat_id: int | None,
    staff_id: int,
    currency: str,
    expected_total_cents: int,
    subtotal_cents: int,
    tax_cents: int,
    tip_cents: int,
    service_charge_cents: int,
    discount_cents: int,
    surcharge_cents: int,
) -> str:
    """Stable hash of the immutable intent behind an idempotency key. Reusing a
    key with a different fingerprint is rejected as a conflict."""
    canonical = "|".join(str(x) for x in (
        provider, order_id, seat_id, staff_id, currency.upper(),
        expected_total_cents, subtotal_cents, tax_cents, tip_cents,
        service_charge_cents, discount_cents, surcharge_cents,
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:64]


def _validate_provider(provider: str) -> None:
    if not provider:
        raise PaymentAttemptError("provider is required (no silent default).")
    # Lazy import avoids any import-order coupling with the provider registry.
    from app.services.payment_providers import UnknownProvider, get_provider
    try:
        get_provider(provider)
    except UnknownProvider as exc:
        raise PaymentAttemptError(str(exc)) from exc


def _by_key(db: Session, key: str) -> PaymentAttempt | None:
    return db.execute(
        select(PaymentAttempt).where(PaymentAttempt.idempotency_key == key)
    ).scalar_one_or_none()


def _assert_same_intent(existing: PaymentAttempt, fingerprint: str) -> None:
    if existing.intent_fingerprint and existing.intent_fingerprint != fingerprint:
        raise IdempotencyConflict(
            f"idempotency key {existing.idempotency_key!r} was already used for a "
            f"different payment intent (attempt {existing.id})."
        )


def create_attempt(
    db: Session,
    *,
    provider: str,
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
    idempotency_key: str | None = None,
) -> PaymentAttempt:
    """Persist a CREATED attempt from an already-locked payable snapshot. Commits.

    Idempotent and concurrency-safe: a repeated key (sequential or concurrent)
    returns the existing attempt; a repeated key with a different intent raises
    ``IdempotencyConflict``; an unregistered provider is rejected.
    """
    _validate_provider(provider)
    if expected_total_cents < 0:
        raise PaymentAttemptError("expected_total_cents cannot be negative.")

    fingerprint = intent_fingerprint(
        provider=provider, order_id=order_id, seat_id=seat_id, staff_id=staff_id,
        currency=currency, expected_total_cents=expected_total_cents,
        subtotal_cents=subtotal_cents, tax_cents=tax_cents, tip_cents=tip_cents,
        service_charge_cents=service_charge_cents, discount_cents=discount_cents,
        surcharge_cents=surcharge_cents,
    )

    if idempotency_key:
        existing = _by_key(db, idempotency_key)
        if existing is not None:
            _assert_same_intent(existing, fingerprint)
            return existing
    else:
        idempotency_key = new_idempotency_key()

    attempt = PaymentAttempt(
        order_id=order_id, seat_id=seat_id, staff_id=staff_id, provider=provider,
        idempotency_key=idempotency_key, intent_fingerprint=fingerprint,
        subtotal_cents=subtotal_cents, tax_cents=tax_cents, tip_cents=tip_cents,
        service_charge_cents=service_charge_cents, discount_cents=discount_cents,
        surcharge_cents=surcharge_cents, expected_total_cents=expected_total_cents,
        currency=currency, status=PaymentAttemptStatus.CREATED,
    )
    db.add(attempt)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent request inserted the same idempotency key first. Re-read
        # and return that attempt instead of surfacing the uniqueness violation.
        db.rollback()
        existing = _by_key(db, idempotency_key)
        if existing is None:
            raise
        _assert_same_intent(existing, fingerprint)
        return existing
    db.refresh(attempt)
    return attempt


def transition(
    db: Session,
    attempt: PaymentAttempt,
    new_status: str,
    *,
    provider_checkout_id: str | None = None,
    provider_payment_id: str | None = None,
    payment_id: int | None = None,
    processor_amount_cents: int | None = None,
    processor_currency: str | None = None,
    last_error: str | None = None,
    commit: bool = True,
    _from_resolver: bool = False,
) -> PaymentAttempt:
    """Concurrency-safe compare-and-swap transition.

    The move is a single UPDATE guarded on the expected current status, so a
    racing writer cannot also succeed — the loser gets ``TransitionConflict``.
    Provider identifiers and ``payment_id`` are write-once (guarded in the same
    UPDATE). Leaving REQUIRES_RECONCILIATION must go through
    ``resolve_reconciliation`` (finding #12).
    """
    if attempt.status == PaymentAttemptStatus.REQUIRES_RECONCILIATION and not _from_resolver:
        raise PaymentAttemptError(
            "resolve REQUIRES_RECONCILIATION via resolve_reconciliation(), not transition()."
        )
    allowed = PAYMENT_ATTEMPT_TRANSITIONS.get(attempt.status, set())
    if new_status not in allowed:
        raise PaymentAttemptError(
            f"illegal transition {attempt.status} -> {new_status} "
            f"(allowed: {sorted(allowed) or 'none — terminal state'})"
        )
    if new_status == PaymentAttemptStatus.SETTLED and payment_id is None:
        raise PaymentAttemptError("settling an attempt requires a payment_id.")

    expected = attempt.status
    values: dict = {"status": new_status, "updated_at": datetime.now()}
    if last_error is not None:
        values["last_error"] = last_error

    # All write-once: NULL accepts a first value, the same value is idempotent, a
    # different value fails the guarded UPDATE (rowcount 0) -> TransitionConflict.
    # Processor amount/currency are evidence and must never be overwritten (#8).
    conds = [PaymentAttempt.id == attempt.id, PaymentAttempt.status == expected]
    for field, val in (
        ("provider_checkout_id", provider_checkout_id),
        ("provider_payment_id", provider_payment_id),
        ("payment_id", payment_id),
        ("processor_amount_cents", processor_amount_cents),
        ("processor_currency", processor_currency),
    ):
        if val is not None:
            values[field] = val
            col = getattr(PaymentAttempt, field)
            conds.append(or_(col.is_(None), col == val))

    # A uniqueness violation (duplicate payment/provider id) or a lock/deadlock
    # under concurrency both mean this transition did not win — surface either as
    # an explicit TransitionConflict, never a leaked driver error.
    try:
        applied = db.execute(
            update(PaymentAttempt).where(and_(*conds)).values(**values)
        ).rowcount
        if applied == 1 and commit:
            db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise TransitionConflict(
            f"transition {expected} -> {new_status} conflicted "
            f"(uniqueness): {getattr(exc, 'orig', exc)}"
        ) from exc
    except OperationalError as exc:
        db.rollback()
        if is_lock_conflict(exc):
            raise TransitionConflict(
                f"transition {expected} -> {new_status} lost a lock/deadlock race: "
                f"{getattr(exc, 'orig', exc)}"
            ) from exc
        raise  # genuine infrastructure failure — propagate, do not mask as a conflict
    if applied != 1:
        db.rollback()
        try:
            fresh = db.get(PaymentAttempt, attempt.id)
            actual = fresh.status if fresh else "<deleted>"
        except Exception:  # noqa: BLE001 — never mask the conflict with a read error
            actual = "<unknown>"
        raise TransitionConflict(
            f"transition {expected} -> {new_status} did not apply "
            f"(current status is {actual!r}, or a write-once id differs)."
        )
    db.refresh(attempt)
    return attempt


def resolve_reconciliation(
    db: Session,
    attempt: PaymentAttempt,
    *,
    resolved_status: str,
    resolved_by: str,
    note: str,
    payment_id: int | None = None,
    provider_payment_id: str | None = None,
) -> PaymentAttempt:
    """Resolve an ambiguous attempt with recorded evidence (finding #12).

    Only valid from REQUIRES_RECONCILIATION. Requires a non-empty ``resolved_by``
    and ``note``; settling additionally requires a ``payment_id``. The evidence is
    persisted alongside the state change so the resolution is auditable, never a
    bare flip.
    """
    if attempt.status != PaymentAttemptStatus.REQUIRES_RECONCILIATION:
        raise PaymentAttemptError(
            "resolve_reconciliation only applies to a REQUIRES_RECONCILIATION attempt."
        )
    if resolved_status not in (
        PaymentAttemptStatus.SETTLED,
        PaymentAttemptStatus.FAILED,
        PaymentAttemptStatus.CANCELLED,
    ):
        raise PaymentAttemptError(f"cannot resolve reconciliation to {resolved_status!r}.")
    if not (resolved_by or "").strip() or not (note or "").strip():
        raise PaymentAttemptError("reconciliation resolution requires resolved_by and a note.")

    attempt.reconciled_at = datetime.now()
    attempt.reconciled_by = resolved_by.strip()[:60]
    attempt.reconciliation_note = note.strip()[:300]
    return transition(
        db, attempt, resolved_status,
        payment_id=payment_id, provider_payment_id=provider_payment_id,
        _from_resolver=True,
    )


def requires_reconciliation(db: Session) -> list[PaymentAttempt]:
    """Charge attempts a recovery worker/human must resolve — flagged, or
    approved by the processor but never settled locally (the 'processor charged,
    app lost it' case). Consumed by the Stage 2d recovery worker."""
    stuck = {
        PaymentAttemptStatus.REQUIRES_RECONCILIATION,
        PaymentAttemptStatus.PROCESSOR_APPROVED,
    }
    return list(
        db.execute(
            select(PaymentAttempt)
            .where(PaymentAttempt.status.in_(stuck))
            .order_by(PaymentAttempt.created_at)
        ).scalars()
    )
