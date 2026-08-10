"""Stage 2a regression tests — durable PaymentAttempt + state machine.

Covers audit findings #1–#5 at the record level: idempotent creation, legal-only
state transitions, one-Payment-per-attempt, write-once provider identifiers, and
an immutable payable snapshot. Fully self-contained (in-memory SQLite, no seed,
no server), addressing #37.  Run: python tests/test_payment_attempts.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import oltp  # noqa: F401  registers all tables on Base.metadata
from app.models.oltp import PaymentAttempt, PaymentAttemptStatus as S
from app.services import payment_attempts as pa

_failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL {label}")


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _mk(db, key=None, total=1000):
    return pa.create_attempt(
        db, order_id=1, staff_id=1, seat_id=1, expected_total_cents=total,
        subtotal_cents=900, tax_cents=100, tip_cents=0, currency="CAD",
        idempotency_key=key,
    )


def test_idempotent_create():
    db = _session()
    a = _mk(db, key="abc")
    b = _mk(db, key="abc")
    check(a.id == b.id, "same idempotency key returns the same attempt")
    n = db.query(PaymentAttempt).count()
    check(n == 1, "no duplicate attempt row for a repeated key")
    c = _mk(db)  # auto key
    d = _mk(db)  # auto key
    check(c.id != d.id and c.idempotency_key != d.idempotency_key,
          "auto-generated keys are unique")


def test_legal_settlement_path():
    db = _session()
    a = _mk(db)
    pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id="chk_1")
    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="pay_1")
    pa.transition(db, a, S.SETTLED, payment_id=42)
    check(a.status == S.SETTLED, "created -> pending -> approved -> settled")
    check(a.provider_checkout_id == "chk_1" and a.provider_payment_id == "pay_1",
          "provider identifiers persisted")
    check(a.payment_id == 42, "settled attempt links its Payment")


def test_illegal_transitions_rejected():
    db = _session()
    a = _mk(db)
    raised = False
    try:
        pa.transition(db, a, S.SETTLED, payment_id=1)  # skips processor states
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "created -> settled is rejected (no processor outcome)")
    check(a.status == S.CREATED, "rejected transition leaves status unchanged")

    # terminal state cannot be reopened
    b = _mk(db)
    pa.transition(db, b, S.FAILED, last_error="declined")
    raised2 = False
    try:
        pa.transition(db, b, S.PROCESSOR_PENDING)
    except pa.PaymentAttemptError:
        raised2 = True
    check(raised2, "a FAILED attempt cannot be reopened")


def test_settle_requires_payment_id():
    db = _session()
    a = _mk(db)
    pa.transition(db, a, S.PROCESSOR_PENDING)
    pa.transition(db, a, S.PROCESSOR_APPROVED)
    raised = False
    try:
        pa.transition(db, a, S.SETTLED)  # no payment_id
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "settling without a payment_id is rejected")


def test_provider_id_write_once():
    db = _session()
    a = _mk(db)
    pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id="chk_1")
    # same value again is fine
    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_checkout_id="chk_1")
    raised = False
    try:
        # different value must be refused
        pa.transition(db, a, S.SETTLED, payment_id=1, provider_checkout_id="chk_2")
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "a set provider id cannot be overwritten with a different value")


def test_snapshot_is_immutable_across_transitions():
    db = _session()
    a = _mk(db, total=1234)
    snap = (a.subtotal_cents, a.tax_cents, a.tip_cents, a.expected_total_cents)
    pa.transition(db, a, S.PROCESSOR_PENDING)
    pa.transition(db, a, S.PROCESSOR_APPROVED)
    pa.transition(db, a, S.SETTLED, payment_id=7)
    now = (a.subtotal_cents, a.tax_cents, a.tip_cents, a.expected_total_cents)
    check(snap == now, "amount snapshot is unchanged by transitions")


def test_one_payment_per_attempt_db_constraint():
    db = _session()
    a = _mk(db, key="k1")
    b = _mk(db, key="k2")
    for x in (a, b):
        pa.transition(db, x, S.PROCESSOR_PENDING)
        pa.transition(db, x, S.PROCESSOR_APPROVED)
    pa.transition(db, a, S.SETTLED, payment_id=100)
    raised = False
    try:
        # two attempts cannot claim the same Payment row
        b.payment_id = 100
        db.commit()
    except IntegrityError:
        raised = True
        db.rollback()
    check(raised, "DB unique constraint blocks two attempts on one Payment")


def test_reconciliation_queue():
    db = _session()
    settled = _mk(db, key="s")
    pa.transition(db, settled, S.PROCESSOR_PENDING)
    pa.transition(db, settled, S.PROCESSOR_APPROVED)
    pa.transition(db, settled, S.SETTLED, payment_id=1)
    approved = _mk(db, key="a")  # Square said yes, never settled locally
    pa.transition(db, approved, S.PROCESSOR_PENDING)
    pa.transition(db, approved, S.PROCESSOR_APPROVED)
    stuck = pa.requires_reconciliation(db)
    ids = {x.id for x in stuck}
    check(approved.id in ids, "approved-but-unsettled attempt needs reconciliation")
    check(settled.id not in ids, "a settled attempt is not in the queue")


if __name__ == "__main__":
    test_idempotent_create()
    test_legal_settlement_path()
    test_illegal_transitions_rejected()
    test_settle_requires_payment_id()
    test_provider_id_write_once()
    test_snapshot_is_immutable_across_transitions()
    test_one_payment_per_attempt_db_constraint()
    test_reconciliation_queue()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall payment-attempt tests passed")
