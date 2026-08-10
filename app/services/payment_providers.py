"""Pluggable payment providers (Stage 2b).

The goal: support *any* payment method across many different venues without the
order/settlement/refund core knowing anything provider-specific. The durable
``PaymentAttempt`` state machine (payment_attempts.py) is the neutral spine; each
real-world payment method is a small **adapter** implementing ``PaymentProvider``
and registered by a string key. ``PaymentInstrument.provider`` names which adapter
settles a given instrument.

Adding a brand-new machine/processor for a new client is therefore:

    1. write a class subclassing PaymentProvider (charge/poll/refund/cancel),
    2. call register(MyProvider()),
    3. set instrument.provider = "my_key".

No change to routers, settlement, refunds, or the state machine.

Two adapters ship here:

* ``ManualProvider`` — cash, e-transfer, keyed card, delivery-platform tender:
  no external processor, so a charge is instantly approved and a refund is a
  local ledger entry (staff hands the money back). This is the default.
* ``SquareTerminalProvider`` — the Square card terminal: asynchronous (create +
  poll) with a real Refunds API call.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models.oltp import PaymentAttemptStatus
from app.services import square


# --------------------------------------------------------------------------
# Result value objects — provider-neutral, expressed in the attempt's vocabulary
# --------------------------------------------------------------------------

@dataclass
class ChargeResult:
    """Outcome of asking a provider to charge. ``status`` is a
    ``PaymentAttemptStatus`` value so the caller feeds it straight into the state
    machine without provider-specific mapping."""
    status: str
    provider_checkout_id: str | None = None
    provider_payment_id: str | None = None
    tip_cents: int = 0
    card_brand: str | None = None
    card_last4: str | None = None
    error: str = ""


@dataclass
class RefundResult:
    ok: bool
    provider_refund_id: str | None = None
    # True when an external processor actually reversed funds; False for a
    # manual/local refund where the ledger entry *is* the reversal.
    external: bool = False
    pending: bool = False
    error: str = ""


# --------------------------------------------------------------------------
# The interface
# --------------------------------------------------------------------------

class PaymentProvider(ABC):
    key: str = ""            # unique; matches PaymentInstrument.provider
    label: str = ""
    is_external: bool = False   # does it call an outside processor?
    needs_polling: bool = False  # does charge() return PENDING to be polled?

    def is_configured(self) -> bool:
        """Whether this provider can be used right now (credentials present, …).
        Manual providers are always ready; external ones check their config."""
        return True

    @abstractmethod
    def charge(
        self,
        *,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        reference: str = "",
        note: str = "",
        tip_cents: int = 0,
    ) -> ChargeResult:
        """Initiate a charge for an already-persisted PaymentAttempt snapshot."""

    def poll(self, provider_checkout_id: str) -> ChargeResult:
        """Re-read an in-progress charge. Only meaningful when needs_polling."""
        raise NotImplementedError(f"{self.key} does not support polling")

    @abstractmethod
    def refund(
        self,
        *,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        provider_payment_id: str | None = None,
    ) -> RefundResult:
        """Reverse funds. External providers call their processor; manual ones
        record a local reversal."""

    def cancel(self, *, provider_checkout_id: str) -> None:
        """Cancel a still-pending (pre-capture) charge. Default: nothing to do."""
        return None


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------

class ManualProvider(PaymentProvider):
    """No external processor: staff attest the money moved. A charge is approved
    immediately; a refund is a local ledger reversal (the cash goes back by
    hand). Covers cash, e-transfer, keyed cards, and platform tenders."""
    key = "manual"
    label = "Manual / cash"
    is_external = False
    needs_polling = False

    def charge(self, *, amount_cents, currency, idempotency_key,
               reference="", note="", tip_cents=0) -> ChargeResult:
        return ChargeResult(status=PaymentAttemptStatus.PROCESSOR_APPROVED,
                            tip_cents=tip_cents)

    def refund(self, *, amount_cents, currency, idempotency_key,
               provider_payment_id=None) -> RefundResult:
        # Nothing to call — the local Refund ledger entry is the reversal.
        return RefundResult(ok=True, external=False)


class SquareTerminalProvider(PaymentProvider):
    """Square card terminal: asynchronous card-present charge with tip on the
    machine, plus a real Refunds API reversal."""
    key = "square_terminal"
    label = "Square terminal"
    is_external = True
    needs_polling = True

    def is_configured(self) -> bool:
        return square.is_configured()

    def charge(self, *, amount_cents, currency, idempotency_key,
               reference="", note="", tip_cents=0) -> ChargeResult:
        try:
            checkout = square.create_checkout(
                amount_cents, reference_id=reference, note=note,
            )
        except square.SquareError as exc:
            return ChargeResult(status=PaymentAttemptStatus.FAILED, error=str(exc))
        return ChargeResult(
            status=PaymentAttemptStatus.PROCESSOR_PENDING,
            provider_checkout_id=checkout.get("id"),
        )

    def poll(self, provider_checkout_id: str) -> ChargeResult:
        try:
            checkout = square.get_checkout(provider_checkout_id)
        except square.SquareError as exc:
            return ChargeResult(status=PaymentAttemptStatus.PROCESSOR_PENDING,
                                provider_checkout_id=provider_checkout_id,
                                error=str(exc))
        status = checkout.get("status")
        if status == square.COMPLETED:
            tip_cents, brand, last4 = square.tip_and_card(checkout)
            payment_ids = checkout.get("payment_ids") or []
            return ChargeResult(
                status=PaymentAttemptStatus.PROCESSOR_APPROVED,
                provider_checkout_id=provider_checkout_id,
                provider_payment_id=payment_ids[0] if payment_ids else None,
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
            return RefundResult(ok=False, external=True,
                                error="no Square payment id to refund against")
        try:
            refund = square.create_refund(
                provider_payment_id, amount_cents, idempotency_key,
            )
        except square.SquareError as exc:
            return RefundResult(ok=False, external=True, error=str(exc))
        status = refund.get("status")
        return RefundResult(
            ok=status in (square.REFUND_COMPLETED, square.REFUND_PENDING),
            provider_refund_id=refund.get("id"),
            external=True,
            pending=status == square.REFUND_PENDING,
            error="" if status != square.REFUND_REJECTED else "refund rejected",
        )

    def cancel(self, *, provider_checkout_id: str) -> None:
        try:
            square.cancel_checkout(provider_checkout_id)
        except square.SquareError:
            pass


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

class UnknownProvider(KeyError):
    """Raised when an instrument names a provider that isn't registered."""


_REGISTRY: dict[str, PaymentProvider] = {}


def register(provider: PaymentProvider) -> None:
    """Register (or replace) an adapter by its key. Call at import time."""
    if not provider.key:
        raise ValueError("payment provider must define a non-empty key")
    _REGISTRY[provider.key] = provider


def get_provider(key: str) -> PaymentProvider:
    """Resolve an adapter by key. Falls back to 'manual' semantics only via an
    explicit key — an unknown key is an error, never a silent guess."""
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise UnknownProvider(
            f"no payment provider registered for {key!r} "
            f"(known: {sorted(_REGISTRY)})"
        ) from exc


def available() -> list[PaymentProvider]:
    """All registered providers (for admin UIs / diagnostics)."""
    return list(_REGISTRY.values())


# Ship with the two reference adapters registered.
register(ManualProvider())
register(SquareTerminalProvider())
