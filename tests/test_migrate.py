"""The invariant that matters: every boolean column reaches Postgres.

The ADDED_COLUMNS DDL is authored for SQLite (integer boolean defaults). On
Postgres an integer default on a BOOLEAN column is a DatatypeMismatch, which the
guarded ALTER swallows as SKIPPED — so the column never lands and every query
touching it 500s (this is exactly how order_item.hh_reverted broke prod). The
Postgres branch must translate `DEFAULT 0/1` to `DEFAULT false/true`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import migrate
from app.migrate import ADDED_COLUMNS, _pg_boolean_ddl


def check(cond, label):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    return cond


ok = True

# --- the translation itself ---------------------------------------------------
ok &= check(
    _pg_boolean_ddl("BOOLEAN NOT NULL DEFAULT 0") == "BOOLEAN NOT NULL DEFAULT false",
    f"DEFAULT 0 -> false ({_pg_boolean_ddl('BOOLEAN NOT NULL DEFAULT 0')!r})",
)
ok &= check(
    _pg_boolean_ddl("BOOLEAN NOT NULL DEFAULT 1") == "BOOLEAN NOT NULL DEFAULT true",
    f"DEFAULT 1 -> true ({_pg_boolean_ddl('BOOLEAN NOT NULL DEFAULT 1')!r})",
)
# Non-boolean DDL is left exactly as-is (integer/varchar defaults are valid PG).
for ddl in (
    "INTEGER NOT NULL DEFAULT 0",
    "INTEGER NOT NULL DEFAULT 1",
    "VARCHAR(200) DEFAULT ''",
    "INTEGER REFERENCES zone(id)",
    "TIMESTAMP",
):
    ok &= check(_pg_boolean_ddl(ddl) == ddl, f"non-boolean untouched: {ddl!r}")

# An already-correct boolean literal passes through unchanged (idempotent).
ok &= check(
    _pg_boolean_ddl("BOOLEAN NOT NULL DEFAULT true") == "BOOLEAN NOT NULL DEFAULT true",
    "already-true is a no-op",
)

# --- every real boolean entry is safe for Postgres ----------------------------
bool_cols = [(t, c, d) for (t, c, d) in ADDED_COLUMNS if "BOOLEAN" in d.upper()]
ok &= check(len(bool_cols) >= 1, f"found {len(bool_cols)} boolean column(s) to guard")
for table, column, ddl in bool_cols:
    out = _pg_boolean_ddl(ddl)
    # No bare integer default may survive on a boolean column, and the result
    # must carry a real boolean literal.
    import re

    bad = re.search(r"DEFAULT\s+[01]\b", out)
    good = re.search(r"DEFAULT\s+(true|false)\b", out, re.IGNORECASE)
    ok &= check(
        bad is None and good is not None,
        f"{table}.{column}: {ddl!r} -> {out!r}",
    )

# --- SQLite path is untouched: it uses the raw DDL, which SQLite accepts -------
# (regression guard — the fix must not alter the working SQLite branch)
import inspect

src = inspect.getsource(migrate.run)
ok &= check(
    "_pg_boolean_ddl" not in src,
    "SQLite run() does not translate DDL (keeps raw integer defaults)",
)

print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
sys.exit(0 if ok else 1)
