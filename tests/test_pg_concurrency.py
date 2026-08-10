"""PostgreSQL concurrency proofs for the payment core (findings #16, #17).

These exercise real row locking, unique races, and FK enforcement that SQLite
cannot prove. They SKIP (exit 0) unless PG_TEST_DSN points at a disposable
Postgres, e.g.:

    PG_TEST_DSN=postgresql+psycopg://rms:rms@localhost:5433/rms_test \
        python tests/test_pg_concurrency.py

Run: python tests/test_pg_concurrency.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._pay_fixture import Session, fresh_schema, make_engine, pg_dsn, seed_parents

from app.models.oltp import Payment, PaymentAttempt, PaymentAttemptStatus as S, RefundAttempt
from app.services import payment_attempts as pa
from app.services import refund_attempts as ra
from app.services import settlement as settle

_failures = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def _run_concurrently(fn, n):
    """Run fn(i) in n threads, collect (result, exception) per thread."""
    out = [None] * n
    barrier = threading.Barrier(n)

    def wrap(i):
        barrier.wait()  # maximise real contention
        try:
            out[i] = ("ok", fn(i))
        except Exception as exc:  # noqa: BLE001
            out[i] = ("err", exc)

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_concurrent_same_key_create(engine, ids):
    Sess = Session(engine)

    def make(_i):
        db = Sess()
        try:
            a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                                  staff_id=ids["staff_id"], expected_total_cents=1000,
                                  subtotal_cents=1000, idempotency_key="concurrent-key")
            return a.id
        finally:
            db.close()

    results = _run_concurrently(make, 8)
    ids_returned = {r[1] for r in results if r[0] == "ok"}
    errs = [r[1] for r in results if r[0] == "err"]
    check(not errs, f"no unhandled errors under concurrent same-key create ({errs[:1]})")
    check(len(ids_returned) == 1, "all concurrent callers resolve to one attempt id")
    verify = Sess()
    n = verify.query(PaymentAttempt).filter_by(idempotency_key="concurrent-key").count()
    verify.close()
    check(n == 1, "exactly one PaymentAttempt row persisted")


def test_concurrent_conflicting_transition(engine, ids):
    Sess = Session(engine)
    db0 = Sess()
    a = pa.create_attempt(db0, provider="manual", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          subtotal_cents=1000, idempotency_key="cas-key")
    pa.transition(db0, a, S.PROCESSOR_PENDING)
    aid = a.id
    db0.close()

    targets = [S.PROCESSOR_APPROVED, S.CANCELLED, S.FAILED]

    def move(i):
        db = Sess()
        try:
            att = db.get(PaymentAttempt, aid)
            kwargs = {"payment_id": ids["payment_id"]} if targets[i] == S.PROCESSOR_APPROVED else {}
            # approve needs no payment_id; use provider_payment_id to differentiate
            if targets[i] == S.PROCESSOR_APPROVED:
                kwargs = {"provider_payment_id": "pay_conc"}
            pa.transition(db, att, targets[i], **kwargs)
            return targets[i]
        finally:
            db.close()

    results = _run_concurrently(move, 3)
    wins = [r[1] for r in results if r[0] == "ok"]
    # Losers refuse explicitly. Two legitimate, timing-dependent flavors, both
    # subclasses of PaymentAttemptError and both meaning "did not apply":
    #  - TransitionConflict: the CAS UPDATE lost the race (rowcount 0), or
    #  - illegal-transition: the loser re-read the row *after* the winner
    #    committed and saw a now-terminal status.
    refusals = [r[1] for r in results
                if r[0] == "err" and isinstance(r[1], pa.PaymentAttemptError)]
    check(len(wins) == 1, f"exactly one transition wins ({wins})")
    check(len(refusals) == 2, "both losers refuse with an explicit typed error")
    # No corruption: the persisted status is exactly the single winner's target.
    verify = Session(engine)()
    final = verify.get(PaymentAttempt, aid).status
    verify.close()
    check(final == wins[0], "the persisted status is exactly the one winner's target")


def test_concurrent_refund_same_key(engine, ids):
    Sess = Session(engine)

    def make(_i):
        db = Sess()
        try:
            r = ra.create_refund_attempt(db, payment_id=ids["payment_id"],
                                         staff_id=ids["staff_id"], provider="manual",
                                         amount_cents=1500, idempotency_key="refund-key")
            return r.id
        finally:
            db.close()

    results = _run_concurrently(make, 8)
    ids_ret = {r[1] for r in results if r[0] == "ok"}
    errs = [r[1] for r in results if r[0] == "err"]
    check(not errs, f"no unhandled errors under concurrent same-key refund ({errs[:1]})")
    check(len(ids_ret) == 1, "all concurrent refund callers resolve to one attempt")
    verify = Sess()
    n = verify.query(RefundAttempt).filter_by(idempotency_key="refund-key").count()
    verify.close()
    check(n == 1, "exactly one RefundAttempt row persisted (#16)")


def test_concurrent_refund_reconciliation(engine, ids):
    from app.models.oltp import RefundAttemptStatus as R
    Sess = Session(engine)
    db0 = Sess()
    r = ra.create_refund_attempt(db0, payment_id=ids["payment_id"], staff_id=ids["staff_id"],
                                 provider="manual", amount_cents=500, idempotency_key="rr")
    ra.transition_refund(db0, r, R.REQUIRES_RECONCILIATION)
    rid = r.id
    db0.close()

    def resolve(_i):
        db = Sess()
        try:
            ref = db.get(RefundAttempt, rid)
            ra.resolve_refund_reconciliation(db, ref, resolved_status=R.COMPLETED,
                                             note="auto", automatic=True, provider_evidence="ev")
            return "ok"
        finally:
            db.close()

    results = _run_concurrently(resolve, 6)
    wins = [r for r in results if r[0] == "ok"]
    refusals = [r for r in results if r[0] == "err" and isinstance(r[1], pa.PaymentAttemptError)]
    check(len(wins) == 1, f"exactly one refund reconciliation wins ({len(wins)})")
    check(len(refusals) == 5, "the losers refuse with an explicit typed error (#1)")


def _approved_ext(db, ids, key):
    a = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          subtotal_cents=1000, currency="CAD", idempotency_key=key)
    pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id="chk_" + key)
    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="pay_" + key,
                  processor_amount_cents=1000, processor_currency="CAD")
    return a


def _pay_factory(db, ids):
    def make():
        p = Payment(order_id=ids["order_id"], instrument_id=ids["instrument_id"],
                    staff_id=ids["staff_id"], total_cents=1000)
        db.add(p)
        return p
    return make


def test_settlement_cas_converges_on_winner(engine, ids):
    """A CAS-losing settle converges ONLY on a real SETTLED winner (#2)."""
    Sess = Session(engine)
    sa, sb = Sess(), Sess()
    a = _approved_ext(sa, ids, "cw")
    aid = a.id
    b = sb.get(PaymentAttempt, aid)                 # B loads the APPROVED attempt
    pay_a = settle.settle_charge(sa, a, payment_factory=_pay_factory(sa, ids)).payment
    sa.commit()                                     # A settles first
    res_b = settle.settle_charge(sb, b, payment_factory=_pay_factory(sb, ids))
    check(res_b.is_settled and res_b.payment.id == pay_a.id,
          "CAS loser converges on the real winner's Payment (#2)")
    sa.close(); sb.close()


def test_settlement_cas_reraises_on_non_winner(engine, ids):
    """A CAS loss to a NON-settled state (e.g. reconciliation) re-raises rather
    than reporting false success (#2)."""
    Sess = Session(engine)
    sa, sb = Sess(), Sess()
    a = _approved_ext(sa, ids, "cr")
    aid = a.id
    b = sb.get(PaymentAttempt, aid)
    pa.transition(sa, a, S.REQUIRES_RECONCILIATION, last_error="lost")
    sa.commit()                                     # A parks it, does not settle
    raised = False
    try:
        settle.settle_charge(sb, b, payment_factory=_pay_factory(sb, ids))
    except pa.TransitionConflict:
        raised = True
    check(raised, "CAS loss to a non-settled state re-raises, not false success (#2)")
    sa.close(); sb.close()


def test_provider_payment_id_unique(engine, ids):
    Sess = Session(engine)
    db = Sess()
    a = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          subtotal_cents=1000, idempotency_key="u1")
    b = pa.create_attempt(db, provider="square_terminal", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          subtotal_cents=1000, idempotency_key="u2")
    pa.transition(db, a, S.PROCESSOR_PENDING, provider_payment_id="DUP")
    raised = False
    try:
        pa.transition(db, b, S.PROCESSOR_PENDING, provider_payment_id="DUP")
    except pa.TransitionConflict:
        raised = True
    db.close()
    check(raised, "two attempts cannot share one provider_payment_id (#5)")


def test_fk_enforced(engine, ids):
    Sess = Session(engine)
    db = Sess()
    raised = False
    try:
        pa.create_attempt(db, provider="manual", order_id=999999,  # no such order
                          staff_id=ids["staff_id"], expected_total_cents=100,
                          subtotal_cents=100, idempotency_key="fk")
    except Exception:  # IntegrityError (FK) — Postgres enforces it
        raised = True
        db.rollback()
    db.close()
    check(raised, "FK to a non-existent order is rejected by Postgres")


def test_one_settlement_per_attempt(engine, ids):
    Sess = Session(engine)
    db = Sess()
    a = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          subtotal_cents=1000, idempotency_key="settle1")
    pa.transition(db, a, S.PROCESSOR_PENDING)
    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="p1")
    pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"])
    # second attempt tries to settle to the SAME payment id
    b = pa.create_attempt(db, provider="manual", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=1000,
                          subtotal_cents=1000, idempotency_key="settle2")
    pa.transition(db, b, S.PROCESSOR_PENDING)
    pa.transition(db, b, S.PROCESSOR_APPROVED, provider_payment_id="p2")
    raised = False
    try:
        pa.transition(db, b, S.SETTLED, payment_id=ids["payment_id"])
    except pa.TransitionConflict:
        raised = True
    db.close()
    check(raised, "one Payment can back at most one settled attempt")


if __name__ == "__main__":
    if not pg_dsn():
        print("SKIP: PG_TEST_DSN not set (Postgres concurrency tests not run)")
        sys.exit(0)
    print(f"Postgres: {pg_dsn()}")
    tests = [
        test_concurrent_same_key_create,
        test_concurrent_conflicting_transition,
        test_concurrent_refund_same_key,
        test_concurrent_refund_reconciliation,
        test_provider_payment_id_unique,
        test_fk_enforced,
        test_one_settlement_per_attempt,
        test_settlement_cas_converges_on_winner,
        test_settlement_cas_reraises_on_non_winner,
    ]
    for fn in tests:
        print(f"- {fn.__name__}")
        engine, _ = make_engine()
        fresh_schema(engine)          # clean slate per test for isolation
        s = Session(engine)()
        ids = seed_parents(s)
        s.close()
        fn(engine, ids)
        engine.dispose()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall Postgres concurrency tests passed")
