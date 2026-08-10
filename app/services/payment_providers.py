"""Pluggable payment providers (hardened per review).

The durable ``PaymentAttempt``/``RefundAttempt`` state machines are the neutral
spine; each real-world payment method is a small adapter implementing
``PaymentProvider`` and registered by a string key. ``PaymentInstrument.provider``
names which adapter settles an instrument.

Adding a charge / poll / refund / cancel provider is additive:

    1. subclass PaymentProvider (implement the methods for the capabilities you
       advertise — register() rejects a provider that claims more than it backs),
    2. register(MyProvider()),
    3. set instrument.provider = "my_key".

Scope, honestly stated: the auth/capture, webhook, and lookup contracts are
*defined and registration-validated* but not yet consumed by settlement, and the
providers are not yet wired into the live charge/refund routes — that is Stage 2c.
So this is "plug in a new charge/refund/polling provider without touching the
state machines", not yet "plug in any processor shape with zero core work".

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


# A capability is only advertisable if its backing method is actually implemented
# (finding #10). register() validates this — a provider may not claim behavior it
# does not provide. REFUND/PARTIAL_REFUND share the abstract refund() (always
# implemented on a concrete provider), so they need no override check here.
_CAPABILITY_METHOD = {
    Capability.POLLING: "poll",
    Capability.LOOKUP: "lookup",
    Capability.AUTHORIZE: "authorize",
    Capability.CAPTURE: "capture",
    Capability.PARTIAL_CAPTURE: "capture",
    Capability.WEBHOOKS: "handle_webhook",
}


# --------------------------------------------------------------------------
# Result value objects — provider-neutral, in the attempts' vocabulary
# --------------------------------------------------------------------------

@dataclass
class ChargeResult:
    """Outcome of asking a provider to charge. ``status`` is a
    ``PaymentAttemptStatus`` value fed straight into the state machine.

    Amount/tip semantics (finding #17), so settlement compares like with like:
      * the attempt's ``expected_total_cents`` is the **pre-tip** amount we asked
        the terminal to charge (items + tax + service charge + surcharge - discount);
      * the guest adds a tip on the terminal, so the processor captures
        base + tip;
      * ``processor_amount_cents`` here is the processor's **pre-tip base**
        (captured total - tip), directly comparable to ``expected_total_cents``;
      * ``tip_cents`` is the processor-confirmed tip.
    ``processor_amount_cents``/``processor_currency`` are read from authoritative
    processor evidence, never local config, and are None when unreadable so
    settlement never verifies against a fabricated value (findings #6/#8).
    """
    status: str
    provider_checkout_id: str | None = None
    provider_payment_id: str | None = None
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

    # Optional, capability-gated methods. A provider that advertises the matching
    # capability MUST override the method; register() enforces it (#10). The base
    # versions exist only so the contract is discoverable and the override check
    # has something to compare against.
    def lookup(self, *, provider_payment_id: str | None = None,
               provider_checkout_id: str | None = None) -> dict:
        raise NotImplementedError(f"{self.key} does not support LOOKUP")

    def authorize(self, *, amount_cents: int, currency: str, idempotency_key: str) -> ChargeResult:
        raise NotImplementedError(f"{self.key} does not support AUTHORIZE")

    def capture(self, *, provider_payment_id: str, amount_cents: int | None = None) -> ChargeResult:
        raise NotImplementedError(f"{self.key} does not support CAPTURE")

    def handle_webhook(self, payload: dict) -> dict:
        raise NotImplementedError(f"{self.key} does not support WEBHOOKS")


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
    # Only what the adapter actually backs with a method. Square Terminal is
    # immediate-capture, so no AUTHORIZE/CAPTURE split is advertised (#10/#19).
    capabilities = frozenset({
        Capability.POLLING, Capability.REFUND,
        Capability.PARTIAL_REFUND, Capability.LOOKUP,
    })

    def is_configured(self) -> bool:
        return square.is_configured()

    def lookup(self, *, provider_payment_id=None, provider_checkout_id=None) -> dict:
        """Provider-side reconciliation read: fetch the authoritative Payment or
        terminal checkout so a recovery worker can resolve an ambiguous attempt."""
        if provider_payment_id:
            return square.get_payment(provider_payment_id)
        if provider_checkout_id:
            return square.get_checkout(provider_checkout_id)
        raise ValueError("lookup needs a provider payment id or checkout id")

    def charge(self, *, amount_cents, currency, idempotency_key,
               reference="", note="", tip_cents=0) -> ChargeResult:
        try:
            checkout = square.create_checkout(
                amount_cents, reference_id=reference, note=note,
                idempotency_key=idempotency_key,   # finding #1: forward the key
                currency_code=currency,            # finding #5: per-operation currency
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
            payment_ids = checkout.get("payment_ids") or []
            if not payment_ids:
                # Completed but no authoritative payment id — cannot reconcile,
                # refund, or link. Do NOT treat as ordinary approval (finding #9).
                return ChargeResult(
                    status=PaymentAttemptStatus.REQUIRES_RECONCILIATION,
                    provider_checkout_id=provider_checkout_id,
                    error="Square COMPLETED without a payment id",
                )
            # Authoritative amount/currency from the Payment object, not local
            # config (findings #6/#17). processor_amount_cents is the PRE-TIP base
            # so it compares to our pre-tip expected_total_cents.
            ev = square.completed_payment_evidence(checkout)
            if ev["base_cents"] is None or not ev["currency"]:
                # COMPLETED but we could not read authoritative amount/currency (the
                # payment lookup failed or was incomplete). Do NOT approve on partial
                # evidence — hand it to reconciliation (finding #3).
                return ChargeResult(
                    status=PaymentAttemptStatus.REQUIRES_RECONCILIATION,
                    provider_checkout_id=provider_checkout_id,
                    provider_payment_id=payment_ids[0],
                    error="COMPLETED but processor amount/currency evidence is incomplete",
                )
            return ChargeResult(
                status=PaymentAttemptStatus.PROCESSOR_APPROVED,
                provider_checkout_id=provider_checkout_id,
                provider_payment_id=payment_ids[0],
                processor_amount_cents=ev["base_cents"],
                processor_currency=ev["currency"],
                tip_cents=ev["tip_cents"], card_brand=ev["brand"], card_last4=ev["last4"],
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
            refund = square.create_refund(provider_payment_id, amount_cents,
                                          idempotency_key, currency_code=currency)
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


def _validate_capabilities(provider: PaymentProvider) -> None:
    """A provider may not advertise a capability it does not implement (#10)."""
    for cap in provider.capabilities:
        method_name = _CAPABILITY_METHOD.get(cap)
        if method_name is None:
            continue  # REFUND/PARTIAL_REFUND ride the abstract refund()
        impl = getattr(type(provider), method_name, None)
        base = getattr(PaymentProvider, method_name, None)
        if impl is None or impl is base:
            raise ValueError(
                f"provider {provider.key!r} advertises capability {cap!r} but does not "
                f"implement {method_name}()")


def register(provider: PaymentProvider, *, override: bool = False) -> None:
    if not provider.key:
        raise ValueError("payment provider must define a non-empty key")
    if provider.key in _REGISTRY and not override:
        raise ValueError(
            f"a payment provider is already registered for {provider.key!r}; "
            f"pass override=True to replace it deliberately (#18)")
    _validate_capabilities(provider)
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
