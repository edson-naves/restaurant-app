"""Refund-attempt lifecycle (finding #6) — multiple independent partial refunds.

Run: python tests/test_refund_attempts.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._pay_fixture import new_db as _db

from app.models.oltp import (
    AuditEvent, Payment, PaymentAttemptStatus as PS, PaymentInstrument,
    RefundAttempt, RefundAttemptStatus as R, Staff,
)
from app.services import payment_attempts as pa
from app.services import refund_attempts as ra


def _settled_attempt(db, ids, provider="square_terminal"):
    """A charge attempt walked to SETTLED against the seeded payment."""
    a = pa.create_attempt(db, provider=provider, order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=10000,
                          subtotal_cents=10000)
    pa.transition(db, a, PS.PROCESSOR_PENDING)
    pa.transition(db, a, PS.PROCESSOR_APPROVED, provider_payment_id="pay_seed",
                  processor_amount_cents=10000, processor_currency="CAD")
    pa.transition(db, a, PS.SETTLED, payment_id=ids["payment_id"])
    return a

_failures = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def _mkref(db, ids, amount, provider="manual", key=None, currency="CAD"):
    # The seeded payment uses a cash/'manual' instrument, so a legacy refund
    # defaults to provider='manual' to match it (#4).
    return ra.create_refund_attempt(
        db, payment_id=ids["payment_id"], staff_id=ids["staff_id"],
        provider=provider, amount_cents=amount, currency=currency, idempotency_key=key,
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
    _settled_attempt(db, ids)  # square_terminal
    _mkref(db, ids, 1000, key="rk", provider="square_terminal")
    raised = False
    try:
        _mkref(db, ids, 2500, key="rk", provider="square_terminal")  # same key, diff amount
    except pa.IdempotencyConflict:
        raised = True
    check(raised, "same refund key + different amount raises IdempotencyConflict (#3)")


def test_legacy_refund_provider_derivation():
    db, ids = _db()  # seeded payment uses a 'manual' cash instrument, no attempt
    # matching provider accepted
    r = _mkref(db, ids, 500, provider="manual")
    check(r.provider == "manual", "legacy manual payment refunded as manual is accepted (#4)")
    # caller claiming square_terminal on a manual legacy payment is rejected
    raised = False
    try:
        _mkref(db, ids, 500, provider="square_terminal", key="x")
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "legacy manual payment cannot be refunded as square_terminal (#4)")


def test_legacy_square_payment_refunded_as_manual_rejected():
    db, ids = _db()
    inst = PaymentInstrument(code="card_terminal", name="Card (terminal)",
                             instrument_type="card", provider="square_terminal")
    db.add(inst); db.flush()
    pay = Payment(order_id=ids["order_id"], instrument_id=inst.id,
                  staff_id=ids["staff_id"], total_cents=5000)
    db.add(pay); db.commit()
    raised = False
    try:
        ra.create_refund_attempt(db, payment_id=pay.id, staff_id=ids["staff_id"],
                                 provider="manual", amount_cents=500)
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "legacy square payment cannot be refunded as manual (#4)")


def test_legacy_provider_must_be_derivable():
    """A legacy payment whose provider cannot be derived (blank or unregistered
    instrument provider) fails closed — caller input never becomes authoritative (#5)."""
    db, ids = _db()
    # blank instrument provider
    blank = PaymentInstrument(code="blank_inst", name="Blank", instrument_type="card", provider="")
    db.add(blank); db.flush()
    pay1 = Payment(order_id=ids["order_id"], instrument_id=blank.id, staff_id=ids["staff_id"], total_cents=1000)
    db.add(pay1); db.commit()
    raised = False
    try:
        ra.create_refund_attempt(db, payment_id=pay1.id, staff_id=ids["staff_id"],
                                 provider="manual", amount_cents=100)
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "legacy payment with blank instrument provider is rejected (#5)")

    # unregistered instrument provider
    bogus = PaymentInstrument(code="bogus_inst", name="Bogus", instrument_type="card", provider="ghost_pay")
    db.add(bogus); db.flush()
    pay2 = Payment(order_id=ids["order_id"], instrument_id=bogus.id, staff_id=ids["staff_id"], total_cents=1000)
    db.add(pay2); db.commit()
    raised = False
    try:
        ra.create_refund_attempt(db, payment_id=pay2.id, staff_id=ids["staff_id"],
                                 provider="ghost_pay", amount_cents=100)
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "legacy payment with unregistered instrument provider is rejected (#5)")


def test_refund_currency_must_match():
    db, ids = _db()
    _settled_attempt(db, ids)  # CAD square_terminal charge
    ok = _mkref(db, ids, 500, provider="square_terminal", currency="CAD")
    check(ok.currency == "CAD", "CAD charge -> CAD refund accepted (#5)")
    raised = False
    try:
        _mkref(db, ids, 500, provider="square_terminal", currency="USD", key="u")
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "CAD charge -> USD refund rejected (#5)")
    # legacy payment: refund currency must equal the venue currency (CAD)
    db2, ids2 = _db()
    raised = False
    try:
        _mkref(db2, ids2, 500, provider="manual", currency="USD")
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "legacy refund in a non-venue currency rejected (#5)")


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


def test_refund_currency_defaults_to_venue():
    db, ids = _db()
    old = os.environ.get("VENUE_CURRENCY")
    os.environ["VENUE_CURRENCY"] = "USD"
    try:
        r = ra.create_refund_attempt(db, payment_id=ids["payment_id"], staff_id=ids["staff_id"],
                                     provider="manual", amount_cents=500)  # currency omitted
        check(r.currency == "USD", "omitted refund currency defaults to venue, not CAD (#3)")
    finally:
        os.environ.pop("VENUE_CURRENCY", None) if old is None else os.environ.__setitem__("VENUE_CURRENCY", old)


def test_refund_reconciliation_authority():
    db, ids = _db()
    r = _mkref(db, ids, 500, provider="manual")
    ra.transition_refund(db, r, R.REQUIRES_RECONCILIATION, last_error="lost")

    # bare transition out of reconciliation is blocked
    for target in (R.COMPLETED, R.FAILED):
        raised = False
        try:
            ra.transition_refund(db, r, target)
        except pa.PaymentAttemptError:
            raised = True
        check(raised, f"bare refund reconciliation -> {target} rejected (#1)")

    # unauthorized actor cannot resolve
    waiter = Staff(name="Wanda", role="waiter", pin_code="x")
    db.add(waiter); db.commit()
    raised = False
    try:
        ra.resolve_refund_reconciliation(db, r, resolved_status=R.COMPLETED, note="ok", actor=waiter)
    except pa.ReconciliationAuthorityError:
        raised = True
    check(raised, "a non-manager cannot resolve refund reconciliation (#1)")

    # automatic without evidence rejected
    raised = False
    try:
        ra.resolve_refund_reconciliation(db, r, resolved_status=R.COMPLETED, note="x", automatic=True)
    except pa.ReconciliationAuthorityError:
        raised = True
    check(raised, "automatic refund reconciliation needs provider evidence (#1)")

    # authorized owner resolves + audit
    owner = db.get(Staff, ids["staff_id"])  # seeded owner
    ra.resolve_refund_reconciliation(db, r, resolved_status=R.COMPLETED, note="verified in dashboard",
                                     actor=owner, provider_evidence="rf_123")
    check(r.status == R.COMPLETED and r.reconciled_by == owner.name,
          "authorized manager resolves refund + records who (#1)")
    check(db.query(AuditEvent).filter_by(action="reconcile_refund_attempt").count() == 1,
          "an audit event is written for the refund resolution (#1)")

    # automatic WITH evidence accepted (fresh refund)
    r2 = _mkref(db, ids, 200, provider="manual", key="r2")
    ra.transition_refund(db, r2, R.REQUIRES_RECONCILIATION)
    ra.resolve_refund_reconciliation(db, r2, resolved_status=R.FAILED, note="processor lookup",
                                     automatic=True, provider_evidence="lookup:not_found")
    check(r2.status == R.FAILED and r2.reconciled_by == "system:auto",
          "automatic refund reconciliation with evidence is accepted (#1)")


def test_external_refund_requires_refund_id():
    db, ids = _db()
    _settled_attempt(db, ids)  # square_terminal settled charge
    r = _mkref(db, ids, 500, provider="square_terminal", key="er1")
    raised = False
    try:
        ra.transition_refund(db, r, R.PROCESSOR_PENDING)  # no id
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "external refund -> PENDING without provider_refund_id rejected (#1)")
    ra.transition_refund(db, r, R.PROCESSOR_PENDING, provider_refund_id="rf_1")
    check(r.status == R.PROCESSOR_PENDING, "external refund -> PENDING with id accepted (#1)")
    ra.transition_refund(db, r, R.COMPLETED)  # persisted id carries forward
    check(r.status == R.COMPLETED, "external refund PENDING(persisted id) -> COMPLETED accepted (#1)")

    r2 = _mkref(db, ids, 200, provider="square_terminal", key="er2")
    raised = False
    try:
        ra.transition_refund(db, r2, R.COMPLETED)  # no id
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "external refund CREATED -> COMPLETED without id rejected (#1)")


def test_manual_refund_completes_without_id():
    db, ids = _db()  # seeded manual payment, no attempt
    r = _mkref(db, ids, 100, provider="manual")
    ra.transition_refund(db, r, R.COMPLETED)  # no provider_refund_id needed
    check(r.status == R.COMPLETED, "manual refund completes without a processor id (#1)")


def test_external_refund_reconciliation_requires_id():
    db, ids = _db()
    _settled_attempt(db, ids)
    r = _mkref(db, ids, 500, provider="square_terminal", key="er")
    ra.transition_refund(db, r, R.REQUIRES_RECONCILIATION)
    raised = False
    try:
        ra.resolve_refund_reconciliation(db, r, resolved_status=R.COMPLETED, note="x",
                                         automatic=True, provider_evidence="ev")  # no id
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "external reconciliation -> COMPLETED without refund id rejected (#1)")
    ra.resolve_refund_reconciliation(db, r, resolved_status=R.COMPLETED, note="x",
                                     automatic=True, provider_evidence="ev", provider_refund_id="rf_done")
    check(r.status == R.COMPLETED, "external reconciliation -> COMPLETED with refund id accepted (#1)")


def test_refund_provider_id_unique_and_write_once():
    db, ids = _db()
    _settled_attempt(db, ids)
    a = _mkref(db, ids, 100, provider="square_terminal", key="u1")
    b = _mkref(db, ids, 100, provider="square_terminal", key="u2")
    ra.transition_refund(db, a, R.PROCESSOR_PENDING, provider_refund_id="RF")
    raised = False
    try:
        ra.transition_refund(db, a, R.COMPLETED, provider_refund_id="RF2")  # change id
    except pa.TransitionConflict:
        raised = True
    check(raised, "provider_refund_id is write-once (#1)")
    raised = False
    try:
        ra.transition_refund(db, b, R.PROCESSOR_PENDING, provider_refund_id="RF")  # duplicate
    except pa.TransitionConflict:
        raised = True
    check(raised, "duplicate (provider, provider_refund_id) rejected (#1)")


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
        test_legacy_refund_provider_derivation,
        test_legacy_square_payment_refunded_as_manual_rejected,
        test_legacy_provider_must_be_derivable,
        test_refund_reconciliation_authority,
        test_external_refund_requires_refund_id,
        test_manual_refund_completes_without_id,
        test_external_refund_reconciliation_requires_id,
        test_refund_provider_id_unique_and_write_once,
        test_refund_currency_must_match,
        test_refund_currency_defaults_to_venue,
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
