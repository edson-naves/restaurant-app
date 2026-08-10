"""Provider adapters (hardened) — capabilities, key forwarding, explicit state
mapping, and non-swallowed cancel. Square adapter driven against a fake HTTP
layer (no creds/network). Run: python tests/test_payment_providers.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.oltp import PaymentAttemptStatus as S
from app.models.oltp import RefundAttemptStatus as R
from app.services import payment_attempts as pa
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


def test_square_poll_reads_processor_evidence():
    # Payment captured 2300 total = 2000 pre-tip base + 300 tip, in CAD.
    def fake(method, path, payload=None):
        if path.endswith("/chk_1"):
            return {"checkout": {"id": "chk_1", "status": "COMPLETED", "payment_ids": ["pay_9"]}}
        if path.startswith("/v2/payments/"):
            return {"payment": {"total_money": {"amount": 2300, "currency": "CAD"},
                                "tip_money": {"amount": 300},
                                "card_details": {"card": {"card_brand": "VISA", "last_4": "4242"}}}}
        raise AssertionError(path)

    orig = _fake(fake)
    try:
        res = pp.get_provider("square_terminal").poll("chk_1")
        check(res.status == S.PROCESSOR_APPROVED, "COMPLETED+payment_id -> approved")
        check(res.provider_payment_id == "pay_9", "captures payment id")
        check(res.processor_amount_cents == 2000,
              "processor amount is the PRE-TIP base = total - tip (#17)")
        check(res.processor_currency == "CAD",
              "processor currency comes from the Payment object, not config (#6)")
        check(res.tip_cents == 300 and res.card_last4 == "4242", "reads tip + last4")
    finally:
        square._request = orig


def test_approval_requires_complete_evidence():
    """COMPLETED only settles with authoritative payment id + amount + currency;
    anything missing -> reconciliation (finding #3)."""
    def completed(payment_body):
        def fake(method, path, payload=None):
            if path.endswith("/chk_1"):
                return {"checkout": {"id": "chk_1", "status": "COMPLETED", "payment_ids": ["pay_9"]}}
            if path.startswith("/v2/payments/"):
                if payment_body == "TRANSPORT":
                    raise square.SquareTransportError("timeout reading payment")
                if payment_body == "DEFINITIVE":
                    raise square.SquareApiError("not found", status_code=404)
                return {"payment": payment_body}
            raise AssertionError(path)
        return fake

    good = {"total_money": {"amount": 2300, "currency": "CAD"}, "tip_money": {"amount": 300}}
    no_amount = {"tip_money": {"amount": 300}}                       # no total_money
    no_currency = {"total_money": {"amount": 2300}, "tip_money": {"amount": 300}}

    cases = [
        (good, S.PROCESSOR_APPROVED, "complete evidence -> APPROVED"),
        ("TRANSPORT", S.REQUIRES_RECONCILIATION, "payment lookup transport failure -> reconcile"),
        ("DEFINITIVE", S.REQUIRES_RECONCILIATION, "payment lookup definitive failure -> reconcile"),
        (no_amount, S.REQUIRES_RECONCILIATION, "missing processor amount -> reconcile"),
        (no_currency, S.REQUIRES_RECONCILIATION, "missing processor currency -> reconcile"),
    ]
    for body, expected, label in cases:
        orig = _fake(completed(body))
        try:
            res = pp.get_provider("square_terminal").poll("chk_1")
            check(res.status == expected, label + " (#3)")
        finally:
            square._request = orig


def test_evidence_semantic_validation():
    """Even with a payment id + currency present, incoherent evidence (negative
    amount, tip > total, malformed currency) must not approve (#4)."""
    def poll_with(payment):
        def fake(method, path, payload=None):
            if path.endswith("/chk_1"):
                return {"checkout": {"id": "chk_1", "status": "COMPLETED", "payment_ids": ["pay_9"]}}
            if path.startswith("/v2/payments/"):
                return {"payment": payment}
            raise AssertionError(path)
        return fake

    cases = [
        ({"total_money": {"amount": 1300, "currency": "CAD"}, "tip_money": {"amount": 300}},
         S.PROCESSOR_APPROVED, "valid tipped evidence -> approve"),
        ({"total_money": {"amount": 1000, "currency": "CAD"}, "tip_money": {"amount": 0}},
         S.PROCESSOR_APPROVED, "valid zero-tip evidence -> approve"),
        ({"total_money": {"amount": -500, "currency": "CAD"}, "tip_money": {"amount": 0}},
         S.REQUIRES_RECONCILIATION, "negative total -> reconcile"),
        ({"total_money": {"amount": 1000, "currency": "CAD"}, "tip_money": {"amount": -100}},
         S.REQUIRES_RECONCILIATION, "negative tip -> reconcile"),
        ({"total_money": {"amount": 1000, "currency": "CAD"}, "tip_money": {"amount": 1500}},
         S.REQUIRES_RECONCILIATION, "tip > total -> reconcile"),
        ({"total_money": {"amount": 1000, "currency": "US"}, "tip_money": {"amount": 0}},
         S.REQUIRES_RECONCILIATION, "malformed currency -> reconcile"),
    ]
    for payment, expected, label in cases:
        orig = _fake(poll_with(payment))
        try:
            res = pp.get_provider("square_terminal").poll("chk_1")
            check(res.status == expected, label + " (#4)")
        finally:
            square._request = orig


def test_charge_and_refund_forward_currency():
    seen = {}

    def fake(method, path, payload=None):
        seen[path] = payload
        if "checkouts" in path:
            return {"checkout": {"id": "chk_1", "status": "PENDING"}}
        return {"refund": {"id": "rf_1", "status": "COMPLETED"}}

    orig = _fake(fake)
    try:
        sq = pp.get_provider("square_terminal")
        sq.charge(amount_cents=1000, currency="USD", idempotency_key="k")
        sq.refund(amount_cents=500, currency="USD", idempotency_key="k2", provider_payment_id="pay_9")
        check(seen["/v2/terminals/checkouts"]["checkout"]["amount_money"]["currency"] == "USD",
              "charge forwards per-operation currency to Square (#5)")
        check(seen["/v2/refunds"]["amount_money"]["currency"] == "USD",
              "refund forwards per-operation currency to Square (#5)")
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


def test_charge_transport_ambiguity_reconciles():
    def fake(method, path, payload=None):
        raise square.SquareTransportError("timeout after send")
    orig = _fake(fake)
    try:
        res = pp.get_provider("square_terminal").charge(
            amount_cents=2000, currency="CAD", idempotency_key="k")
        check(res.status == S.REQUIRES_RECONCILIATION,
              "charge transport timeout -> reconciliation, never FAILED (#4)")
    finally:
        square._request = orig


def test_charge_definitive_decline_fails():
    def fake(method, path, payload=None):
        raise square.SquareApiError("card declined", status_code=402)
    orig = _fake(fake)
    try:
        res = pp.get_provider("square_terminal").charge(
            amount_cents=2000, currency="CAD", idempotency_key="k")
        check(res.status == S.FAILED, "definitive 4xx decline -> FAILED (#4)")
    finally:
        square._request = orig


def test_square_4xx_classification():
    """Only a definitive financial decline is FAILED; conflict/auth/config/
    invalid/unexpected 4xx -> reconciliation, never safe-to-retry (#8)."""
    def err(status, code=None, cat=None):
        def fake(method, path, payload=None):
            raise square.SquareApiError("boom", status_code=status, code=code, error_category=cat)
        return fake

    cases = [
        ((402, None, "PAYMENT_METHOD_ERROR"), S.FAILED, "402 payment-method decline -> FAILED"),
        ((400, "CARD_DECLINED", None), S.FAILED, "CARD_DECLINED -> FAILED"),
        ((409, "IDEMPOTENCY_KEY_REUSED", None), S.REQUIRES_RECONCILIATION, "409 conflict -> reconcile (charge may exist)"),
        ((401, None, "AUTHENTICATION_ERROR"), S.REQUIRES_RECONCILIATION, "401 auth/config -> reconcile, not safe-retry"),
        ((404, "NOT_FOUND", None), S.REQUIRES_RECONCILIATION, "404 invalid target -> reconcile"),
        ((400, "SOMETHING_ODD", None), S.REQUIRES_RECONCILIATION, "unexpected 4xx -> reconcile"),
    ]
    for (status, code, cat), expected, label in cases:
        orig = _fake(err(status, code, cat))
        try:
            res = pp.get_provider("square_terminal").charge(
                amount_cents=1000, currency="CAD", idempotency_key="k")
            check(res.status == expected, label + " (#8)")
        finally:
            square._request = orig


def test_unsupported_cancel_is_explicit():
    # Manual does not advertise CANCEL and must not inherit a false success (#3).
    raised = False
    try:
        pp.get_provider("manual").cancel(provider_checkout_id="x")
    except NotImplementedError:
        raised = True
    check(raised, "an unsupported provider cancel raises, never returns ok=True (#3)")
    check(pp.Capability.CANCEL in pp.get_provider("square_terminal").capabilities,
          "Square advertises CANCEL and implements it (#3)")


def test_cancel_capability_must_be_backed():
    class NoCancelPay(pp.PaymentProvider):
        key = "no_cancel_pay"; is_external = True
        capabilities = frozenset({pp.Capability.CANCEL})  # advertises but never overrides cancel()
        def charge(self, *, amount_cents, currency, idempotency_key, reference="", note="", tip_cents=0):
            return pp.ChargeResult(status=S.PROCESSOR_APPROVED)
        def refund(self, *, amount_cents, currency, idempotency_key, provider_payment_id=None):
            return pp.RefundResult(status=R.COMPLETED)
    raised = False
    try:
        pp.register(NoCancelPay())
    except ValueError:
        raised = True
    check(raised, "advertising CANCEL without implementing cancel() is rejected (#3)")


def test_unknown_capability_name_rejected():
    class TeleportPay(pp.PaymentProvider):
        key = "teleport_pay"; is_external = True
        capabilities = frozenset({"teleport"})  # not in the vocabulary
        def charge(self, *, amount_cents, currency, idempotency_key, reference="", note="", tip_cents=0):
            return pp.ChargeResult(status=S.PROCESSOR_APPROVED)
        def refund(self, *, amount_cents, currency, idempotency_key, provider_payment_id=None):
            return pp.RefundResult(status=R.COMPLETED)
    raised = False
    try:
        pp.register(TeleportPay())
    except ValueError as exc:
        raised = "unknown" in str(exc).lower()
    check(raised, "a provider advertising an unknown capability name is rejected (#7)")


def test_poll_transient_stays_pending_definitive_reconciles():
    def transient(method, path, payload=None):
        raise square.SquareTransportError("503 from Square")

    def definitive(method, path, payload=None):
        raise square.SquareApiError("not found", status_code=404)

    orig = _fake(transient)
    try:
        r = pp.get_provider("square_terminal").poll("chk_1")
        check(r.status == S.PROCESSOR_PENDING, "transient poll error stays PENDING (#9)")
    finally:
        square._request = orig
    orig = _fake(definitive)
    try:
        r = pp.get_provider("square_terminal").poll("chk_1")
        check(r.status == S.REQUIRES_RECONCILIATION,
              "definitive poll error -> reconciliation, not PENDING forever (#9)")
    finally:
        square._request = orig


def test_cancel_status_mapping():
    cases = {
        "CANCELED": (True, False),
        "COMPLETED": (False, True),   # cancel failed; payment likely exists
        "PENDING": (False, True),     # ambiguous
        "WEIRD": (False, True),       # unknown
    }
    for st, (ok, recon) in cases.items():
        def fake(method, path, payload=None, _s=st):
            return {"checkout": {"id": "chk_1", "status": _s}}
        orig = _fake(fake)
        try:
            res = pp.get_provider("square_terminal").cancel(provider_checkout_id="chk_1")
            check(res.ok == ok and res.requires_reconciliation == recon,
                  f"cancel status {st} -> ok={ok}, reconcile={recon} (#11)")
        finally:
            square._request = orig


def test_operationalerror_classification():
    # Only a lock/deadlock is a transition conflict; infra failures propagate (#12).
    class FakeOrig:
        def __init__(self, sqlstate): self.sqlstate = sqlstate
    class FakeOpErr(Exception):
        def __init__(self, sqlstate): self.orig = FakeOrig(sqlstate)
    check(pa.is_lock_conflict(FakeOpErr("40P01")), "deadlock (40P01) classified as lock conflict")
    check(pa.is_lock_conflict(FakeOpErr("55P03")), "lock_not_available (55P03) is a conflict")
    check(not pa.is_lock_conflict(FakeOpErr("08006")), "connection failure (08006) is NOT a conflict")


class AcmePay(pp.PaymentProvider):
    key = "acme_pay"; label = "Acme"; is_external = True
    capabilities = frozenset({pp.Capability.REFUND, pp.Capability.PARTIAL_REFUND})

    def charge(self, *, amount_cents, currency, idempotency_key, reference="", note="", tip_cents=0):
        return pp.ChargeResult(status=S.PROCESSOR_APPROVED, provider_payment_id="acme_1")

    def refund(self, *, amount_cents, currency, idempotency_key, provider_payment_id=None):
        return pp.RefundResult(status=R.COMPLETED, external=True, provider_refund_id="acme_rf")


def test_new_provider_plugs_in():
    pp.register(AcmePay())
    got = pp.get_provider("acme_pay")
    check(got.label == "Acme", "new provider resolves by key")
    check(got.charge(amount_cents=100, currency="USD", idempotency_key="k").provider_payment_id == "acme_1",
          "the new provider drives a charge")


def test_unsupported_capability_rejected():
    class LiarPay(pp.PaymentProvider):
        key = "liar_pay"; is_external = True
        capabilities = frozenset({pp.Capability.LOOKUP})  # never implements lookup()
        def charge(self, *, amount_cents, currency, idempotency_key, reference="", note="", tip_cents=0):
            return pp.ChargeResult(status=S.PROCESSOR_APPROVED)
        def refund(self, *, amount_cents, currency, idempotency_key, provider_payment_id=None):
            return pp.RefundResult(status=R.COMPLETED)
    raised = False
    try:
        pp.register(LiarPay())
    except ValueError:
        raised = True
    check(raised, "a provider advertising an unimplemented capability is rejected (#10)")


def test_duplicate_registry_key_rejected():
    raised = False
    try:
        pp.register(AcmePay())  # already registered above
    except ValueError:
        raised = True
    check(raised, "duplicate provider registry key is rejected (#18)")
    pp.register(AcmePay(), override=True)  # deliberate override is allowed
    check(pp.get_provider("acme_pay") is not None, "override=True replaces deliberately")


def test_builtin_capabilities_are_backed():
    for prov in pp.available():
        for cap, method in pp._CAPABILITY_METHOD.items():
            if cap in prov.capabilities:
                impl = getattr(type(prov), method, None)
                check(impl is not None and impl is not getattr(pp.PaymentProvider, method),
                      f"{prov.key} backs advertised {cap} with {method}()")


if __name__ == "__main__":
    for fn in (
        test_registry_and_capabilities,
        test_manual_is_local_with_amount,
        test_square_forwards_idempotency_key,
        test_square_poll_reads_processor_evidence,
        test_approval_requires_complete_evidence,
        test_evidence_semantic_validation,
        test_charge_and_refund_forward_currency,
        test_square_completed_without_payment_id_reconciles,
        test_square_refund_state_mapping,
        test_square_refund_without_payment_id_reconciles,
        test_cancel_is_not_swallowed,
        test_charge_transport_ambiguity_reconciles,
        test_charge_definitive_decline_fails,
        test_square_4xx_classification,
        test_unsupported_cancel_is_explicit,
        test_cancel_capability_must_be_backed,
        test_unknown_capability_name_rejected,
        test_poll_transient_stays_pending_definitive_reconciles,
        test_cancel_status_mapping,
        test_operationalerror_classification,
        test_new_provider_plugs_in,
        test_unsupported_capability_rejected,
        test_duplicate_registry_key_rejected,
        test_builtin_capabilities_are_backed,
    ):
        print(f"- {fn.__name__}")
        fn()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall payment-provider tests passed")
