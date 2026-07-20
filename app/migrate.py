"""Additive schema migrations.

There is no migration framework here, and Base.metadata.create_all() only
creates missing *tables* — it never alters one that already exists. So a column
added to a model after the database was seeded is silently absent, and the
first query touching it fails at runtime with "no such column".

Each entry below is an idempotent ADD COLUMN guarded by PRAGMA table_info.
Additive only: no drops, no type changes, nothing that could lose a row of
service history. Run on every startup; a fully migrated database is a no-op.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

# (table, column, DDL type + default). The default matters: existing rows are
# backfilled with it, so it has to be the value that preserves current
# behaviour — every table that exists today is an active one.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("restaurant_table", "is_active", "BOOLEAN NOT NULL DEFAULT 1"),
)


def run(engine: Engine) -> list[str]:
    """Apply any missing columns. Returns the ones actually added."""
    applied: list[str] = []
    with engine.begin() as conn:
        for table, column, ddl in ADDED_COLUMNS:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            if not rows:
                continue                       # table not created yet; create_all owns it
            if any(r[1] == column for r in rows):
                continue                       # already migrated
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            applied.append(f"{table}.{column}")
    return applied
