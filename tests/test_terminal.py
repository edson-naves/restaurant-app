"""Stage 2c slice 2b — Square terminal charge wired through the durable attempt
lifecycle + settlement. Exercises the external settle-vs-reconcile policy without a
live Square (crafted ChargeResults; square.create_checkout monkeypatched for start).
Run: python tests/test_terminal.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._pay_fixture import (
    Session, _state as _fx_state, fresh_schema, make_engine, pg_dsn, seed_charge,
)

from app.models.oltp import (
    Order, Payment, PaymentAllocation, PaymentAttempt, PaymentAttemptStatus as S,
    PaymentInstrument, Seat,
)
from app.services import charge
from app.services import payment_attempts as pa
from app.services import settlement as settle
from app.services import square
from app.services.payment_providers import ChargeResult
from app.services.payments import PaymentError, compute_breakdown, pay_seat

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
    card = PaymentInstrument(code="card_terminal", name="Card (terminal)",
                             instrument_type="card", provider="square_terminal")
    s.add(card)
    s.commit()
    sc.card_id = card.id
    s.close()
    # Track the engine so the NEXT _scenario disposes this one — otherwise each PG
    # engine's pool leaks connections and a long suite exhausts max_connections.
    _fx_state["engine"] = engine
    return SM, sc


def _pending_attempt(db, sc, *, key="t", base=1000, service=0, checkout="chk1"):
    """A committed square_terminal attempt in PROCESSOR_PENDING (as after start)."""
    inst = db.get(PaymentInstrument, sc.card_id)
    bd = compute_breakdown(db, inst, items_cents=base, service_charge_cents=service,
                           card_surcharge_rate=0.0)
    a = pa.create_attempt(
        db, provider="square_terminal", order_id=sc.order_id, seat_id=sc.seat_id,
        staff_id=sc.staff_id, expected_total_cents=bd.expected_total_cents,
        subtotal_cents=bd.items_cents, tax_cents=bd.tax_cents,
        service_charge_cents=bd.service_charge_cents, discount_cents=bd.discount_cents,
        surcharge_cents=bd.card_surcharge_cents, tip_cents=0, currency="CAD",
        item_ids=[sc.item_id], idempotency_key=key)
    pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id=checkout)
    return a, bd.expected_total_cents


def _approved(base, *, cur="CAD", tip=200, pid="sqpay1", checkout="chk1"):
    return ChargeResult(status=S.PROCESSOR_APPROVED, provider_checkout_id=checkout,
                        provider_payment_id=pid, processor_amount_cents=base,
                        processor_currency=cur, tip_cents=tip, card_brand="Visa", card_last4="4242")


def _advance(db, sc, a, result, is_partial=False):
    order = db.get(Order, sc.order_id)
    seat = db.get(Seat, sc.seat_id)
    return charge.advance_terminal_attempt(
        db, order, seat, a, result, staff_id=sc.staff_id, instrument_id=sc.card_id,
        card_surcharge_rate=0.0, is_partial=is_partial)


# --------------------------------------------------- settle / reconcile policy

def test_matching_evidence_settles_with_terminal_tip():
    SM, sc = _scenario()
    db = SM()
    a, expected = _pending_attempt(db, sc)
    out = _advance(db, sc, a, _approved(expected, tip=250))
    check(out["state"] == "done", "matching terminal evidence settles")
    p = db.get(Payment, out["payment_id"])
    check(p is not None and p.tip_cents == 250, "terminal-confirmed tip becomes the Payment tip (#5)")
    check(db.query(Payment).filter_by(order_id=sc.order_id).count() == 1, "exactly one Payment")
    fresh = db.get(PaymentAttempt, a.id)
    check(fresh.status == S.SETTLED and fresh.payment_id == p.id
          and fresh.provider_payment_id == "sqpay1", "attempt SETTLED, evidence + Payment linked")
    db.close()


def test_amount_mismatch_reconciles_no_payment():
    SM, sc = _scenario()
    db = SM()
    a, expected = _pending_attempt(db, sc)
    out = _advance(db, sc, a, _approved(expected + 500))   # processor base too high
    check(out["state"] == "reconciling", "amount mismatch reconciles (no settle)")
    check(db.query(Payment).filter_by(order_id=sc.order_id).count() == 0, "no Payment on amount mismatch")
    check(db.get(PaymentAttempt, a.id).status == S.REQUIRES_RECONCILIATION,
          "attempt parked REQUIRES_RECONCILIATION")
    db.close()


def test_currency_mismatch_reconciles_no_payment():
    SM, sc = _scenario()
    db = SM()
    a, expected = _pending_attempt(db, sc)
    out = _advance(db, sc, a, _approved(expected, cur="USD"))
    check(out["state"] == "reconciling", "currency mismatch reconciles")
    check(db.query(Payment).filter_by(order_id=sc.order_id).count() == 0, "no Payment on currency mismatch")
    db.close()


def test_reconcile_result_parks_and_keeps_evidence():
    SM, sc = _scenario()
    db = SM()
    a, _ = _pending_attempt(db, sc)
    # provider.poll maps ambiguous/incoherent outcomes to REQUIRES_RECONCILIATION,
    # sometimes with a payment id we must preserve.
    out = _advance(db, sc, a, ChargeResult(status=S.REQUIRES_RECONCILIATION,
                   provider_checkout_id="chk1", provider_payment_id="sqpay9",
                   error="COMPLETED but evidence incoherent"))
    check(out["state"] == "reconciling", "ambiguous poll outcome parks (#9)")
    fresh = db.get(PaymentAttempt, a.id)
    check(fresh.status == S.REQUIRES_RECONCILIATION and fresh.provider_payment_id == "sqpay9",
          "provider payment id preserved for reconciliation (#9)")
    check(db.query(Payment).filter_by(order_id=sc.order_id).count() == 0, "no auto-settle on ambiguity")
    db.close()


def test_order_drift_after_capture_reconciles():
    """Guardrail #7: the seat's item is paid out-of-band after the terminal captured;
    the local booking finds nothing outstanding, so the terminal attempt reconciles
    (external money moved) rather than rejecting — and books no second Payment."""
    SM, sc = _scenario()
    db = SM()
    a, expected = _pending_attempt(db, sc)
    # pay the item on another (manual) instrument first
    cash = PaymentInstrument(code="cash1", name="Cash", instrument_type="cash", provider="manual")
    db.add(cash); db.commit()
    order = db.get(Order, sc.order_id); seat = db.get(Seat, sc.seat_id)
    pay_seat(db, order, seat, instrument_id=cash.id, staff_id=sc.staff_id, item_ids=[sc.item_id])
    db.commit()
    n = db.query(Payment).filter_by(order_id=sc.order_id).count()
    out = _advance(db, sc, a, _approved(expected))
    check(out["state"] == "reconciling", "drift after capture reconciles (#7)")
    check(db.query(Payment).filter_by(order_id=sc.order_id).count() == n,
          "no second Payment booked on the already-paid item (#7)")
    check(db.get(PaymentAttempt, a.id).status == S.REQUIRES_RECONCILIATION, "attempt parked")
    db.close()


def test_resettle_is_idempotent():
    SM, sc = _scenario()
    db = SM()
    a, expected = _pending_attempt(db, sc)
    out1 = _advance(db, sc, a, _approved(expected))
    n = db.query(Payment).filter_by(order_id=sc.order_id).count()
    out2 = _advance(db, sc, a, _approved(expected))   # duplicate poll after settle
    check(out1["state"] == "done" and out2["state"] == "done", "re-poll after settle reports done")
    check(out2["payment_id"] == out1["payment_id"], "converges on the same Payment")
    check(db.query(Payment).filter_by(order_id=sc.order_id).count() == n, "no second Payment (#8)")
    db.close()


def test_tip_is_durable_and_recovers_after_crash():
    """v-fix #2: the terminal tip is committed as write-once processor evidence with the
    approval, so a crash before local settlement recovers the REAL tip (not zero). The
    APPROVED-with-evidence attempt is also on the recovery queue (#8)."""
    SM, sc = _scenario()
    db = SM()
    a, expected = _pending_attempt(db, sc)
    # Approval evidence — including the tip — commits; then the process "crashes"
    # before the local Payment is written.
    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="sqpayX",
                  processor_amount_cents=expected, processor_currency="CAD",
                  processor_tip_cents=250)
    aid = a.id
    check(any(x.id == aid for x in pa.requires_reconciliation(db)),
          "APPROVED-with-evidence attempt is on the recovery queue (#8)")
    db.close()

    # Restart: a fresh session re-polls and settles — from the DURABLE tip, not zero.
    db2 = SM()
    a2 = db2.get(PaymentAttempt, aid)
    check(a2.processor_tip_cents == 250, "tip persisted as durable evidence (#2)")
    out = _advance(db2, sc, a2, _approved(expected, tip=250))
    p = db2.get(Payment, out["payment_id"])
    check(out["state"] == "done" and p.tip_cents == 250,
          "recovery settles with the persisted tip, never inferred zero (#2)")
    check(db2.query(Payment).filter_by(order_id=sc.order_id).count() == 1, "exactly one Payment")
    db2.close()


def test_conflicting_tip_evidence_fails_closed():
    """v-fix #2: a re-poll whose tip disagrees with the already-recorded evidence must
    NOT overwrite it — it reconciles (fail closed)."""
    SM, sc = _scenario()
    db = SM()
    a, expected = _pending_attempt(db, sc)
    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="sqpayX",
                  processor_amount_cents=expected, processor_currency="CAD",
                  processor_tip_cents=250)
    out = _advance(db, sc, a, _approved(expected, tip=300))   # conflicting tip
    check(out["state"] == "reconciling", "conflicting tip evidence reconciles (fail closed)")
    check(db.get(PaymentAttempt, a.id).processor_tip_cents == 250, "recorded tip not overwritten")
    check(db.query(Payment).filter_by(order_id=sc.order_id).count() == 0, "no Payment on tip conflict")
    db.close()


def _drifted_component_factory(db, sc, *, items, discount, tax, service, surcharge, tip, total):
    def factory():
        p = Payment(order_id=sc.order_id, seat_id=sc.seat_id, instrument_id=sc.card_id,
                    staff_id=sc.staff_id, items_cents=items, discount_cents=discount,
                    tax_cents=tax, service_charge_cents=service, card_surcharge_cents=surcharge,
                    tip_cents=tip, total_cents=total)
        db.add(p)
        return p
    return factory


def test_component_drift_same_total_reconciles():
    """v-fix #3: external settlement compares EVERY pre-tip component, not just the
    total. A booking whose components drift (subtotal down, tax up) but whose pre-tip
    total stays equal must reconcile and write no Payment."""
    SM, sc = _scenario()
    db = SM()
    a, expected = _pending_attempt(db, sc)   # subtotal 1000, tax 50, pre-tip 1050
    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="sqpayD",
                  processor_amount_cents=expected, processor_currency="CAD",
                  processor_tip_cents=200)
    # pre-tip total unchanged (900 + 150 = 1050) but subtotal/tax drifted.
    f = _drifted_component_factory(db, sc, items=900, discount=0, tax=150, service=0,
                                   surcharge=0, tip=200, total=1250)
    res = settle.settle_charge(db, a, payment_factory=f, commit=True)
    check(not res.is_settled, "same total but drifted components -> reconcile (#3)")
    check(db.query(Payment).filter_by(order_id=sc.order_id).count() == 0, "no Payment on component drift")
    check(db.get(PaymentAttempt, a.id).status == S.REQUIRES_RECONCILIATION, "attempt parked")
    db.close()


def test_scoped_lookup_rejects_other_seat():
    """v-fix #4: the terminal lookup is scoped to order+seat+provider, so a checkout id
    can never resolve another seat's/order's attempt."""
    from app.routers.pay import _terminal_attempt
    SM, sc = _scenario()
    db = SM()
    a, _ = _pending_attempt(db, sc, checkout="chkX")
    check(_terminal_attempt(db, "chkX", sc.order_id, sc.seat_id) is not None,
          "correct order+seat resolves the attempt")
    check(_terminal_attempt(db, "chkX", sc.order_id, sc.seat_id + 99999) is None,
          "a different seat does not resolve it (#4)")
    check(_terminal_attempt(db, "chkX", sc.order_id + 99999, sc.seat_id) is None,
          "a different order does not resolve it (#4)")
    db.close()


def test_transport_pending_past_deadline_escalates():
    """v-fix #6: a poll still PENDING past the terminal deadline stops polling forever
    and escalates to reconciliation."""
    from datetime import datetime, timedelta
    SM, sc = _scenario()
    db = SM()
    a, _ = _pending_attempt(db, sc)
    a.created_at = datetime.now() - timedelta(seconds=charge.TERMINAL_PENDING_DEADLINE_S + 30)
    db.commit()
    out = _advance(db, sc, a, ChargeResult(status=S.PROCESSOR_PENDING,
                   provider_checkout_id="chk1", error="transient lookup error"))
    check(out["state"] == "reconciling", "pending past deadline escalates to reconciliation (#6)")
    check(db.get(PaymentAttempt, a.id).status == S.REQUIRES_RECONCILIATION, "attempt parked")
    db.close()


def test_stale_cancel_cannot_overwrite_approval():
    """v-fix #5: a cancel that loses the CAS race to a concurrent approval must not
    overwrite the approval evidence."""
    SM, sc = _scenario()
    setup = SM()
    a, expected = _pending_attempt(setup, sc)
    aid = a.id
    setup.close()
    # Session B loads the attempt while it is still PENDING (a cancel in flight).
    dbb = SM()
    ab = dbb.get(PaymentAttempt, aid)
    # Session A approves + settles first.
    da = SM()
    aa = da.get(PaymentAttempt, aid)
    _advance(da, sc, aa, _approved(expected, tip=200))
    da.close()
    # B's stale cancel must be refused (CAS race lost, or a re-read terminal state) and
    # must NOT overwrite the approval evidence.
    raised = False
    try:
        pa.transition(dbb, ab, S.CANCELLED, last_error="stale cancel")
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "stale cancel is refused with a typed error (#5)")
    verify = SM()
    fresh = verify.get(PaymentAttempt, aid)
    check(fresh.status == S.SETTLED and fresh.provider_payment_id == "sqpay1",
          "approval/settlement evidence is intact after the stale cancel (#5)")
    verify.close(); dbb.close()


def test_phase1_snapshot_reflects_committed_state():
    """v-fix #1: the phase-1 snapshot is taken under the order lock from CURRENT
    committed state — if the seat was paid just before, start finds nothing to charge
    rather than pricing a stale balance."""
    SM, sc = _scenario()
    db = SM()
    cash = PaymentInstrument(code="cash2", name="Cash", instrument_type="cash", provider="manual")
    db.add(cash); db.commit()
    order = db.get(Order, sc.order_id); seat = db.get(Seat, sc.seat_id)
    pay_seat(db, order, seat, instrument_id=cash.id, staff_id=sc.staff_id, item_ids=[sc.item_id])
    db.commit()
    raised = False
    try:
        charge.start_terminal_attempt(
            db, order, seat, staff_id=sc.staff_id, instrument_id=sc.card_id,
            item_ids=[sc.item_id], service_charge_rate=0.0, card_surcharge_rate=0.0,
            idempotency_key="p1")
    except PaymentError:
        raised = True
    check(raised, "start prices from current locked state, not a stale balance (#1)")
    check(db.query(PaymentAttempt).filter_by(idempotency_key="p1").count() == 0,
          "no terminal attempt created when nothing is outstanding")
    db.close()


def test_canceled_result_cancels_attempt():
    SM, sc = _scenario()
    db = SM()
    a, _ = _pending_attempt(db, sc)
    out = _advance(db, sc, a, ChargeResult(status=S.CANCELLED, provider_checkout_id="chk1"))
    check(out["state"] == "canceled", "canceled terminal reports canceled")
    check(db.get(PaymentAttempt, a.id).status == S.CANCELLED, "attempt CANCELLED")
    check(db.query(Payment).filter_by(order_id=sc.order_id).count() == 0, "no Payment on cancel")
    db.close()


# ---------------------------------------------- start: attempt before Square (#1/#2)

def test_start_commits_attempt_before_square_and_is_idempotent():
    SM, sc = _scenario()
    calls = {"n": 0}
    orig = square.create_checkout

    def fake_checkout(amount_cents, **kw):
        calls["n"] += 1
        # Prove the durable attempt is already committed before Square is contacted
        # (#1): read it in a brand-new session.
        probe = SM()
        seen = probe.query(PaymentAttempt).filter_by(idempotency_key=kw.get("idempotency_key")).count()
        probe.close()
        calls["committed_before_call"] = seen == 1
        return {"id": "chk_started"}

    square.create_checkout = fake_checkout
    try:
        db = SM()
        order = db.get(Order, sc.order_id); seat = db.get(Seat, sc.seat_id)
        a1, r1 = charge.start_terminal_attempt(
            db, order, seat, staff_id=sc.staff_id, instrument_id=sc.card_id,
            item_ids=[sc.item_id], service_charge_rate=0.0, card_surcharge_rate=0.0,
            idempotency_key="tok-term")
        check(calls["committed_before_call"], "durable attempt committed before the Square call (#1)")
        check(r1.status == S.PROCESSOR_PENDING and a1.provider_checkout_id == "chk_started",
              "start moves the attempt to PROCESSOR_PENDING with the checkout id")
        # Double-submit with the same token: no second Square checkout, same attempt.
        a2, r2 = charge.start_terminal_attempt(
            db, order, seat, staff_id=sc.staff_id, instrument_id=sc.card_id,
            item_ids=[sc.item_id], service_charge_rate=0.0, card_surcharge_rate=0.0,
            idempotency_key="tok-term")
        check(calls["n"] == 1, "duplicate submit does not open a second Square checkout (#2)")
        check(a2.id == a1.id, "duplicate submit reuses the one durable attempt (#2)")
        check(db.query(PaymentAttempt).filter_by(idempotency_key="tok-term").count() == 1,
              "exactly one attempt row for the token")
        db.close()
    finally:
        square.create_checkout = orig


# --------------------------------------------------------------- Postgres only

def test_concurrent_terminal_settle_one_payment():
    """Two concurrent settlements of the same APPROVED terminal attempt produce one
    Payment (CAS converge). Postgres only."""
    if not pg_dsn():
        check(True, "SKIP concurrent (needs Postgres)")
        return
    SM, sc = _scenario()
    setup = SM()
    a, expected = _pending_attempt(setup, sc)
    pa.transition(setup, a, S.PROCESSOR_APPROVED, provider_payment_id="sqpayC",
                  processor_amount_cents=expected, processor_currency="CAD",
                  processor_tip_cents=200)
    aid = a.id
    setup.close()

    da, dbb = SM(), SM()
    aa = da.get(PaymentAttempt, aid)
    ab = dbb.get(PaymentAttempt, aid)
    o_a, s_a = da.get(Order, sc.order_id), da.get(Seat, sc.seat_id)
    o_b, s_b = dbb.get(Order, sc.order_id), dbb.get(Seat, sc.seat_id)
    r = _approved(expected)
    out_a = charge.advance_terminal_attempt(da, o_a, s_a, aa, r, staff_id=sc.staff_id,
                                            instrument_id=sc.card_id, card_surcharge_rate=0.0)
    out_b = charge.advance_terminal_attempt(dbb, o_b, s_b, ab, r, staff_id=sc.staff_id,
                                            instrument_id=sc.card_id, card_surcharge_rate=0.0)
    check(out_a["state"] == "done" and out_b["state"] == "done", "both settlements converge on done")
    verify = SM()
    check(verify.query(Payment).filter_by(order_id=sc.order_id).count() == 1,
          "exactly one Payment across concurrent terminal settlements")
    verify.close(); da.close(); dbb.close()


def test_phase1_lock_released_before_square():
    """v-fix #1 / guardrail #3: prove the phase-1 order lock is RELEASED before any
    Square I/O — a FOR UPDATE NOWAIT probe fired from inside create_checkout finds the
    order row free. Postgres only (NOWAIT semantics)."""
    if not pg_dsn():
        check(True, "SKIP lock-release probe (needs Postgres)")
        return
    from sqlalchemy import select as _select
    SM, sc = _scenario()
    orig = square.create_checkout
    probe = {}

    def fake_checkout(amount_cents, **kw):
        p = SM()
        try:
            p.execute(_select(Order.id).where(Order.id == sc.order_id).with_for_update(nowait=True)).first()
            probe["lock_free"] = True
        except Exception:
            probe["lock_free"] = False
        finally:
            p.rollback(); p.close()
        return {"id": "chk_lockprobe"}

    square.create_checkout = fake_checkout
    try:
        db = SM()
        order = db.get(Order, sc.order_id); seat = db.get(Seat, sc.seat_id)
        charge.start_terminal_attempt(
            db, order, seat, staff_id=sc.staff_id, instrument_id=sc.card_id,
            item_ids=[sc.item_id], service_charge_rate=0.0, card_surcharge_rate=0.0,
            idempotency_key="lockprobe")
        db.close()
    finally:
        square.create_checkout = orig
    check(probe.get("lock_free") is True,
          "phase-1 order lock is released before the Square call (#1/#3)")


if __name__ == "__main__":
    for fn in (test_matching_evidence_settles_with_terminal_tip,
               test_amount_mismatch_reconciles_no_payment,
               test_currency_mismatch_reconciles_no_payment,
               test_reconcile_result_parks_and_keeps_evidence,
               test_order_drift_after_capture_reconciles,
               test_resettle_is_idempotent,
               test_tip_is_durable_and_recovers_after_crash,
               test_conflicting_tip_evidence_fails_closed,
               test_component_drift_same_total_reconciles,
               test_scoped_lookup_rejects_other_seat,
               test_transport_pending_past_deadline_escalates,
               test_stale_cancel_cannot_overwrite_approval,
               test_phase1_snapshot_reflects_committed_state,
               test_canceled_result_cancels_attempt,
               test_start_commits_attempt_before_square_and_is_idempotent,
               test_phase1_lock_released_before_square,
               test_concurrent_terminal_settle_one_payment):
        print(f"- {fn.__name__}")
        fn()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall terminal tests passed")
