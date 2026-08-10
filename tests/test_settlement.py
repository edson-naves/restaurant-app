"""Stage 2c slice 1 (corrected) — charge settlement service.

Amount/currency invariant + idempotent Payment + structured mismatch outcome +
service-owned selection canonicalization. Runs on SQLite-with-FK by default,
Postgres if PG_TEST_DSN is set. Run: python tests/test_settlement.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._pay_fixture import (
    Session, _state as _fx_state, fresh_schema, make_engine, new_db as _db, seed_parents,
)

from app.models.oltp import Payment, PaymentAttempt, PaymentAttemptStatus as S
from app.services import payment_attempts as pa
from app.services import settlement as settle

_failures = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def _fresh_sessions():
    # Tear down any open new_db() session/engine first, so drop_all on a shared
    # Postgres database is not blocked by its still-held table locks.
    if _fx_state["session"] is not None:
        try:
            _fx_state["session"].close()
        except Exception:
            pass
    if _fx_state["engine"] is not None:
        _fx_state["engine"].dispose()
    _fx_state["session"] = _fx_state["engine"] = None
    engine, _ = make_engine()
    fresh_schema(engine)
    SM = Session(engine)
    s = SM()
    ids = seed_parents(s)
    s.close()
    return SM, ids


def _approved_ext(db, ids, *, expected=1000, pamt=None, pcur="CAD", key=None, item_ids=None):
    pamt = expected if pamt is None else pamt
    a = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=expected,
                          subtotal_cents=expected, currency="CAD",
                          item_ids=item_ids, idempotency_key=key)
    pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id="chk")
    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="pay",
                  processor_amount_cents=pamt, processor_currency=pcur)
    return a


def _factory(db, ids, amount):
    def make():
        p = Payment(order_id=ids["order_id"], instrument_id=ids["instrument_id"],
                    staff_id=ids["staff_id"], total_cents=amount)
        db.add(p)
        return p
    return make


def test_external_settlement_amount_match():
    db, ids = _db()
    a = _approved_ext(db, ids, expected=1000)
    n0 = db.query(Payment).count()
    res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
    check(res.is_settled, "matching evidence settles")
    check(a.status == S.SETTLED and a.payment_id == res.payment.id, "attempt links its one Payment")
    check(db.query(Payment).count() == n0 + 1, "exactly one new Payment created")


def test_settlement_is_idempotent():
    db, ids = _db()
    a = _approved_ext(db, ids, expected=1000)
    r1 = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
    n = db.query(Payment).count()
    r2 = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
    check(r2.is_settled and r2.payment.id == r1.payment.id, "retry returns the same Payment")
    check(db.query(Payment).count() == n, "retry creates no duplicate Payment (#6)")


def test_amount_mismatch_reconciles_no_payment():
    db, ids = _db()
    a = _approved_ext(db, ids, expected=1000, pamt=9999)
    n0 = db.query(Payment).count()
    res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
    check(not res.is_settled, "amount mismatch returns a non-settled result (no exception, #3)")
    check(a.status == S.REQUIRES_RECONCILIATION, "mismatch parks the attempt for reconciliation")
    check(db.query(Payment).count() == n0, "no Payment written on mismatch")


def test_currency_mismatch_reconciles_no_payment():
    db, ids = _db()
    a = _approved_ext(db, ids, expected=1000, pcur="USD")
    n0 = db.query(Payment).count()
    res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
    check(not res.is_settled and a.status == S.REQUIRES_RECONCILIATION and db.query(Payment).count() == n0,
          "currency mismatch: no Payment; parked for reconciliation (#3)")


def test_mismatch_reconciliation_durable_across_outer_tx():
    """Slice-1 review #1 + slice-1-fix review #4: with commit=False inside the
    caller's transaction, BOTH an amount and a currency mismatch must survive the
    caller's commit (no exception rolls it back)."""
    for label, kw in (("amount", {"pamt": 9999}), ("currency", {"pcur": "USD"})):
        SM, ids = _fresh_sessions()
        db = SM()
        a = _approved_ext(db, ids, expected=1000, **kw)
        aid = a.id
        res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000), commit=False)
        check(not res.is_settled, f"{label} mismatch reported as result, not exception (#1)")
        db.commit()      # caller commits the transaction incl. the reconciliation transition
        db.close()
        db2 = SM()
        fresh = db2.get(PaymentAttempt, aid)
        check(fresh.status == S.REQUIRES_RECONCILIATION,
              f"{label} mismatch reconciliation durably committed across outer tx (#1/#4)")
        db2.close()


def test_item_id_validation_is_strict():
    db, ids = _db()

    def make(item_ids, key):
        return pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                                 staff_id=ids["staff_id"], expected_total_cents=1000,
                                 item_ids=item_ids, idempotency_key=key)

    check(make([1], "a").line_selection == "1", "positive int accepted (#3)")
    check(make(["2"], "b").line_selection == "2", "digit string accepted (#3)")
    for bad, label in ((True, "bool True"), (False, "bool False"), (1.9, "float"),
                       ("1.9", "decimal string"), (0, "zero"), (-1, "negative"),
                       (object(), "object"), ("²", "unicode superscript-2"),
                       ("٣", "arabic-indic digit")):
        raised = False
        try:
            make([bad], f"k_{label}")
        except pa.PaymentAttemptError:
            raised = True
        check(raised, f"{label} item id rejected (#3)")


def test_manual_settles_without_evidence():
    db, ids = _db()
    a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=500,
                          subtotal_cents=500, idempotency_key="m")
    pa.transition(db, a, S.PROCESSOR_PENDING)
    pa.transition(db, a, S.PROCESSOR_APPROVED)
    res = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 500))
    check(res.is_settled and a.status == S.SETTLED, "manual provider settles with no evidence")


def test_selection_canonicalized_by_service():
    """Slice-1 review #4: the attempt service owns canonicalization."""
    db, ids = _db()
    a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          item_ids=[3, 2, 1], idempotency_key="k")
    b = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          item_ids=[1, 1, 2, 3], idempotency_key="k")
    check(a.id == b.id and a.line_selection == "1,2,3",
          "unordered/duplicate item_ids canonicalize identically in the service (#4)")
    raised = False
    try:
        pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          item_ids=["not-an-int"], idempotency_key="k2")
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "malformed item id raises an explicit domain error (#4)")


def test_different_selection_conflicts():
    db, ids = _db()
    pa.create_attempt(db, provider="manual", order_id=ids["order_id"], staff_id=ids["staff_id"],
                      expected_total_cents=1000, item_ids=[1, 2, 3], idempotency_key="s")
    raised = False
    try:
        pa.create_attempt(db, provider="manual", order_id=ids["order_id"], staff_id=ids["staff_id"],
                          expected_total_cents=1000, item_ids=[1, 2, 4], idempotency_key="s")
    except pa.IdempotencyConflict:
        raised = True
    check(raised, "same key + different paid-item selection -> IdempotencyConflict (#3)")


if __name__ == "__main__":
    for fn in (
        test_external_settlement_amount_match,
        test_settlement_is_idempotent,
        test_amount_mismatch_reconciles_no_payment,
        test_currency_mismatch_reconciles_no_payment,
        test_mismatch_reconciliation_durable_across_outer_tx,
        test_item_id_validation_is_strict,
        test_manual_settles_without_evidence,
        test_selection_canonicalized_by_service,
        test_different_selection_conflicts,
    ):
        print(f"- {fn.__name__}")
        fn()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall settlement tests passed")
