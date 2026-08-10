"""Stage 2b regression tests — pluggable payment providers.

Proves the abstraction the way the requirement framed it: any payment method
plugs in behind one interface, the core stays provider-neutral, and the Square
adapter now actually calls the Refunds API (audit #1). Self-contained — the
Square adapter is exercised against a fake HTTP layer, no credentials or network.
Run: python tests/test_payment_providers.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.oltp import PaymentAttemptStatus as S
from app.services import payment_providers as pp
from app.services import square

_failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL {label}")


def test_registry_resolves_builtin_providers():
    manual = pp.get_provider("manual")
    sq = pp.get_provider("square_terminal")
    check(isinstance(manual, pp.ManualProvider), "'manual' resolves")
    check(isinstance(sq, pp.SquareTerminalProvider), "'square_terminal' resolves")
    check(not manual.is_external and sq.is_external, "external flag is per-provider")
    raised = False
    try:
        pp.get_provider("does_not_exist")
    except pp.UnknownProvider:
        raised = True
    check(raised, "an unknown provider key raises, never silently guesses")


def test_manual_provider_is_local():
    m = pp.get_provider("manual")
    charge = m.charge(amount_cents=1500, currency="CAD", idempotency_key="k", tip_cents=200)
    check(charge.status == S.PROCESSOR_APPROVED, "manual charge is instantly approved")
    check(charge.tip_cents == 200, "manual charge carries the staff-entered tip")
    refund = m.refund(amount_cents=1500, currency="CAD", idempotency_key="k")
    check(refund.ok and not refund.external, "manual refund is local (no processor call)")


def test_square_adapter_charges_and_polls():
    calls = []

    def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        if path == "/v2/terminals/checkouts":
            return {"checkout": {"id": "chk_1", "status": "PENDING"}}
        if path.endswith("/chk_1"):
            return {"checkout": {"id": "chk_1", "status": "COMPLETED",
                                 "payment_ids": ["pay_9"], "tip_money": {"amount": 300}}}
        if path.startswith("/v2/payments/"):
            return {"payment": {"tip_money": {"amount": 300},
                                "card_details": {"card": {"card_brand": "VISA", "last_4": "4242"}}}}
        raise AssertionError(path)

    orig = square._request
    square._request = fake_request
    try:
        sq = pp.get_provider("square_terminal")
        charge = sq.charge(amount_cents=2000, currency="CAD", idempotency_key="k1")
        check(charge.status == S.PROCESSOR_PENDING, "square charge returns PENDING")
        check(charge.provider_checkout_id == "chk_1", "square charge captures checkout id")
        polled = sq.poll("chk_1")
        check(polled.status == S.PROCESSOR_APPROVED, "square poll maps COMPLETED->approved")
        check(polled.provider_payment_id == "pay_9", "square poll captures payment id")
        check(polled.tip_cents == 300 and polled.card_last4 == "4242",
              "square poll reads tip + card last-4")
    finally:
        square._request = orig


def test_square_adapter_executes_a_real_refund():
    """Audit #1: the refund must hit the processor, with an idempotency key."""
    seen = {}

    def fake_request(method, path, payload=None):
        seen["method"], seen["path"], seen["payload"] = method, path, payload
        return {"refund": {"id": "rf_1", "status": "COMPLETED"}}

    orig = square._request
    square._request = fake_request
    try:
        sq = pp.get_provider("square_terminal")
        res = sq.refund(amount_cents=500, currency="CAD",
                        idempotency_key="idem-123", provider_payment_id="pay_9")
        check(seen.get("path") == "/v2/refunds", "square refund calls the Refunds API")
        check(seen["payload"]["idempotency_key"] == "idem-123",
              "square refund passes the idempotency key")
        check(seen["payload"]["payment_id"] == "pay_9", "square refund targets the payment")
        check(res.ok and res.external and res.provider_refund_id == "rf_1",
              "square refund reports success + external refund id")
    finally:
        square._request = orig


def test_square_refund_without_payment_id_fails_safe():
    sq = pp.get_provider("square_terminal")
    res = sq.refund(amount_cents=500, currency="CAD", idempotency_key="k",
                    provider_payment_id=None)
    check(not res.ok, "square refund with no payment id fails instead of lying")


def test_a_new_provider_plugs_in():
    """The whole point: a new machine/processor is one class + register()."""
    class AcmePay(pp.PaymentProvider):
        key = "acme_pay"
        label = "Acme Pay"
        is_external = True

        def charge(self, *, amount_cents, currency, idempotency_key,
                   reference="", note="", tip_cents=0):
            return pp.ChargeResult(status=S.PROCESSOR_APPROVED,
                                   provider_payment_id="acme_1")

        def refund(self, *, amount_cents, currency, idempotency_key,
                   provider_payment_id=None):
            return pp.RefundResult(ok=True, external=True, provider_refund_id="acme_rf")

    pp.register(AcmePay())
    got = pp.get_provider("acme_pay")
    check(got.label == "Acme Pay", "a newly registered provider resolves by key")
    charge = got.charge(amount_cents=100, currency="USD", idempotency_key="k")
    check(charge.provider_payment_id == "acme_1", "the new provider drives a charge")
    check("acme_pay" in [p.key for p in pp.available()], "it appears in available()")


if __name__ == "__main__":
    test_registry_resolves_builtin_providers()
    test_manual_provider_is_local()
    test_square_adapter_charges_and_polls()
    test_square_adapter_executes_a_real_refund()
    test_square_refund_without_payment_id_fails_safe()
    test_a_new_provider_plugs_in()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall payment-provider tests passed")
