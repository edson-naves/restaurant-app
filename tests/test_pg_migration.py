"""PostgreSQL migration-UPGRADE proofs (findings #1, #2, #14, #15).

Unlike the other suites (drop_all + create_all = fresh schema), this builds the
*previous* Stage-2a `payment_attempt` shape, inserts representative rows, runs the
real migration, and asserts the upgraded schema + preserved data. Skips (exit 0)
without PG_TEST_DSN — upgrade behavior is Postgres-specific.

Run: PG_TEST_DSN=... python tests/test_pg_migration.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._pay_fixture import pg_dsn

from sqlalchemy import create_engine, text
from app import migrate

_failures = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


# The Stage-2a payment_attempt, before this round's hardening: provider is
# VARCHAR(20) DEFAULT 'square', provider_refund_id still present, and the
# provider-scoped UNIQUE constraints do NOT exist yet.
LEGACY_DDL = """
CREATE TABLE payment_attempt (
  id SERIAL PRIMARY KEY,
  order_id INTEGER NOT NULL,
  seat_id INTEGER,
  staff_id INTEGER NOT NULL,
  provider VARCHAR(20) NOT NULL DEFAULT 'square',
  provider_checkout_id VARCHAR(64),
  provider_payment_id VARCHAR(64),
  provider_refund_id VARCHAR(64),
  idempotency_key VARCHAR(64) NOT NULL,
  subtotal_cents INTEGER NOT NULL DEFAULT 0,
  tax_cents INTEGER NOT NULL DEFAULT 0,
  tip_cents INTEGER NOT NULL DEFAULT 0,
  service_charge_cents INTEGER NOT NULL DEFAULT 0,
  discount_cents INTEGER NOT NULL DEFAULT 0,
  surcharge_cents INTEGER NOT NULL DEFAULT 0,
  expected_total_cents INTEGER NOT NULL DEFAULT 0,
  currency VARCHAR(3) NOT NULL DEFAULT 'CAD',
  status VARCHAR(30) NOT NULL DEFAULT 'created',
  last_error TEXT NOT NULL DEFAULT '',
  payment_id INTEGER,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT uq_attempt_idempotency_key UNIQUE (idempotency_key),
  CONSTRAINT uq_attempt_payment UNIQUE (payment_id)
);
"""


def _build_legacy(engine, rows):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS refund_attempt CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS payment_attempt CASCADE"))
        conn.execute(text(LEGACY_DDL))
        for r in rows:
            conn.execute(text(
                "INSERT INTO payment_attempt (order_id, staff_id, provider, "
                "provider_payment_id, provider_checkout_id, idempotency_key, "
                "expected_total_cents, status) VALUES (:o,:s,:p,:pp,:pc,:k,:t,:st)"), r)


def _constraints(engine):
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(text(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_name='payment_attempt' AND table_schema=current_schema()"))}


def _provider_len(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name='payment_attempt' AND column_name='provider' "
            "AND table_schema=current_schema()")).scalar_one()


def _has_column(engine, col):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name='payment_attempt' "
            "AND column_name=:c AND table_schema=current_schema()"), {"c": col}).first() is not None


def test_clean_upgrade(engine):
    _build_legacy(engine, [
        {"o": 1, "s": 1, "p": "square", "pp": "PAY_A", "pc": "CHK_A", "k": "k1", "t": 4500, "st": "settled"},
        {"o": 2, "s": 1, "p": "square", "pp": "PAY_B", "pc": "CHK_B", "k": "k2", "t": 9500, "st": "created"},
        {"o": 3, "s": 1, "p": "square", "pp": None, "pc": None, "k": "k3", "t": 100, "st": "created"},
    ])
    applied = migrate.run(engine, strict=True)
    print("    migration applied:", [a for a in applied if "attempt" in a.lower() or "provider" in a.lower()][:8])

    cons = _constraints(engine)
    check("uq_attempt_provider_payment" in cons, "provider_payment UNIQUE constraint exists after upgrade")
    check("uq_attempt_provider_checkout" in cons, "provider_checkout UNIQUE constraint exists after upgrade")
    check(_provider_len(engine) == 30, "provider column widened to 30")
    check(_has_column(engine, "intent_fingerprint"), "new column intent_fingerprint added")
    check(_has_column(engine, "processor_currency"), "new column processor_currency added")
    check(not _has_column(engine, "provider_refund_id"), "retired provider_refund_id dropped (all-null)")

    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM payment_attempt")).scalar_one()
        sq = conn.execute(text("SELECT COUNT(*) FROM payment_attempt WHERE provider='square_terminal'")).scalar_one()
        keys = {r[0] for r in conn.execute(text("SELECT idempotency_key FROM payment_attempt"))}
    check(n == 3, "all rows preserved through migration")
    check(sq == 3, "provider backfilled 'square' -> 'square_terminal'")
    check(keys == {"k1", "k2", "k3"}, "financial identifiers preserved")


def test_upgrade_blocks_on_duplicate_payment_id(engine):
    _build_legacy(engine, [
        {"o": 1, "s": 1, "p": "square", "pp": "DUP", "pc": None, "k": "d1", "t": 100, "st": "created"},
        {"o": 2, "s": 1, "p": "square", "pp": "DUP", "pc": None, "k": "d2", "t": 100, "st": "created"},
    ])
    raised = False
    try:
        migrate.run(engine, strict=True)
    except migrate.MigrationError as exc:
        raised = "duplicate" in str(exc).lower()
    check(raised, "upgrade fails closed on duplicate provider_payment_id (no silent rewrite)")


def test_upgrade_blocks_on_duplicate_checkout_id(engine):
    _build_legacy(engine, [
        {"o": 1, "s": 1, "p": "square", "pp": None, "pc": "DUPC", "k": "c1", "t": 100, "st": "created"},
        {"o": 2, "s": 1, "p": "square", "pp": None, "pc": "DUPC", "k": "c2", "t": 100, "st": "created"},
    ])
    raised = False
    try:
        migrate.run(engine, strict=True)
    except migrate.MigrationError as exc:
        raised = "duplicate" in str(exc).lower()
    check(raised, "upgrade fails closed on duplicate provider_checkout_id (#15)")


def test_duplicate_rejected_after_upgrade(engine):
    """Once upgraded, the live constraint rejects a duplicate external id."""
    _build_legacy(engine, [
        {"o": 1, "s": 1, "p": "square", "pp": "P1", "pc": "C1", "k": "u1", "t": 100, "st": "created"},
    ])
    migrate.run(engine, strict=True)
    raised = False
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO payment_attempt (order_id, staff_id, provider, "
                              "provider_payment_id, idempotency_key, expected_total_cents) "
                              "VALUES (9,1,'square_terminal','P1','u2',100)"))
    except Exception:
        raised = True
    check(raised, "duplicate provider_payment_id rejected by the live constraint post-upgrade")


if __name__ == "__main__":
    if not pg_dsn():
        print("SKIP: PG_TEST_DSN not set (migration-upgrade tests are Postgres-specific)")
        sys.exit(0)
    print(f"Postgres: {pg_dsn()}")
    for fn in (test_clean_upgrade, test_upgrade_blocks_on_duplicate_payment_id,
               test_upgrade_blocks_on_duplicate_checkout_id, test_duplicate_rejected_after_upgrade):
        print(f"- {fn.__name__}")
        eng = create_engine(pg_dsn(), future=True)
        fn(eng)
        eng.dispose()
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall migration-upgrade tests passed")
