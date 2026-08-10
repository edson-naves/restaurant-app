"""Provider adapters (hardened) — capabilities, key forwarding, explicit state
mapping, and non-swallowed cancel. Square adapter driven against a fake HTTP
layer (no creds/network). Run: python tests/test_payment_providers.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.oltp import PaymentAttemptStatus as S
from app.models.oltp import RefundAttemptStatus as R
from app.services import payment_providers as pp
from app.services import square

_failures = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def _fake(fn):
    orig = square._request
    square._request = fn
    return orig


def test_registry_and_capabilities():
    manual = pp.get_provider("manual")
    sq = pp.get_provider("square_terminal")
    check(not manual.is_external and sq.is_external, "external flag per provider")
    check(sq.needs_polling and not manual.needs_polling, "polling capability drives needs_polling")
    check(pp.Capability.PARTIAL_REFUND in manual.capabilities, "manual advertises partial_refund")
    raised = False
    try:
        pp.get_provider("nope")
    except pp.UnknownProvider:
        raised = True
    check(raised, "unknown provider raises, never guesses")


def test_manual_is_local_with_amount():
    m = pp.get_provider("manual")
    c = m.charge(amount_cents=1500, currency="CAD", idempotency_key="k", tip_cents=200)
    check(c.status == S.PROCESSOR_APPROVED, "manual charge instantly approved")
    check(c.processor_amount_cents == 1500 and c.processor_currency == "CAD",
          "manual charge echoes processor amount/currency")
    r = m.refund(amount_cents=1500, currency="CAD", idempotency_key="k")
    check(r.status == R.COMPLETED and not r.external, "manual refund is local + completed")


def test_square_forwards_idempotency_key():
    seen = {}

    def fake(method, path, payload=None):
        seen[path] = payload
        return {"checkout": {"id": "chk_1", "status": "PENDING"}}

    orig = _fake(fake)
    try:
        sq = pp.get_provider("square_terminal")
        sq.charge(amount_cents=2000, currency="CAD", idempotency_key="IDEM-XYZ")
        body = seen["/v2/terminals/checkouts"]
        check(body["idempotency_key"] == "IDEM-XYZ",
              "persisted idempotency key is forwarded into the Square charge (#1)")
    finally:
        square._request = orig


def test_square_poll_completed_with_payment():
    def fake(method, path, payload=None):
        if path.endswith("/chk_1"):
            return {"checkout": {"id": "chk_1", "status": "COMPLETED",
                                 "payment_ids": ["pay_9"], "amount_money": {"amount": 2000}}}
        if path.startswith("/v2/payments/"):
            return {"payment": {"tip_money": {"amount": 300},
                                "card_details": {"card": {"card_brand": "VISA", "last_4": "4242"}}}}
        raise AssertionError(path)

    orig = _fake(fake)
    try:
        res = pp.get_provider("square_terminal").poll("chk_1")
        check(res.status == S.PROCESSOR_APPROVED, "COMPLETED+payment_id -> approved")
        check(res.provider_payment_id == "pay_9", "captures payment id")
        check(res.processor_amount_cents == 2000, "captures processor amount (#8)")
        check(res.tip_cents == 300 and res.card_last4 == "4242", "reads tip + last4")
    finally:
        square._request = orig


def test_square_completed_without_payment_id_reconciles():
    def fake(method, path, payload=None):
        return {"checkout": {"id": "chk_1", "status": "COMPLETED", "payment_ids": []}}

    orig = _fake(fake)
    try:
        res = pp.get_provider("square_terminal").poll("chk_1")
        check(res.status == S.REQUIRES_RECONCILIATION,
              "COMPLETED without payment id -> reconciliation, not approval (#9)")
    finally:
        square._request = orig


def test_square_refund_state_mapping():
    cases = {
        "COMPLETED": R.COMPLETED,
        "PENDING": R.PROCESSOR_PENDING,
        "REJECTED": R.REJECTED,
        "FAILED": R.FAILED,
        "WEIRD": R.REQUIRES_RECONCILIATION,
    }
    for sq_status, expected in cases.items():
        def fake(method, path, payload=None, _s=sq_status):
            return {"refund": {"id": "rf_1", "status": _s}}
        orig = _fake(fake)
        try:
            res = pp.get_provider("square_terminal").refund(
                amount_cents=500, currency="CAD", idempotency_key="k",
                provider_payment_id="pay_9")
            check(res.status == expected, f"Square refund {sq_status} -> {expected}")
            if sq_status in ("FAILED", "WEIRD"):
                check(bool(res.error), f"{sq_status} carries a non-empty error (#11)")
        finally:
            square._request = orig


def test_square_refund_without_payment_id_reconciles():
    res = pp.get_provider("square_terminal").refund(
        amount_cents=500, currency="CAD", idempotency_key="k", provider_payment_id=None)
    check(res.status == R.REQUIRES_RECONCILIATION, "refund w/o payment id -> reconcile, not lie")


def test_cancel_is_not_swallowed():
    def fake(method, path, payload=None):
        raise square.SquareError("network blip")
    orig = _fake(fake)
    try:
        res = pp.get_provider("square_terminal").cancel(provider_checkout_id="chk_1")
        check(not res.ok and res.requires_reconciliation,
              "a failed cancel flags reconciliation, never silent success (#10)")
    finally:
        square._request = orig


def test_new_provider_plugs_in():
    class AcmePay(pp.PaymentProvider):
        key = "acme_pay"; label = "Acme"; is_external = True
        capabilities = frozenset({pp.Capability.AUTHORIZE, pp.Capability.CAPTURE})

        def charge(self, *, amount_cents, currency, idempotency_key, reference="", note="", tip_cents=0):
            return pp.ChargeResult(status=S.PROCESSOR_APPROVED, provider_payment_id="acme_1")

        def refund(self, *, amount_cents, currency, idempotency_key, provider_payment_id=None):
            return pp.RefundResult(status=R.COMPLETED, external=True, provider_refund_id="acme_rf")

    pp.register(AcmePay())
    got = pp.get_provider("acme_pay")
    check(got.label == "Acme", "new provider resolves by key")
    check(pp.Capability.AUTHORIZE in got.capabilities, "new provider advertises capabilities")


if __name__ == "__main__":
    for fn in (
        test_registry_and_capabilities,
        test_manual_is_local_with_amount,
        test_square_forwards_idempotency_key,
        test_square_poll_completed_with_payment,
        test_square_completed_without_payment_id_reconciles,
        test_square_refund_state_mapping,
        test_square_refund_without_payment_id_reconciles,
        test_cancel_is_not_swallowed,
        test_new_provider_plugs_in,
    ):
        print(f"- {fn.__name__}")
        fn()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall payment-provider tests passed")
