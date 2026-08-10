"""Stage 2a (hardened) — durable PaymentAttempt + concurrency-safe state machine.

Runs against SQLite-with-FK by default, or Postgres if PG_TEST_DSN is set. Uses a
real parent graph (finding #16). Concurrency proofs live in test_pg_concurrency.py.
Run: python tests/test_payment_attempts.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._pay_fixture import new_db as _db

from app.models.oltp import PaymentAttempt, PaymentAttemptStatus as S
from app.services import payment_attempts as pa

_failures = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def _mk(db, ids, key=None, total=1000, provider="manual", order_id=None):
    return pa.create_attempt(
        db, provider=provider, order_id=order_id or ids["order_id"],
        staff_id=ids["staff_id"], expected_total_cents=total,
        subtotal_cents=total, idempotency_key=key,
    )


def test_provider_required_and_validated():
    db, ids = _db()
    raised = False
    try:
        pa.create_attempt(db, provider="nope", order_id=ids["order_id"],
                          staff_id=ids["staff_id"], expected_total_cents=100)
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "unregistered provider is rejected (no silent default)")


def test_idempotent_create_same_intent():
    db, ids = _db()
    a = _mk(db, ids, key="abc")
    b = _mk(db, ids, key="abc")
    check(a.id == b.id, "same key + same intent returns the same attempt")
    check(db.query(PaymentAttempt).count() == 1, "no duplicate row")


def test_same_key_different_intent_conflicts():
    db, ids = _db()
    _mk(db, ids, key="dup", total=1000)
    raised = False
    try:
        _mk(db, ids, key="dup", total=9999)  # different amount => different intent
    except pa.IdempotencyConflict:
        raised = True
    check(raised, "same key + different intent raises IdempotencyConflict")


def test_legal_settlement_path():
    db, ids = _db()
    a = _mk(db, ids)
    pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id="chk_1")
    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="pay_1")
    pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"])
    check(a.status == S.SETTLED, "created->pending->approved->settled")
    check(a.provider_payment_id == "pay_1", "provider id persisted")
    check(a.payment_id == ids["payment_id"], "settled attempt links its Payment")


def test_illegal_and_terminal():
    db, ids = _db()
    a = _mk(db, ids)
    raised = False
    try:
        pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"])
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "created->settled rejected (no processor outcome)")
    b = _mk(db, ids, key="k2")
    pa.transition(db, b, S.FAILED, last_error="declined")
    raised2 = False
    try:
        pa.transition(db, b, S.PROCESSOR_PENDING)
    except pa.PaymentAttemptError:
        raised2 = True
    check(raised2, "FAILED is terminal, cannot reopen")


def test_settle_requires_payment_id():
    db, ids = _db()
    a = _mk(db, ids)
    pa.transition(db, a, S.PROCESSOR_PENDING)
    pa.transition(db, a, S.PROCESSOR_APPROVED)
    raised = False
    try:
        pa.transition(db, a, S.SETTLED)
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "settling without payment_id is rejected")


def test_write_once_provider_id_via_cas():
    db, ids = _db()
    a = _mk(db, ids)
    pa.transition(db, a, S.PROCESSOR_PENDING, provider_checkout_id="chk_1")
    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_checkout_id="chk_1")  # same ok
    raised = False
    try:
        pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"],
                      provider_checkout_id="chk_2")  # different -> conflict
    except pa.TransitionConflict:
        raised = True
    check(raised, "a set provider id cannot be overwritten with a different value")


def test_snapshot_immutable_across_transitions():
    db, ids = _db()
    a = _mk(db, ids, total=1234)
    snap = (a.subtotal_cents, a.expected_total_cents)
    pa.transition(db, a, S.PROCESSOR_PENDING)
    pa.transition(db, a, S.PROCESSOR_APPROVED)
    pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"])
    check(snap == (a.subtotal_cents, a.expected_total_cents),
          "amount snapshot unchanged by transitions (service contract)")


def test_processor_evidence_is_write_once():
    db, ids = _db()
    a = _mk(db, ids)
    pa.transition(db, a, S.PROCESSOR_PENDING)
    pa.transition(db, a, S.PROCESSOR_APPROVED, provider_payment_id="p1",
                  processor_amount_cents=1000, processor_currency="CAD")
    # Same value on the next transition is fine (idempotent).
    pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"],
                  processor_amount_cents=1000)
    check(a.processor_amount_cents == 1000 and a.processor_currency == "CAD",
          "processor evidence persisted")
    # A *different* processor amount must not overwrite the evidence. Use a
    # separate attempt and a payment_id-free transition so the conflict can only
    # come from the write-once evidence guard, not the unique payment_id.
    b = _mk(db, ids, key="k2")
    pa.transition(db, b, S.PROCESSOR_PENDING)
    pa.transition(db, b, S.PROCESSOR_APPROVED, processor_amount_cents=1000, processor_currency="CAD")
    raised = False
    try:
        pa.transition(db, b, S.REQUIRES_RECONCILIATION, processor_amount_cents=9999)
    except pa.TransitionConflict:
        raised = True
    check(raised, "processor amount evidence cannot be overwritten with a different value (#8)")


def test_reconciliation_needs_evidence():
    db, ids = _db()
    a = _mk(db, ids)
    pa.transition(db, a, S.REQUIRES_RECONCILIATION, last_error="lost")
    # plain transition out is blocked
    raised = False
    try:
        pa.transition(db, a, S.SETTLED, payment_id=ids["payment_id"])
    except pa.PaymentAttemptError:
        raised = True
    check(raised, "plain transition cannot leave REQUIRES_RECONCILIATION")
    # resolution requires evidence
    raised2 = False
    try:
        pa.resolve_reconciliation(db, a, resolved_status=S.SETTLED,
                                  resolved_by="", note="", payment_id=ids["payment_id"])
    except pa.PaymentAttemptError:
        raised2 = True
    check(raised2, "resolution without resolved_by/note is rejected")
    pa.resolve_reconciliation(db, a, resolved_status=S.SETTLED, resolved_by="mgr",
                              note="looked up in Square dashboard", payment_id=ids["payment_id"])
    check(a.status == S.SETTLED and a.reconciled_by == "mgr" and a.reconciliation_note,
          "resolution with evidence settles and records who/why")


if __name__ == "__main__":
    for fn in (
        test_provider_required_and_validated,
        test_idempotent_create_same_intent,
        test_same_key_different_intent_conflicts,
        test_legal_settlement_path,
        test_illegal_and_terminal,
        test_settle_requires_payment_id,
        test_write_once_provider_id_via_cas,
        test_snapshot_immutable_across_transitions,
        test_processor_evidence_is_write_once,
        test_reconciliation_needs_evidence,
    ):
        print(f"- {fn.__name__}")
        fn()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall payment-attempt tests passed")
