"""Stage 2c slice 2a — manual/cash charge wired through the durable attempt
lifecycle + settlement + the no-commit pay_seat core. Covers the slice-2a deep
review: the local-snapshot invariant (booked Payment must match the durable
attempt) and request-idempotency-token behaviour. Run: python tests/test_charge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._pay_fixture import (
    Session, _state as _fx_state, fresh_schema, make_engine, pg_dsn, seed_charge,
)

from app.models.oltp import (
    Order, Payment, PaymentAllocation, PaymentAttempt, PaymentAttemptStatus as S,
    PaymentInstrument, Seat, SeatStatus,
)
from app.services import charge
from app.services import payment_attempts as pa
from app.services import settings as settings_svc
from app.services import settlement as settle
from app.services.payments import PaymentError, compute_breakdown, pay_seat
from app.services.settlement import SettlementDrift

_failures = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def _scenario(**kw):
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
    sc = seed_charge(s, **kw)
    s.close()
    return SM, sc


def _pay_factory(db, sc, selected, *, tip=0, service=0, discount=0, approver=None):
    """A real no-commit pay_seat factory bound to a fresh order/seat in `db`."""
    order = db.get(Order, sc.order_id)
    seat = db.get(Seat, sc.seat_id)

    def factory():
        return pay_seat(db, order, seat, instrument_id=sc.instrument_id, staff_id=sc.staff_id,
                        item_ids=selected, tip_cents=tip, service_charge_cents=service,
                        discount_cents=discount, discount_approved_by_id=approver)
    return factory


def _charge(db, sc, *, selected=None, key=None, tip=0, service=0, discount=0, approver=None,
            base=None, factory=None, commit=True):
    order = db.get(Order, sc.order_id)
    seat = db.get(Seat, sc.seat_id)
    selected = selected if selected is not None else [sc.item_id]
    if base is None:
        base = 1000 * len(selected)
    factory = factory or _pay_factory(db, sc, selected, tip=tip, service=service,
                                       discount=discount, approver=approver)
    return charge.settle_manual_charge(
        db, order, seat, staff_id=sc.staff_id, instrument_id=sc.instrument_id,
        base_cents=base, selected_item_ids=selected, payment_factory=factory, tip_cents=tip,
        service_charge_cents=service, discount_cents=discount, idempotency_key=key, commit=commit)


def _snapshot_attempt(db, sc, *, key, items=1000, tip=0, service=0, discount=0,
                      instrument_id=None, surcharge_rate=0.0, selected=None):
    """Create a durable manual attempt whose money snapshot is computed by the same
    compute_breakdown pay_seat uses, then drive it to PROCESSOR_APPROVED. Used to
    inject a config/state change between the snapshot and the settlement."""
    inst = db.get(PaymentInstrument, instrument_id or sc.instrument_id)
    selected = selected if selected is not None else [sc.item_id]
    bd = compute_breakdown(db, inst, items_cents=items, tip_cents=tip,
                           service_charge_cents=service, card_surcharge_rate=surcharge_rate,
                           discount_cents=discount)
    a = pa.create_attempt(
        db, provider="manual", order_id=sc.order_id, seat_id=sc.seat_id, staff_id=sc.staff_id,
        expected_total_cents=bd.expected_total_cents, subtotal_cents=bd.items_cents,
        tax_cents=bd.tax_cents, tip_cents=bd.tip_cents, service_charge_cents=bd.service_charge_cents,
        discount_cents=bd.discount_cents, surcharge_cents=bd.card_surcharge_cents,
        item_ids=selected, idempotency_key=key)
    pa.transition(db, a, S.PROCESSOR_PENDING)
    pa.transition(db, a, S.PROCESSOR_APPROVED)
    return a


def _expect_drift(fn) -> bool:
    try:
        fn()
    except SettlementDrift:
        return True
    return False


# --------------------------------------------------------------- happy path

def test_manual_charge_settles_atomically():
    SM, sc = _scenario()
    db = SM()
    result, attempt = _charge(db, sc, key="k1")
    check(result.is_settled, "manual charge settles")
    check(attempt.provider == "manual" and attempt.status == S.SETTLED, "attempt SETTLED (manual, no external id)")
    check(attempt.payment_id == result.payment.id, "attempt links its one Payment")
    check(db.query(Payment).count() == 1, "exactly one Payment")
    check(db.query(PaymentAllocation).count() == 1, "one allocation for the paid item")
    check(db.get(Seat, sc.seat_id).status in (SeatStatus.PAID, SeatStatus.PAID_PARTIAL),
          "seat marked paid")
    db.close()


def test_settlement_is_atomic_and_attempt_durable():
    """Guardrails #3/#6: attempt committed before settlement; a crash before the outer
    commit rolls back Payment + allocations + seat, leaving a durable CREATED attempt."""
    SM, sc = _scenario()
    db = SM()
    _, attempt = _charge(db, sc, key="atomic", commit=False)
    aid = attempt.id
    db.rollback()   # simulate a crash before the outer commit
    db.close()
    db2 = SM()
    a = db2.get(PaymentAttempt, aid)
    check(a is not None and a.status == S.CREATED and a.payment_id is None,
          "attempt is durable as CREATED after the rollback (#3)")
    check(db2.query(Payment).count() == 0, "no Payment persisted (atomic rollback #6)")
    check(db2.query(PaymentAllocation).count() == 0, "no allocation persisted")
    check(db2.get(Seat, sc.seat_id).status == SeatStatus.OPEN, "seat not marked paid")
    db2.close()


# ------------------------------------------ P0: local-snapshot invariant (#1)

def _mismatched_payment_factory(db, sc, *, items=1000, total=None, tip=0):
    """A factory that books a Payment NOT matching the attempt snapshot, to prove
    the settlement service refuses to settle it."""
    total = items if total is None else total

    def factory():
        p = Payment(order_id=sc.order_id, seat_id=sc.seat_id, instrument_id=sc.instrument_id,
                    staff_id=sc.staff_id, items_cents=items, tip_cents=tip, total_cents=total)
        db.add(p)
        return p
    return factory


def test_wrong_total_factory_rejected():
    """A factory whose Payment total disagrees with its own components -> drift, no settle."""
    SM, sc = _scenario()
    db = SM()
    f = _mismatched_payment_factory(db, sc, items=1000, total=1234)  # 1234 != 1000 base
    raised = False
    try:
        _charge(db, sc, key="wt", base=1000, factory=f)
    except SettlementDrift:
        raised = True
    check(raised, "inconsistent Payment total is refused (#1)")
    db.close()
    db2 = SM()
    check(db2.query(Payment).count() == 0, "no Payment persisted on drift")
    check(db2.query(PaymentAllocation).count() == 0, "no allocation persisted on drift")
    a = db2.query(PaymentAttempt).one()
    check(a.status == S.CREATED and a.payment_id is None,
          "attempt left durable + retryable (CREATED), not SETTLED (#1)")
    db2.close()


def test_wrong_base_rejected():
    """The attempt snapshot base (999) disagrees with what pay_seat books from locked
    state (1000) -> drift. This is the TOCTOU proof: locked recompute != attempt (#3)."""
    SM, sc = _scenario()
    db = SM()
    raised = False
    try:
        _charge(db, sc, key="wb", base=999)   # item is 1000
    except SettlementDrift:
        raised = True
    check(raised, "booked payable != attempt base is refused (#3)")
    db.close()
    db2 = SM()
    check(db2.query(Payment).count() == 0 and db2.query(PaymentAllocation).count() == 0,
          "no partial Payment/allocation survives the rejection (#3)")
    db2.close()


def test_wrong_tip_rejected():
    """A tip booked onto the Payment that the attempt did not capture -> drift (#1)."""
    SM, sc = _scenario()
    db = SM()
    # attempt captures tip=0, but the factory books tip=500 through pay_seat
    f = _pay_factory(db, sc, [sc.item_id], tip=500)
    raised = False
    try:
        _charge(db, sc, key="wtip", tip=0, factory=f)
    except SettlementDrift:
        raised = True
    check(raised, "tip not in the attempt snapshot is refused (#1)")
    db.close()


def test_toctou_item_paid_elsewhere_rejected():
    """Realistic drift: one of the two selected items is paid out-of-band between the
    attempt being created and its settlement. pay_seat books only the still-outstanding
    item, so the booked payable (1000) != the attempt snapshot (2000) -> no second
    Payment on the already-paid item; the whole settle rejects (#3)."""
    SM, sc = _scenario(n_items=2)
    i1, i2 = sc.item_ids
    db = SM()
    # someone pays item i2 first (its own committed charge)
    _charge(db, sc, selected=[i2], base=1000, key="first")
    n_after_first = db.query(Payment).count()
    # now try to settle a stale attempt that still believes both items are payable
    raised = False
    try:
        _charge(db, sc, selected=[i1, i2], base=2000, key="stale")
    except SettlementDrift:
        raised = True
    check(raised, "a drifted two-item settlement is refused (#3)")
    check(db.query(Payment).count() == n_after_first, "no second Payment booked on the paid item (#3)")
    db.close()


# ------------------------------------- P1: request idempotency token behaviour

def test_same_token_same_intent_converges():
    """Double-submit / lost-response retry: same token + same intent -> the same
    durable attempt and the same Payment, with no duplicate allocation (#1/#2/#4)."""
    SM, sc = _scenario()
    db = SM()
    r1, a1 = _charge(db, sc, key="tok")
    n_pay, n_alloc = db.query(Payment).count(), db.query(PaymentAllocation).count()
    r2, a2 = _charge(db, sc, key="tok")   # resubmit same token
    check(a1.id == a2.id, "same token resolves to the same durable attempt (#1)")
    check(r2.payment.id == r1.payment.id, "same token converges on the same Payment (#4)")
    check(db.query(Payment).count() == n_pay and db.query(PaymentAllocation).count() == n_alloc,
          "no duplicate Payment or allocation on retry (#2)")
    db.close()


def test_response_loss_retry_in_fresh_session_converges():
    """The lost-response retry arrives on a brand-new session/request after the first
    committed: it must still converge on the one settled Payment (#4)."""
    SM, sc = _scenario()
    db = SM()
    r1, a1 = _charge(db, sc, key="lost")
    a1_id, p1_id = a1.id, r1.payment.id
    db.close()
    db2 = SM()   # a fresh request
    r2, a2 = _charge(db2, sc, key="lost")
    check(a2.id == a1_id and r2.payment.id == p1_id, "fresh-session retry converges (#4)")
    check(db2.query(Payment).count() == 1, "still exactly one Payment")
    db2.close()


def test_same_token_changed_amount_conflicts():
    """Reusing a token for a materially different intent (changed amount) must not
    silently settle a different amount -> IdempotencyConflict (#3)."""
    SM, sc = _scenario()
    db = SM()
    _charge(db, sc, key="c", base=1000)
    raised = False
    try:
        _charge(db, sc, key="c", base=2000)   # same token, different amount
    except pa.IdempotencyConflict:
        raised = True
    check(raised, "same token + changed amount raises IdempotencyConflict (#3)")
    db.close()


def test_drift_retry_reuses_same_durable_attempt():
    """Recovery policy (#6): after a local-settlement drift, retrying with the same
    token + same intent reuses the ONE durable attempt (no attempt proliferation),
    rather than spawning a second stray CREATED attempt per submit."""
    SM, sc = _scenario()
    db = SM()
    for _ in range(2):
        try:
            _charge(db, sc, key="r", base=999)  # permanent snapshot/world mismatch -> drift
        except SettlementDrift:
            pass
    check(db.query(PaymentAttempt).filter_by(idempotency_key="r").count() == 1,
          "repeated drifting retries reuse the one durable attempt (#6)")
    a = db.query(PaymentAttempt).filter_by(idempotency_key="r").one()
    check(a.status == S.CREATED and a.payment_id is None, "the reused attempt stays retryable")
    check(db.query(Payment).count() == 0, "no Payment ever booked across the retries")
    db.close()


# ------------------------------------------ v4 P0: full snapshot + semantics

def test_expected_total_is_pre_tip_with_all_components():
    """v4 #1: expected_total_cents is the provider-neutral PRE-TIP total
    (subtotal - discount + tax + service + surcharge), NOT the raw item subtotal;
    the final Payment total = expected_total + tip."""
    SM, sc = _scenario()
    settings_svc.save(SM(), {"gst_rate": "5", "pst_rate": "0", "service_charge_rate": "10"})
    db = SM()
    # base 1000; 10% service = 100; 5% tax on (1000) = 50; tip 200. Discount 0.
    result, attempt = _charge(db, sc, key="sem", tip=200, service=100)
    check(attempt.subtotal_cents == 1000, "subtotal snapshot is the raw item base")
    check(attempt.tax_cents == 50 and attempt.service_charge_cents == 100,
          "tax + service captured on the attempt")
    check(attempt.expected_total_cents == 1000 - 0 + 50 + 100 + 0,
          "expected_total_cents is the pre-tip total (1150), not the subtotal (#1)")
    check(attempt.tip_cents == 200, "tip captured separately")
    check(result.payment.total_cents == attempt.expected_total_cents + attempt.tip_cents,
          "final Payment total == expected_total + tip (#1)")
    db.close()


def test_tax_config_drift_rejected():
    """v4 #2: the committed attempt tax snapshot is authoritative — if venue tax
    config changes before the locked settlement, the booked Payment's tax differs
    and the settle drifts rather than silently using the new rate."""
    SM, sc = _scenario()
    db = SM()
    a = _snapshot_attempt(db, sc, key="taxdrift")     # snapshot at the default 5% tax
    settings_svc.save(db, {"gst_rate": "0"})          # tax config changes to 0%
    f = _pay_factory(db, sc, [sc.item_id])            # pay_seat now books tax = 0
    check(_expect_drift(lambda: settle.settle_charge(db, a, payment_factory=f, commit=False)),
          "tax change after snapshot -> SettlementDrift (#2)")
    db.rollback()
    check(db.query(Payment).count() == 0 and db.query(PaymentAllocation).count() == 0,
          "no Payment/allocation survives tax drift (#2)")
    fresh = db.get(PaymentAttempt, a.id)
    check(fresh.status != S.SETTLED and fresh.payment_id is None,
          "attempt not settled; stays retryable (#2)")
    db.close()


def test_surcharge_drift_rejected():
    """v4 #2: same for the card surcharge. Snapshot with a 10% surcharge, then settle
    with the surcharge rate dropped to 0 -> the booked surcharge differs -> drift."""
    SM, sc = _scenario()
    db = SM()
    card = PaymentInstrument(code="visa_t", name="Visa", instrument_type="card", provider="manual")
    db.add(card)
    db.commit()
    a = _snapshot_attempt(db, sc, key="scdrift", instrument_id=card.id, surcharge_rate=0.10)
    check(a.surcharge_cents > 0, "surcharge captured on the attempt snapshot")

    def f():
        order = db.get(Order, sc.order_id)
        seat = db.get(Seat, sc.seat_id)
        return pay_seat(db, order, seat, instrument_id=card.id, staff_id=sc.staff_id,
                        item_ids=[sc.item_id], card_surcharge_rate=0.0)  # rate dropped
    check(_expect_drift(lambda: settle.settle_charge(db, a, payment_factory=f, commit=False)),
          "surcharge change after snapshot -> SettlementDrift (#2)")
    db.rollback()
    check(db.query(Payment).count() == 0, "no Payment survives surcharge drift (#2)")
    db.close()


# ---------------------------------------------------------- Postgres only

def test_concurrent_manual_charge_no_double():
    """Two concurrent charges of the same item cannot both create a Payment: pay_seat's
    order-row lock + re-validation makes the loser see nothing outstanding. PG only."""
    if not pg_dsn():
        check(True, "SKIP concurrent (needs Postgres)")
        return
    SM, sc = _scenario()
    da, dbb = SM(), SM()
    r1, _ = _charge(da, sc, key="cA")
    raised = False
    try:
        _charge(dbb, sc, key="cB")   # same item, different key -> nothing outstanding
    except PaymentError:
        raised = True
    check(r1.is_settled and raised, "second concurrent charge is refused (no double Payment)")
    verify = SM()
    check(verify.query(Payment).count() == 1, "exactly one Payment across both")
    verify.close(); da.close(); dbb.close()


if __name__ == "__main__":
    for fn in (test_manual_charge_settles_atomically,
               test_settlement_is_atomic_and_attempt_durable,
               test_wrong_total_factory_rejected,
               test_wrong_base_rejected,
               test_wrong_tip_rejected,
               test_toctou_item_paid_elsewhere_rejected,
               test_same_token_same_intent_converges,
               test_response_loss_retry_in_fresh_session_converges,
               test_same_token_changed_amount_conflicts,
               test_drift_retry_reuses_same_durable_attempt,
               test_expected_total_is_pre_tip_with_all_components,
               test_tax_config_drift_rejected,
               test_surcharge_drift_rejected,
               test_concurrent_manual_charge_no_double):
        print(f"- {fn.__name__}")
        fn()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall charge tests passed")
