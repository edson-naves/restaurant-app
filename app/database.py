"""Database engine and session management.

Money is stored throughout as integer cents. A payment system cannot tolerate
binary floating point drift, and SQLite has no native decimal type.
"""
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_DIR = Path(__file__).resolve().parent.parent / "db"
DB_DIR.mkdir(exist_ok=True)
DB_PATH = DB_DIR / "restaurant.db"

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
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
