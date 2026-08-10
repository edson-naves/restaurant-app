"""Shared test fixture for the payment core.

Builds an engine against PostgreSQL when ``PG_TEST_DSN`` is set (real FK / row
locking / unique races), otherwise a throwaway file-backed SQLite with
``PRAGMA foreign_keys=ON`` so even the SQLite runs create and honour real parent
rows (review finding #16 — no dangling FK ids). Provides ``seed_parents`` which
inserts a real Staff / Channel / Order / PaymentInstrument / Payment graph so
payment/refund attempts reference rows that actually exist.
"""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import oltp  # noqa: F401  register tables
from app.models.oltp import Channel, Order, Payment, PaymentInstrument, Staff


def pg_dsn() -> str | None:
    return os.environ.get("PG_TEST_DSN")


def make_engine():
    """Return (engine, is_postgres). SQLite path enables FK enforcement."""
    dsn = pg_dsn()
    if dsn:
        return create_engine(dsn, future=True), True
    path = os.path.join(tempfile.gettempdir(), f"paytest_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_con, _rec):  # noqa: ANN001
        dbapi_con.execute("PRAGMA foreign_keys=ON")

    return engine, False


def fresh_schema(engine):
    """Clean slate: drop then create the whole schema (safe on the disposable
    test Postgres and on a throwaway SQLite file)."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def Session(engine):
    return sessionmaker(bind=engine, future=True)


# Per-test isolation helper. On a shared Postgres database, the previous test's
# open session holds locks that would block the next test's drop_all/create_all
# (a hang). new_db() closes and disposes the prior engine/session before
# rebuilding, and seeds a fresh parent graph.
_state: dict = {"session": None, "engine": None}


def new_db():
    if _state["session"] is not None:
        try:
            _state["session"].close()
        except Exception:
            pass
    if _state["engine"] is not None:
        _state["engine"].dispose()
    engine, _is_pg = make_engine()
    fresh_schema(engine)
    session = Session(engine)()
    ids = seed_parents(session)
    _state["session"], _state["engine"] = session, engine
    return session, ids


def seed_parents(session) -> dict:
    """Insert a minimal, valid parent graph and return the ids attempts reference."""
    staff = Staff(name="Tester", role="owner", pin_code="pbkdf2_sha256$1$x$y")
    channel = Channel(code=f"ch_{uuid.uuid4().hex[:6]}", name="Dine", channel_type="dine_in")
    session.add_all([staff, channel])
    session.flush()
    order = Order(code=f"O{uuid.uuid4().hex[:8]}", channel_id=channel.id)
    inst = PaymentInstrument(
        code=f"cash_{uuid.uuid4().hex[:6]}", name="Cash",
        instrument_type="cash", provider="manual",
    )
    session.add_all([order, inst])
    session.flush()
    payment = Payment(order_id=order.id, instrument_id=inst.id, staff_id=staff.id,
                      total_cents=10000)
    session.add(payment)
    session.flush()
    session.commit()
    return {"staff_id": staff.id, "order_id": order.id,
            "payment_id": payment.id, "instrument_id": inst.id}
