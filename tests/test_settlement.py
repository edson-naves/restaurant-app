"""Stage 2c slice 1 — charge settlement service (amount/currency invariant +
idempotent local Payment). Runs on SQLite-with-FK by default, Postgres if
PG_TEST_DSN is set. Run: python tests/test_settlement.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._pay_fixture import new_db as _db

from app.models.oltp import Payment, PaymentAttemptStatus as S
from app.services import payment_attempts as pa
from app.services import settlement as settle

_failures = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def _approved_ext(db, ids, *, expected=1000, pamt=None, pcur="CAD", key=None, selection=""):
    pamt = expected if pamt is None else pamt
    a = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=expected,
                          subtotal_cents=expected, currency="CAD",
                          line_selection=selection, idempotency_key=key)
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
    pay = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
    check(a.status == S.SETTLED, "matching evidence settles the attempt")
    check(a.payment_id == pay.id, "attempt links its one Payment")
    check(db.query(Payment).count() == n0 + 1, "exactly one new Payment created")


def test_settlement_is_idempotent():
    db, ids = _db()
    a = _approved_ext(db, ids, expected=1000)
    pay = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
    n = db.query(Payment).count()
    pay2 = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
    check(pay2.id == pay.id, "retry returns the same Payment")
    check(db.query(Payment).count() == n, "retry creates no duplicate Payment (#6)")


def test_amount_mismatch_reconciles_no_payment():
    db, ids = _db()
    a = _approved_ext(db, ids, expected=1000, pamt=9999)  # processor charged a different base
    n0 = db.query(Payment).count()
    raised = False
    try:
        settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
    except settle.SettlementMismatch:
        raised = True
    check(raised, "amount mismatch raises SettlementMismatch (#3)")
    check(a.status == S.REQUIRES_RECONCILIATION, "mismatch parks the attempt for reconciliation")
    check(db.query(Payment).count() == n0, "no Payment written on mismatch")


def test_currency_mismatch_reconciles_no_payment():
    db, ids = _db()
    a = _approved_ext(db, ids, expected=1000, pcur="USD")  # attempt currency is CAD
    n0 = db.query(Payment).count()
    raised = False
    try:
        settle.settle_charge(db, a, payment_factory=_factory(db, ids, 1000))
    except settle.SettlementMismatch:
        raised = True
    check(raised, "currency mismatch raises SettlementMismatch (#3)")
    check(a.status == S.REQUIRES_RECONCILIATION and db.query(Payment).count() == n0,
          "no Payment; parked for reconciliation")


def test_manual_settles_without_evidence():
    db, ids = _db()
    a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=500,
                          subtotal_cents=500, idempotency_key="m")
    pa.transition(db, a, S.PROCESSOR_PENDING)
    pa.transition(db, a, S.PROCESSOR_APPROVED)  # manual: no external evidence
    pay = settle.settle_charge(db, a, payment_factory=_factory(db, ids, 500))
    check(a.status == S.SETTLED and a.payment_id == pay.id,
          "manual provider settles with no processor evidence")


def test_selection_is_part_of_the_fingerprint():
    db, ids = _db()
    k = "sel"
    a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          line_selection=pa.canonical_selection([3, 1, 2]), idempotency_key=k)
    b = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          line_selection=pa.canonical_selection([1, 2, 3]), idempotency_key=k)
    check(a.id == b.id, "same key + order-independent same selection -> same attempt")
    raised = False
    try:
        pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          line_selection=pa.canonical_selection([1, 2, 4]), idempotency_key=k)
    except pa.IdempotencyConflict:
        raised = True
    check(raised, "same key + different paid-item selection -> IdempotencyConflict (#3)")


if __name__ == "__main__":
    for fn in (
        test_external_settlement_amount_match,
        test_settlement_is_idempotent,
        test_amount_mismatch_reconciles_no_payment,
        test_currency_mismatch_reconciles_no_payment,
        test_manual_settles_without_evidence,
        test_selection_is_part_of_the_fingerprint,
    ):
        print(f"- {fn.__name__}")
        fn()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall settlement tests passed")
