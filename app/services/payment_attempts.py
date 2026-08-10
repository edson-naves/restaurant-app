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

from app.config import venue_currency
from app.models.oltp import (
    PAYMENT_ATTEMPT_TRANSITIONS,
    AuditEvent,
    PaymentAttempt,
    PaymentAttemptStatus,
    Role,
    Staff,
)

# Roles permitted to resolve an ambiguous payment by hand.
_RECON_ROLES = frozenset({Role.OWNER, Role.MANAGER})


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


# Current fingerprint algorithm version (v2 = selection-aware).
CURRENT_FP_VERSION = 2


def _strict_item_id(v) -> int:
    """A paid-item id is a positive integer. Reject bool/float/decimal-string/
    zero/negative/objects rather than silently coercing (slice-1-fix review #3).
    A digit-only string is accepted (form inputs arrive as strings)."""
    if isinstance(v, bool):
        raise PaymentAttemptError(f"invalid item id {v!r} (bool is not an id)")
    if isinstance(v, int):
        n = v
    elif isinstance(v, str) and v.strip().isdigit():
        n = int(v.strip())
    else:
        raise PaymentAttemptError(f"invalid item id {v!r} (expected a positive integer)")
    if n <= 0:
        raise PaymentAttemptError(f"invalid item id {n} (must be positive)")
    return n


def canonical_selection(item_ids) -> str:
    """Stable identity of the paid-item set: sorted, de-duplicated, comma-joined
    (Stage 2c). '' means a whole-order/amount-only intent. Each id is strictly
    validated (positive integer) — no lossy coercion (slice-1-fix review #3)."""
    return ",".join(str(i) for i in sorted({_strict_item_id(i) for i in (item_ids or [])}))


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
    line_selection: str = "",
    version: int = CURRENT_FP_VERSION,
) -> str:
    """Stable hash of the immutable intent behind an idempotency key. ``version``
    selects the algorithm: v1 (pre-Stage-2c) excludes the paid-item selection, v2
    includes it. A durable attempt is always re-matched using its stored version,
    so introducing v2 never turns a legacy v1 intent into a false conflict."""
    fields = [
        provider, order_id, seat_id, staff_id, currency.upper(),
        expected_total_cents, subtotal_cents, tax_cents, tip_cents,
        service_charge_cents, discount_cents, surcharge_cents,
    ]
    if version >= 2:
        fields.append(line_selection)
    canonical = "|".join(str(x) for x in fields)
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


def _valid_currency(cur: str | None) -> bool:
    return isinstance(cur, str) and len(cur) == 3 and cur.isalpha()


def _require_approval_evidence(
    attempt: PaymentAttempt, provider_payment_id: str | None,
    processor_amount_cents: int | None, processor_currency: str | None,
) -> None:
    """External providers cannot enter PROCESSOR_APPROVED without authoritative
    evidence — enforced at the state-machine boundary, not just in the Square
    adapter (finding #2). Manual/local providers approve instantly with no
    external evidence. Effective values combine what's already persisted with
    what this transition supplies."""
    from app.services.payment_providers import get_provider
    if not get_provider(attempt.provider).is_external:
        return
    eff_pay = provider_payment_id or attempt.provider_payment_id
    eff_amt = (processor_amount_cents if processor_amount_cents is not None
               else attempt.processor_amount_cents)
    eff_cur = processor_currency or attempt.processor_currency
    if not eff_pay or eff_amt is None or eff_amt < 0 or not _valid_currency(eff_cur):
        raise PaymentAttemptError(
            "an external provider cannot enter PROCESSOR_APPROVED without a provider "
            "payment id, a non-negative processor amount, and a valid processor currency (#2).")


def _by_key(db: Session, key: str) -> PaymentAttempt | None:
    return db.execute(
        select(PaymentAttempt).where(PaymentAttempt.idempotency_key == key)
    ).scalar_one_or_none()


def _assert_same_intent(existing: PaymentAttempt, intent: dict) -> None:
    """Re-match the requested intent against a stored attempt using the stored
    row's own fingerprint version (slice-1-fix #1) — so a v1 legacy row is compared
    with the v1 (selection-unaware) algorithm and a v2 row with v2."""
    expected = intent_fingerprint(**intent, version=existing.fingerprint_version)
    if existing.intent_fingerprint and existing.intent_fingerprint != expected:
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
    currency: str | None = None,
    item_ids=None,
    idempotency_key: str | None = None,
) -> PaymentAttempt:
    """Persist a CREATED attempt from an already-locked payable snapshot. Commits.

    ``currency`` defaults to the venue currency (``config.venue_currency()``) when
    omitted — never a hard-coded CAD — so a USD venue does not silently create a
    CAD intent (#3). ``item_ids`` (the paid selection) is canonicalized *here*, so
    the paid-item identity is owned by the attempt service, not the caller
    (slice-1 review #4). Idempotent and concurrency-safe: a repeated key returns
    the existing attempt; a repeated key with a different intent raises
    ``IdempotencyConflict``; an unregistered provider is rejected.
    """
    _validate_provider(provider)
    currency = (currency or venue_currency()).upper()
    line_selection = canonical_selection(item_ids)
    if expected_total_cents < 0:
        raise PaymentAttemptError("expected_total_cents cannot be negative.")

    intent = dict(
        provider=provider, order_id=order_id, seat_id=seat_id, staff_id=staff_id,
        currency=currency, expected_total_cents=expected_total_cents,
        subtotal_cents=subtotal_cents, tax_cents=tax_cents, tip_cents=tip_cents,
        service_charge_cents=service_charge_cents, discount_cents=discount_cents,
        surcharge_cents=surcharge_cents, line_selection=line_selection,
    )
    fingerprint = intent_fingerprint(**intent, version=CURRENT_FP_VERSION)

    if idempotency_key:
        existing = _by_key(db, idempotency_key)
        if existing is not None:
            _assert_same_intent(existing, intent)
            return existing
    else:
        idempotency_key = new_idempotency_key()

    attempt = PaymentAttempt(
        order_id=order_id, seat_id=seat_id, staff_id=staff_id, provider=provider,
        idempotency_key=idempotency_key, intent_fingerprint=fingerprint,
        fingerprint_version=CURRENT_FP_VERSION, line_selection=line_selection,
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
        _assert_same_intent(existing, intent)
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
    if new_status == PaymentAttemptStatus.PROCESSOR_APPROVED:
        _require_approval_evidence(attempt, provider_payment_id,
                                   processor_amount_cents, processor_currency)

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


class ReconciliationAuthorityError(PaymentAttemptError):
    """The caller lacks the authority/evidence to resolve an ambiguous attempt."""


def resolve_reconciliation(
    db: Session,
    attempt: PaymentAttempt,
    *,
    resolved_status: str,
    note: str,
    actor: Staff | None = None,
    automatic: bool = False,
    provider_evidence: str | None = None,
    payment_id: int | None = None,
    provider_payment_id: str | None = None,
) -> PaymentAttempt:
    """Resolve an ambiguous attempt under explicit authority, with an audit trail
    (findings #12, #13). The only exit from REQUIRES_RECONCILIATION.

    Two authorities, and nothing else settles an ambiguous payment:

    * **Manual** — ``actor`` must be a Staff with an OWNER/MANAGER role. The
      resolution records who, when, why, and (when present) provider evidence.
    * **Automatic** — ``automatic=True`` (a recovery worker) MUST supply
      ``provider_evidence`` (e.g. a processor lookup result / transaction id). A
      free-text note is never enough for automated settlement.

    Always writes an ``audit_event`` and persists the evidence on the attempt.
    """
    if attempt.status != PaymentAttemptStatus.REQUIRES_RECONCILIATION:
        raise PaymentAttemptError(
            "resolve_reconciliation only applies to a REQUIRES_RECONCILIATION attempt.")
    if resolved_status not in (
        PaymentAttemptStatus.SETTLED,
        PaymentAttemptStatus.FAILED,
        PaymentAttemptStatus.CANCELLED,
    ):
        raise PaymentAttemptError(f"cannot resolve reconciliation to {resolved_status!r}.")
    if not (note or "").strip():
        raise PaymentAttemptError("reconciliation resolution requires a note.")

    if automatic:
        if not (provider_evidence or "").strip():
            raise ReconciliationAuthorityError(
                "automatic reconciliation must supply provider_evidence — a note alone "
                "cannot settle an ambiguous payment (#13).")
        actor_id, resolved_by = None, "system:auto"
    else:
        if actor is None or actor.role not in _RECON_ROLES:
            raise ReconciliationAuthorityError(
                "manual reconciliation requires an OWNER/MANAGER actor (#13).")
        actor_id, resolved_by = actor.id, actor.name

    detail = f"attempt {attempt.id} -> {resolved_status}"
    if provider_evidence:
        detail += f" [evidence: {provider_evidence}]"
    detail = f"{detail}: {note.strip()}"[:300]
    db.add(AuditEvent(staff_id=actor_id, action="reconcile_payment_attempt",
                      detail=detail, order_id=attempt.order_id))

    attempt.reconciled_at = datetime.now()
    attempt.reconciled_by = resolved_by[:60]
    attempt.reconciliation_note = (
        (note.strip() + (f" | evidence: {provider_evidence}" if provider_evidence else ""))[:300])
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
