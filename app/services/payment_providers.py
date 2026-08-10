"""Pluggable payment providers (hardened per review).

The durable ``PaymentAttempt``/``RefundAttempt`` state machines are the neutral
spine; each real-world payment method is a small adapter implementing
``PaymentProvider`` and registered by a string key. ``PaymentInstrument.provider``
names which adapter settles an instrument.

Adding a brand-new machine/processor for a new client is:

    1. subclass PaymentProvider (charge/poll/refund/cancel + a capabilities set),
    2. register(MyProvider()),
    3. set instrument.provider = "my_key".

No change to routers, settlement, refunds, or the state machines.

Capabilities (finding #7) let the settlement core ask what a provider supports
(polling, webhooks, auth/capture split, partial capture/refund, lookup) instead
of assuming Square-terminal shape. Results carry the processor-confirmed amount
and currency (finding #8) so settlement can verify or reconcile against the local
snapshot, and every provider outcome maps to an explicit
``PaymentAttemptStatus``/``RefundAttemptStatus`` — including the ambiguous cases
(finding #9/#10/#11).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.models.oltp import PaymentAttemptStatus, RefundAttemptStatus
from app.services import square


# --------------------------------------------------------------------------
# Capability vocabulary (finding #7)
# --------------------------------------------------------------------------

class Capability:
    POLLING = "polling"
    WEBHOOKS = "webhooks"
    AUTHORIZE = "authorize"          # auth without capture
    CAPTURE = "capture"              # later capture of an auth
    PARTIAL_CAPTURE = "partial_capture"
    REFUND = "refund"
    PARTIAL_REFUND = "partial_refund"
    LOOKUP = "lookup"                # provider-side reconciliation lookup


# --------------------------------------------------------------------------
# Result value objects — provider-neutral, in the attempts' vocabulary
# --------------------------------------------------------------------------

@dataclass
class ChargeResult:
    """Outcome of asking a provider to charge. ``status`` is a
    ``PaymentAttemptStatus`` value fed straight into the state machine."""
    status: str
    provider_checkout_id: str | None = None
    provider_payment_id: str | None = None
    # Processor-confirmed amount/currency (finding #8) for settlement to verify.
    processor_amount_cents: int | None = None
    processor_currency: str | None = None
    tip_cents: int = 0
    card_brand: str | None = None
    card_last4: str | None = None
    error: str = ""


@dataclass
class RefundResult:
    status: str                       # a RefundAttemptStatus value
    provider_refund_id: str | None = None
    external: bool = False            # did an external processor reverse funds?
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (RefundAttemptStatus.COMPLETED,
                               RefundAttemptStatus.PROCESSOR_PENDING)


@dataclass
class CancelResult:
    """Outcome of a pre-capture cancel (finding #10). Never silently swallowed:
    an ambiguous/failed cancel flags for reconciliation instead of assuming
    success."""
    ok: bool
    provider_status: str = ""
    requires_reconciliation: bool = False
    error: str = ""


# --------------------------------------------------------------------------
# The interface
# --------------------------------------------------------------------------

class PaymentProvider(ABC):
    key: str = ""
    label: str = ""
    is_external: bool = False
    capabilities: frozenset[str] = frozenset()

    @property
    def needs_polling(self) -> bool:
        return Capability.POLLING in self.capabilities

    def is_configured(self) -> bool:
        return True

    @abstractmethod
    def charge(self, *, amount_cents: int, currency: str, idempotency_key: str,
               reference: str = "", note: str = "", tip_cents: int = 0) -> ChargeResult:
        ...

    def poll(self, provider_checkout_id: str) -> ChargeResult:
        raise NotImplementedError(f"{self.key} does not support polling")

    @abstractmethod
    def refund(self, *, amount_cents: int, currency: str, idempotency_key: str,
               provider_payment_id: str | None = None) -> RefundResult:
        ...

    def cancel(self, *, provider_checkout_id: str) -> CancelResult:
        return CancelResult(ok=True)


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------

class ManualProvider(PaymentProvider):
    """No external processor: staff attest the money moved. Charge is approved
    immediately; refund is a local ledger reversal. Covers cash, e-transfer,
    keyed cards, and platform tenders."""
    key = "manual"
    label = "Manual / cash"
    is_external = False
    capabilities = frozenset({Capability.REFUND, Capability.PARTIAL_REFUND})

    def charge(self, *, amount_cents, currency, idempotency_key,
               reference="", note="", tip_cents=0) -> ChargeResult:
        return ChargeResult(
            status=PaymentAttemptStatus.PROCESSOR_APPROVED,
            processor_amount_cents=amount_cents, processor_currency=currency,
            tip_cents=tip_cents,
        )

    def refund(self, *, amount_cents, currency, idempotency_key,
               provider_payment_id=None) -> RefundResult:
        # Nothing to call — the local Refund ledger entry is the reversal.
        return RefundResult(status=RefundAttemptStatus.COMPLETED, external=False)


class SquareTerminalProvider(PaymentProvider):
    """Square card terminal: asynchronous card-present charge with tip on the
    machine, plus a real Refunds API reversal."""
    key = "square_terminal"
    label = "Square terminal"
    is_external = True
    capabilities = frozenset({
        Capability.POLLING, Capability.CAPTURE, Capability.REFUND,
        Capability.PARTIAL_REFUND, Capability.LOOKUP,
    })

    def is_configured(self) -> bool:
        return square.is_configured()

    def charge(self, *, amount_cents, currency, idempotency_key,
               reference="", note="", tip_cents=0) -> ChargeResult:
        try:
            checkout = square.create_checkout(
                amount_cents, reference_id=reference, note=note,
                idempotency_key=idempotency_key,   # finding #1: forward the key
            )
        except square.SquareApiError as exc:
            # A definitive 4xx: Square rejected the request. No charge exists.
            return ChargeResult(status=PaymentAttemptStatus.FAILED, error=str(exc))
        except square.SquareError as exc:
            # Transport/unknown (timeout, dropped connection, 5xx). Square may have
            # accepted the checkout — must reconcile, never assume FAILED (#4).
            return ChargeResult(status=PaymentAttemptStatus.REQUIRES_RECONCILIATION,
                                error=f"unknown submission outcome: {exc}")
        return ChargeResult(
            status=PaymentAttemptStatus.PROCESSOR_PENDING,
            provider_checkout_id=checkout.get("id"),
        )

    def poll(self, provider_checkout_id: str) -> ChargeResult:
        try:
            checkout = square.get_checkout(provider_checkout_id)
        except square.SquareTransportError as exc:
            # Transient — keep polling.
            return ChargeResult(status=PaymentAttemptStatus.PROCESSOR_PENDING,
                                provider_checkout_id=provider_checkout_id,
                                error=f"transient lookup error: {exc}")
        except square.SquareError as exc:
            # Definitive lookup error (auth/config/not-found). Do not stay PENDING
            # forever — hand it to reconciliation (#9).
            return ChargeResult(status=PaymentAttemptStatus.REQUIRES_RECONCILIATION,
                                provider_checkout_id=provider_checkout_id,
                                error=f"definitive lookup error: {exc}")
        status = checkout.get("status")
        if status == square.COMPLETED:
            tip_cents, brand, last4 = square.tip_and_card(checkout)
            payment_ids = checkout.get("payment_ids") or []
            if not payment_ids:
                # Completed but no authoritative payment id — cannot reconcile,
                # refund, or link. Do NOT treat as ordinary approval (finding #9).
                return ChargeResult(
                    status=PaymentAttemptStatus.REQUIRES_RECONCILIATION,
                    provider_checkout_id=provider_checkout_id,
                    error="Square COMPLETED without a payment id",
                )
            amount = _completed_amount(checkout)
            return ChargeResult(
                status=PaymentAttemptStatus.PROCESSOR_APPROVED,
                provider_checkout_id=provider_checkout_id,
                provider_payment_id=payment_ids[0],
                processor_amount_cents=amount,
                processor_currency=(square.currency()),
                tip_cents=tip_cents, card_brand=brand, card_last4=last4,
            )
        if status == square.CANCELED:
            return ChargeResult(status=PaymentAttemptStatus.CANCELLED,
                                provider_checkout_id=provider_checkout_id)
        return ChargeResult(status=PaymentAttemptStatus.PROCESSOR_PENDING,
                            provider_checkout_id=provider_checkout_id)

    def refund(self, *, amount_cents, currency, idempotency_key,
               provider_payment_id=None) -> RefundResult:
        if not provider_payment_id:
            return RefundResult(status=RefundAttemptStatus.REQUIRES_RECONCILIATION,
                                external=True,
                                error="no Square payment id to refund against")
        try:
            refund = square.create_refund(provider_payment_id, amount_cents, idempotency_key)
        except square.SquareError as exc:
            # Transport/API failure: unknown processor outcome → reconcile.
            return RefundResult(status=RefundAttemptStatus.REQUIRES_RECONCILIATION,
                                external=True, error=str(exc))
        # Explicit mapping of every known Square refund state (finding #11).
        return _map_square_refund(refund)

    def cancel(self, *, provider_checkout_id: str) -> CancelResult:
        try:
            checkout = square.cancel_checkout(provider_checkout_id)
        except square.SquareError as exc:
            # Do not swallow: an ambiguous cancel must be reconciled (finding #10).
            return CancelResult(ok=False, requires_reconciliation=True, error=str(exc))
        # A 2xx does not by itself mean cancellation succeeded — validate the
        # returned checkout status explicitly (finding #11).
        status = checkout.get("status") or ""
        if status == square.CANCELED:
            return CancelResult(ok=True, provider_status=status)
        if status == square.COMPLETED:
            return CancelResult(ok=False, provider_status=status, requires_reconciliation=True,
                                error="checkout already COMPLETED — a payment likely exists")
        if status in (square.PENDING, square.IN_PROGRESS, square.CANCEL_REQUESTED):
            return CancelResult(ok=False, provider_status=status, requires_reconciliation=True,
                                error="cancellation not yet authoritative")
        return CancelResult(ok=False, provider_status=status, requires_reconciliation=True,
                            error=f"unexpected cancel status {status!r}")


def _completed_amount(checkout: dict) -> int | None:
    money = checkout.get("amount_money") or {}
    amt = money.get("amount")
    return int(amt) if amt is not None else None


_SQUARE_REFUND_MAP = {
    square.REFUND_COMPLETED: (RefundAttemptStatus.COMPLETED, ""),
    square.REFUND_PENDING: (RefundAttemptStatus.PROCESSOR_PENDING, ""),
    square.REFUND_REJECTED: (RefundAttemptStatus.REJECTED, "Square rejected the refund"),
    square.REFUND_FAILED: (RefundAttemptStatus.FAILED, "Square refund failed"),
}


def _map_square_refund(refund: dict) -> RefundResult:
    status = refund.get("status")
    mapped, err = _SQUARE_REFUND_MAP.get(
        status, (RefundAttemptStatus.REQUIRES_RECONCILIATION, f"unknown Square refund status {status!r}")
    )
    return RefundResult(status=mapped, provider_refund_id=refund.get("id"),
                        external=True, error=err)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

class UnknownProvider(KeyError):
    """Raised when an instrument names a provider that isn't registered."""


_REGISTRY: dict[str, PaymentProvider] = {}


def register(provider: PaymentProvider) -> None:
    if not provider.key:
        raise ValueError("payment provider must define a non-empty key")
    _REGISTRY[provider.key] = provider


def get_provider(key: str) -> PaymentProvider:
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise UnknownProvider(
            f"no payment provider registered for {key!r} (known: {sorted(_REGISTRY)})"
        ) from exc


def available() -> list[PaymentProvider]:
    return list(_REGISTRY.values())


register(ManualProvider())
register(SquareTerminalProvider())
