"""Refund-attempt lifecycle (finding #6) — multiple independent partial refunds.

Run: python tests/test_refund_attempts.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._pay_fixture import new_db as _db

from app.models.oltp import PaymentAttemptStatus as PS, RefundAttempt, RefundAttemptStatus as R
from app.services import payment_attempts as pa
from app.services import refund_attempts as ra


def _settled_attempt(db, ids, provider="square_terminal"):
    """A charge attempt walked to SETTLED against the seeded payment."""
    a = pa.create_attempt(db, provider=provider, order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=10000,
                          subtotal_cents=10000)
    pa.transition(db, a, PS.PROCESSOR_PENDING)
    pa.transition(db, a, PS.PROCESSOR_APPROVED, provider_payment_id="pay_seed")
    pa.transition(db, a, PS.SETTLED, payment_id=ids["payment_id"])
    return a

_failures = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def _mkref(db, ids, amount, provider="square_terminal", key=None):
    return ra.create_refund_attempt(
        db, payment_id=ids["payment_id"], staff_id=ids["staff_id"],
        provider=provider, amount_cents=amount, idempotency_key=key,
    )


def test_multiple_partial_refunds():
    db, ids = _db()
    r1 = _mkref(db, ids, 1500)
    r2 = _mkref(db, ids, 2000)
    r3 = _mkref(db, ids, 500)
    check(len({r1.id, r2.id, r3.id}) == 3, "three independent refund attempts exist")
    check(len({r1.idempotency_key, r2.idempotency_key, r3.idempotency_key}) == 3,
          "each refund has its own idempotency key")


def test_idempotent_refund_create():
    db, ids = _db()
    a = _mkref(db, ids, 1000, key="rk")
    b = _mkref(db, ids, 1000, key="rk")
    check(a.id == b.id, "same key returns the same refund attempt")
    check(db.query(RefundAttempt).count() == 1, "no duplicate refund row")


def test_refund_amount_must_be_positive():
    db, ids = _db()
    raised = False
    try:
        _mkref(db, ids, 0)
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "zero/negative refund amount is rejected")


def test_running_total_counts_inflight_and_completed():
    db, ids = _db()
    r1 = _mkref(db, ids, 1500)
    r2 = _mkref(db, ids, 2000)
    ra.transition_refund(db, r1, R.COMPLETED, provider_refund_id="rf_1")
    # r2 stays CREATED (in flight) — still counts against balance
    total = ra.refunded_and_pending_cents(db, ids["payment_id"])
    check(total == 3500, "refunded+pending total counts completed and in-flight")
    ra.transition_refund(db, r2, R.REJECTED, last_error="declined")
    total2 = ra.refunded_and_pending_cents(db, ids["payment_id"])
    check(total2 == 1500, "a rejected refund frees its amount from the total")


def test_same_key_different_amount_conflicts():
    db, ids = _db()
    _settled_attempt(db, ids)
    _mkref(db, ids, 1000, key="rk")
    raised = False
    try:
        _mkref(db, ids, 2500, key="rk")  # same key, different amount
    except pa.IdempotencyConflict:
        raised = True
    check(raised, "same refund key + different amount raises IdempotencyConflict (#3)")


def test_provider_mismatch_rejected():
    db, ids = _db()
    _settled_attempt(db, ids, provider="square_terminal")
    raised = False
    try:
        _mkref(db, ids, 500, provider="manual")  # settled attempt is square_terminal
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "refund provider must match the charge attempt's provider (#7)")


def test_wrong_charge_attempt_rejected():
    db, ids = _db()
    _settled_attempt(db, ids)
    # A charge attempt that does NOT back this payment (still pending, no payment_id).
    other = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
                              staff_id=ids["staff_id"], expected_total_cents=500)
    raised = False
    try:
        ra.create_refund_attempt(db, payment_id=ids["payment_id"], staff_id=ids["staff_id"],
                                 provider="square_terminal", amount_cents=500,
                                 charge_attempt_id=other.id)
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "a charge attempt not backing this payment is rejected (#7)")


def test_refund_state_transitions():
    db, ids = _db()
    r = _mkref(db, ids, 500)
    ra.transition_refund(db, r, R.PROCESSOR_PENDING, provider_refund_id="rf_x")
    ra.transition_refund(db, r, R.COMPLETED)
    check(r.status == R.COMPLETED, "created->pending->completed")
    raised = False
    try:
        ra.transition_refund(db, r, R.FAILED)  # terminal
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "COMPLETED refund is terminal")


if __name__ == "__main__":
    for fn in (
        test_multiple_partial_refunds,
        test_idempotent_refund_create,
        test_refund_amount_must_be_positive,
        test_running_total_counts_inflight_and_completed,
        test_same_key_different_amount_conflicts,
        test_provider_mismatch_rejected,
        test_wrong_charge_attempt_rejected,
        test_refund_state_transitions,
    ):
        print(f"- {fn.__name__}")
        fn()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall refund-attempt tests passed")
