"""Square Terminal API — card-present payments (section 4.2, terminal path).

The physical card terminal, not our app, captures the card and prompts the
customer for a tip, then hands the result back. We drive it over Square's
Terminal API:

    create_checkout(amount)  -> terminal wakes, customer taps + tips
    get_checkout(id)         -> poll until COMPLETED / CANCELED
    get_payment(payment_id)  -> read the card brand + last-4

This keeps card data on the terminal (out of our PCI scope — we only ever store
the last-4 and an auth reference) and makes the tip the customer's real choice
on the machine, not a number a waiter types.

Configuration comes from the environment (set locally in .env, on Render in the
dashboard). With nothing configured, is_configured() is False and the app keeps
using manual payment entry — so the terminal is strictly additive.
"""
from __future__ import annotations

import os
import time
import uuid

import httpx

# Pinned so a Square-side default change can't silently alter request/response
# shapes under us. Bump deliberately when adopting new fields.
_API_VERSION = "2026-07-15"
_TIMEOUT = httpx.Timeout(20.0)

# Terminal checkout lifecycle (Square). PENDING/IN_PROGRESS are still on the
# machine; the rest are terminal (done, one way or another).
PENDING = "PENDING"
IN_PROGRESS = "IN_PROGRESS"
CANCEL_REQUESTED = "CANCEL_REQUESTED"
CANCELED = "CANCELED"
COMPLETED = "COMPLETED"
_DONE = {COMPLETED, CANCELED}


class SquareError(Exception):
    """Any non-2xx from Square, or a checkout that ended without completing."""


class SquareTransportError(SquareError):
    """The request may or may not have reached Square — a timeout, a dropped
    connection, or a 5xx/throttle. The processor outcome is UNKNOWN, so the caller
    must reconcile rather than assume the operation did not happen."""


class SquareApiError(SquareError):
    """Square returned a definitive error response (a 4xx). ``status_code``,
    ``code`` (Square error code) and ``error_category`` let the caller classify a
    genuine decline vs an auth/config problem, a request conflict, or a bad
    device/location — see ``classify_charge_error``."""

    def __init__(self, message: str, status_code: int,
                 code: str | None = None, error_category: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.error_category = error_category


# HTTP statuses whose outcome is ambiguous/retryable rather than a definitive
# rejection (server errors + throttling + request timeout).
_TRANSIENT_STATUS = frozenset({408, 425, 429})

# Normalized create-charge error categories (finding #8). Only DECLINE is a
# definitive "no charge, financial rejection"; everything else must NOT be
# treated as safe-to-retry — the request may have conflicted, or config is broken.
CHARGE_DECLINE = "decline"
CHARGE_CONFLICT = "conflict"          # idempotency/request conflict — charge may exist
CHARGE_AUTH_CONFIG = "auth_config"    # auth/permission/config failure
CHARGE_INVALID_TARGET = "invalid_target"  # bad device/location/not-found
CHARGE_UNEXPECTED = "unexpected"

_DECLINE_CODES = frozenset({
    "CARD_DECLINED", "GENERIC_DECLINE", "INSUFFICIENT_FUNDS", "CVV_FAILURE",
    "ADDRESS_VERIFICATION_FAILURE", "CARD_EXPIRED", "INVALID_CARD",
    "CARD_DECLINED_VERIFICATION_REQUIRED", "PAN_FAILURE",
})


def classify_charge_error(exc: SquareApiError) -> str:
    """Map a definitive Square 4xx to a normalized charge-error category so the
    core never infers a new charge is safe merely because the status was 4xx."""
    code = (exc.code or "").upper()
    cat = (exc.error_category or "").upper()
    sc = exc.status_code
    if sc == 409 or code in ("IDEMPOTENCY_KEY_REUSED", "CONFLICT"):
        return CHARGE_CONFLICT
    if cat == "PAYMENT_METHOD_ERROR" or sc == 402 or code in _DECLINE_CODES:
        return CHARGE_DECLINE
    if cat == "AUTHENTICATION_ERROR" or sc in (401, 403) or code in ("UNAUTHORIZED", "FORBIDDEN"):
        return CHARGE_AUTH_CONFIG
    if sc == 404 or code in ("NOT_FOUND", "DEVICE_UNAVAILABLE", "INVALID_LOCATION"):
        return CHARGE_INVALID_TARGET
    return CHARGE_UNEXPECTED


# --------------------------------------------------------------------------
# Configuration (read at call time so env changes need no restart of imports)
# --------------------------------------------------------------------------

def _env() -> str:
    return (os.environ.get("SQUARE_ENV") or "sandbox").strip().lower()


def _base_url() -> str:
    return (
        "https://connect.squareup.com"
        if _env() == "production"
        else "https://connect.squareupsandbox.com"
    )


def access_token() -> str:
    return (os.environ.get("SQUARE_ACCESS_TOKEN") or "").strip()


def location_id() -> str:
    return (os.environ.get("SQUARE_LOCATION_ID") or "").strip()


def currency() -> str:
    return (os.environ.get("SQUARE_CURRENCY") or "CAD").strip().upper()


def default_device_id() -> str:
    """The terminal to send checkouts to. In Sandbox this is one of Square's
    magic simulator device IDs; in production it's your paired Square Terminal."""
    return (os.environ.get("SQUARE_DEVICE_ID") or "").strip()


def is_configured() -> bool:
    """True when we have enough to talk to a terminal. Gates the whole feature."""
    return bool(access_token() and location_id() and default_device_id())


def is_sandbox() -> bool:
    return _env() != "production"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    return {
        "Square-Version": _API_VERSION,
        "Authorization": f"Bearer {access_token()}",
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{_base_url()}{path}"
    try:
        resp = httpx.request(method, url, headers=_headers(), json=payload, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        # Never reached a response — the request may still have been processed.
        raise SquareTransportError(f"Could not reach the card terminal service: {exc}") from exc
    code = resp.status_code
    if code // 100 == 2:
        return resp.json()
    if code >= 500 or code in _TRANSIENT_STATUS:
        # Server-side/throttle: the operation may have taken effect. Ambiguous.
        raise SquareTransportError(_error_message(resp))
    # A definitive 4xx: capture the Square error code/category for classification.
    err = {}
    try:
        errs = resp.json().get("errors") or []
        err = errs[0] if errs else {}
    except Exception:  # noqa: BLE001
        pass
    raise SquareApiError(_error_message(resp), status_code=code,
                         code=err.get("code"), error_category=err.get("category"))


def _error_message(resp: httpx.Response) -> str:
    try:
        errs = resp.json().get("errors") or []
        if errs:
            e = errs[0]
            return f"Square error ({resp.status_code}): {e.get('detail') or e.get('code')}"
    except Exception:
        pass
    return f"Square error ({resp.status_code})."


# --------------------------------------------------------------------------
# Terminal checkouts
# --------------------------------------------------------------------------

def create_checkout(
    amount_cents: int,
    reference_id: str = "",
    note: str = "",
    allow_tip: bool = True,
    device_id: str | None = None,
    idempotency_key: str | None = None,
    currency_code: str | None = None,
) -> dict:
    """Send an amount to the terminal for the customer to pay + tip on.

    amount_cents is the pre-tip total to charge (items + tax + any service
    charge). The terminal adds the tip on top. Returns the created checkout
    (status PENDING); poll get_checkout / wait_for_checkout for the result.

    ``idempotency_key`` is the caller's durable key (the PaymentAttempt's), so a
    retry after a crash reaches Square with the *same* key and cannot double
    charge. Only when no key is supplied do we mint one (never for a real
    attempt-backed charge).
    """
    # tip_settings lives inside device_options (DeviceCheckoutOptions) — that's
    # what drives the tip prompt shown to the customer on the terminal.
    device_options: dict = {"device_id": device_id or default_device_id()}
    if allow_tip:
        device_options["tip_settings"] = {
            "allow_tipping": True,
            "tip_percentages": [15, 18, 20],
            "custom_tip_field": True,
        }
    else:
        device_options["tip_settings"] = {"allow_tipping": False}
    body = {
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "checkout": {
            "amount_money": {"amount": int(amount_cents),
                             "currency": (currency_code or currency()).upper()},
            "device_options": device_options,
            "deadline_duration": "PT5M",
        },
    }
    checkout = body["checkout"]
    if reference_id:
        checkout["reference_id"] = reference_id[:40]
    if note:
        checkout["note"] = note[:500]
    return _request("POST", "/v2/terminals/checkouts", body)["checkout"]


def get_checkout(checkout_id: str) -> dict:
    """Fetch a terminal checkout's current state."""
    return _request("GET", f"/v2/terminals/checkouts/{checkout_id}")["checkout"]


def cancel_checkout(checkout_id: str) -> dict:
    """Ask the terminal to cancel a still-pending checkout."""
    return _request("POST", f"/v2/terminals/checkouts/{checkout_id}/cancel")["checkout"]


def get_payment(payment_id: str) -> dict:
    """Fetch a completed Square payment (for card brand + last-4)."""
    return _request("GET", f"/v2/payments/{payment_id}")["payment"]


# Refund lifecycle (Square). COMPLETED means the money is on its way back.
REFUND_PENDING = "PENDING"
REFUND_COMPLETED = "COMPLETED"
REFUND_REJECTED = "REJECTED"
REFUND_FAILED = "FAILED"


def create_refund(
    payment_id: str,
    amount_cents: int,
    idempotency_key: str,
    reason: str = "",
    currency_code: str | None = None,
) -> dict:
    """Reverse money to the card via the Square Refunds API (RefundPayment).

    ``idempotency_key`` makes the call safe to retry: Square returns the same
    refund for a repeated key instead of refunding twice. Returns the refund dict
    (``id``, ``status`` — PENDING/COMPLETED/REJECTED/FAILED). Raises SquareError
    on transport/API failure so the caller can flag the attempt for reconciliation
    rather than marking it refunded.
    """
    body = {
        "idempotency_key": idempotency_key,
        "payment_id": payment_id,
        "amount_money": {"amount": int(amount_cents),
                         "currency": (currency_code or currency()).upper()},
    }
    if reason:
        body["reason"] = reason[:192]
    return _request("POST", "/v2/refunds", body)["refund"]


def wait_for_checkout(checkout_id: str, timeout_s: float = 120.0, interval_s: float = 1.5) -> dict:
    """Poll until the checkout completes, cancels, or we give up.

    Production really wants webhooks (there can be a long gap while the guest
    fiddles with the machine), but polling is simple, works identically in
    Sandbox, and is fine while a waiter is standing at the terminal. Returns the
    final checkout dict; raises SquareError only on transport/API failure.
    """
    deadline = time.monotonic() + timeout_s
    checkout = get_checkout(checkout_id)
    while checkout.get("status") not in _DONE and time.monotonic() < deadline:
        time.sleep(interval_s)
        checkout = get_checkout(checkout_id)
    return checkout


def completed_payment_evidence(checkout: dict) -> dict:
    """Authoritative amount/currency/tip evidence for a COMPLETED checkout, read
    from the underlying Square Payment (not local config — finding #6).

    Returns a dict with, when the payment is readable:
      captured_total_cents  — what Square captured (base + tip)
      tip_cents             — the tip the guest added on the terminal
      base_cents            — captured_total - tip (comparable to our pre-tip
                              expected_total_cents — finding #17)
      currency              — the processor's currency (e.g. 'CAD')
      brand, last4          — card details
    Missing/unreadable fields are None so settlement never compares against
    fabricated evidence.
    """
    ev = {"captured_total_cents": None, "tip_cents": 0, "base_cents": None,
          "currency": None, "brand": None, "last4": None}
    payment_ids = checkout.get("payment_ids") or []
    if not payment_ids:
        return ev
    try:
        pay = get_payment(payment_ids[0])
    except SquareError:
        return ev  # unreadable — leave evidence None, do not guess
    total_money = pay.get("total_money") or {}
    tip = int((pay.get("tip_money") or {}).get("amount") or 0)
    total = total_money.get("amount")
    total = int(total) if total is not None else None
    card = (pay.get("card_details") or {}).get("card") or {}
    ev.update(
        captured_total_cents=total, tip_cents=tip,
        base_cents=(total - tip) if total is not None else None,
        currency=total_money.get("currency") or (pay.get("amount_money") or {}).get("currency"),
        brand=card.get("card_brand"), last4=card.get("last_4"),
    )
    return ev


def tip_and_card(checkout: dict) -> tuple[int, str | None, str | None]:
    """From a COMPLETED checkout, return (tip_cents, card_brand, card_last4).

    The authoritative tip and the card details both live on the underlying
    Payment (the checkout's own tip_money can be absent), so we look it up. Any
    hiccup degrades gracefully — a tip we can still read off the checkout, and
    no card detail — rather than failing a payment we already collected.
    """
    brand: str | None = None
    last4: str | None = None
    tip_cents = int((checkout.get("tip_money") or {}).get("amount") or 0)
    payment_ids = checkout.get("payment_ids") or []
    if payment_ids:
        try:
            pay = get_payment(payment_ids[0])
            tip_cents = int((pay.get("tip_money") or {}).get("amount") or tip_cents)
            card = (pay.get("card_details") or {}).get("card") or {}
            brand = card.get("card_brand")
            last4 = card.get("last_4")
        except SquareError:
            pass
    return tip_cents, brand, last4
