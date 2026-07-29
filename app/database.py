"""Database engine and session management.

Money is stored throughout as integer cents. A payment system cannot tolerate
binary floating point drift, and SQLite has no native decimal type.

The engine is chosen from the environment. With nothing set, it falls back to a
local SQLite file — zero-config for development and the test suites. In
production, set DATABASE_URL to a Postgres DSN so many tablets can write at once
(SQLite serialises writers and starts throwing "database is locked" under a
busy floor):

    DATABASE_URL=postgresql+psycopg://user:password@host:5432/restaurant

All date/time logic lives in Python and stores plain columns, so the ORM ports
between the two dialects without any raw-SQL changes.
"""
import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_DIR = Path(__file__).resolve().parent.parent / "db"
DB_DIR.mkdir(exist_ok=True)
DB_PATH = DB_DIR / "restaurant.db"

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")
IS_SQLITE = DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    # One file, many threads: SQLite guards each connection to its creating
    # thread unless told otherwise.
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        future=True,
    )
else:
    # Postgres (or any server DB): a real pool with pre-ping so a connection
    # dropped by the server (restart, idle timeout) is detected and replaced
    # instead of surfacing as an error mid-request.
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
        max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "20")),
        future=True,
    )


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    # Foreign-key enforcement and WAL are SQLite concerns only; Postgres
    # enforces FKs natively and has its own write-ahead log.
    if not IS_SQLITE:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def money(cents: int) -> str:
    """Format integer cents as a display string."""
    return f"{cents / 100:,.2f}"
