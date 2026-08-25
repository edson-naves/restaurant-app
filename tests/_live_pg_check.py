"""Live Postgres proof of the boolean-default fix (not part of the SQLite suite).

Runs only when PG_TEST_URL is set. To be safe against being pointed at a real
database, ALL work happens inside a throwaway schema this test creates and drops
— it never creates, alters, or drops anything in `public` or any application
table. The only destructive statement is `DROP SCHEMA <temp> CASCADE` on the
schema we just created, which contains nothing but this test's own tables. If a
schema by that name somehow already exists, the test aborts without touching it.

Proves, against a real Postgres:
  1. the ORIGINAL SQLite-style DDL ('BOOLEAN NOT NULL DEFAULT 0') is rejected by
     Postgres (DatatypeMismatch) — i.e. the bug is real;
  2. the TRANSLATED DDL from _pg_boolean_ddl lands the column with the right
     boolean type and default;
  3. driving the real _run_postgres() over the 4 boolean ADDED_COLUMNS on tables
     that exist-but-lack-the-column adds every one of them (no SKIP).

_run_postgres() introspects and writes via current_schema()/unqualified names,
so the temp schema is pinned as the whole session's search_path — every table it
touches resolves inside the sandbox, never in public.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text
from app.migrate import ADDED_COLUMNS, _pg_boolean_ddl, _run_postgres

URL = os.environ.get("PG_TEST_URL")
if not URL:
    print("SKIP: PG_TEST_URL not set")
    sys.exit(0)

SCHEMA = f"mise_booltest_{os.getpid()}"


def check(cond, label):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    return cond


# --- isolate: create a throwaway schema; abort if the name is already taken ---
admin = create_engine(URL)
with admin.connect() as c:
    exists = c.execute(text(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"
    ), {"s": SCHEMA}).first()
    if exists:
        print(f"ABORT: schema {SCHEMA!r} already exists — refusing to touch it")
        sys.exit(2)
with admin.begin() as c:
    c.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))

# Every connection from this engine is pinned to the temp schema, so _run_postgres'
# current_schema() checks and unqualified DDL all resolve inside the sandbox.
eng = create_engine(URL, connect_args={"options": f"-csearch_path={SCHEMA}"})

ok = True
try:
    # 1) original DDL must fail on Postgres ----------------------------------
    with eng.begin() as c:
        c.execute(text("CREATE TABLE _bt (id serial primary key)"))
    raw_failed = False
    try:
        with eng.begin() as c:
            c.execute(text('ALTER TABLE _bt ADD COLUMN v BOOLEAN NOT NULL DEFAULT 0'))
    except Exception as exc:
        raw_failed = "boolean" in str(exc).lower() or "DatatypeMismatch" in type(exc).__name__
    ok &= check(raw_failed, "original 'DEFAULT 0' rejected by Postgres (bug is real)")

    # 2) translated DDL lands the column with a real boolean default ---------
    with eng.begin() as c:
        c.execute(text("DROP TABLE _bt"))
        c.execute(text("CREATE TABLE _bt (id serial primary key)"))
        c.execute(text(f'ALTER TABLE _bt ADD COLUMN v {_pg_boolean_ddl("BOOLEAN NOT NULL DEFAULT 0")}'))
        c.execute(text("INSERT INTO _bt DEFAULT VALUES"))
    with eng.connect() as c:
        row = c.execute(text("SELECT v FROM _bt")).scalar_one()
        dtype = c.execute(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name='_bt' AND column_name='v'"
        ), {"s": SCHEMA}).scalar_one()
    ok &= check(dtype == "boolean" and row is False,
                f"translated DDL -> {dtype} column, default backfilled {row!r}")

    # 3) real _run_postgres adds all 4 boolean columns on pre-existing tables
    bool_cols = [(t, c, d) for (t, c, d) in ADDED_COLUMNS if "BOOLEAN" in d.upper()]
    with eng.begin() as c:
        for table, _, _ in bool_cols:
            c.execute(text(f'CREATE TABLE "{table}" (id serial primary key)'))
    applied = _run_postgres(eng)
    for table, column, _ in bool_cols:
        with eng.connect() as c:
            got = c.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name=:t AND column_name=:col"
            ), {"s": SCHEMA, "t": table, "col": column}).scalar_one_or_none()
        ok &= check(got == "boolean", f"_run_postgres added {table}.{column} as {got!r}")
        # exact token (trailing ':') so 'payment.voided' does not match the unrelated
        # 'payment.voided_at' / 'payment.voided_by_id' FK columns that legitimately skip
        ok &= check(f"SKIPPED {table}.{column}:" not in " ".join(applied),
                    f"{table}.{column} not in SKIPPED list")
finally:
    # only ever drops the schema we created above (and its own test tables)
    eng.dispose()
    with admin.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))

print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
sys.exit(0 if ok else 1)
