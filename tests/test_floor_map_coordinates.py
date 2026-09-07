"""Floor Plan Builder — model, migration and backfill for the free-placement
(per-mille) table coordinates (docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md
§3-4). Implementation-only slice: no UI, no drag endpoints, no visual change.

Exercises the real `migrate.run()` dialect dispatch — SQLite always; against a
disposable PostgreSQL database only when BOTH PG_TEST_DSN names one AND
ALLOW_DESTRUCTIVE_PG_TESTS=1 is set (never production — see
assert_disposable_postgres_target() below) — and asserts the approved
contract: `ELIGIBLE(table, zone)` as an INNER JOIN, the per-floor
GRID_ROWS_SEEN formula, Python-side round-half-up, the [0,1000] clamp, the
in-transaction fail-closed post-condition, and idempotency.

Run: python tests/test_floor_map_coordinates.py
     PG_TEST_DSN=postgresql+psycopg://user:pass@host/some_test_db \
     ALLOW_DESTRUCTIVE_PG_TESTS=1 python tests/test_floor_map_coordinates.py
"""
import math
import os
import re
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from app import migrate
from app.database import Base
from app.models import oltp  # noqa: F401  register every table on Base.metadata
from tests._pay_fixture import pg_dsn

_failures = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)
    return cond


# --------------------------------------------------------------------------
# PostgreSQL destructive-target guard (fix required by independent review):
# this file's PostgreSQL scenarios call Base.metadata.drop_all() and raw
# DROP TABLE against whatever PG_TEST_DSN names. A DSN pointed at the wrong
# database by accident — a typo, a copy-pasted production URL, an
# environment variable leaking from another shell — must NEVER be silently
# wiped. This guard is fail-closed: every check must pass before this file
# performs a single destructive statement against a PostgreSQL target. It is
# pure string/URL inspection — no network I/O, no engine, no connection — so
# a rejection is provably incapable of having touched any database.
# --------------------------------------------------------------------------

# Hostname suffixes documented for this project (RENDER.md: Render's own
# Postgres host pattern `dpg-xxxx.<region>-postgres.render.com`, and its
# documented Neon fallback, neon.tech) plus other common managed-PostgreSQL
# production hosting suffixes, as generic defense in depth. Not exhaustive —
# the database-name "test" marker and the explicit opt-in env var below are
# the primary protections, not this list. Extend it if this project's actual
# production host is ever put on record somewhere this file can read.
_KNOWN_PRODUCTION_HOST_SUFFIXES = (
    "render.com",
    "neon.tech",
    "rds.amazonaws.com",
    "amazonaws.com",
    "supabase.co",
    "supabase.com",
    "database.windows.net",
    "herokuapp.com",
    "digitalocean.com",
    "elephantsql.com",
)

# "test" only as a delimited token — bounded by start/end of string, "_", or
# "-" — not any substring match. A plain r"test" match (the first version of
# this guard) would have accepted "contest", "latest", "testament", and
# "protest" as if they were disposable-test-named databases, which they are
# not; this is a real false-positive fixed here, not a hypothetical one.
_TEST_MARKER_RE = re.compile(r"(?:^|[_-])test(?:$|[_-])", re.IGNORECASE)

_ALLOW_DESTRUCTIVE_ENV = "ALLOW_DESTRUCTIVE_PG_TESTS"


class UnsafePostgresTargetError(RuntimeError):
    """A PostgreSQL target failed the disposable-database safety guard.
    Always raised before any connection is opened or any destructive
    statement is issued — never after."""


def assert_disposable_postgres_target(dsn: str) -> None:
    """Fail-closed: raise unless `dsn` is unambiguously a disposable test
    PostgreSQL database. MUST be called, and MUST pass, before this file
    performs create_all()/drop_all() or any raw DROP/CREATE against a
    PostgreSQL engine. Every check below runs before any network I/O — this
    function never constructs an Engine, never opens a connection.

    Checks, all required:
      - `dsn` parses as a valid SQLAlchemy URL (sqlalchemy.engine.make_url);
      - the dialect is PostgreSQL;
      - the URL names a database (never an ambiguous "whatever's default");
      - the host does not end with a known production-hosting suffix;
      - the database name contains an unambiguous test marker ("test");
      - the ALLOW_DESTRUCTIVE_PG_TESTS=1 environment variable is set —
        an explicit, separate opt-in even when everything else looks safe.

    Never includes the password, the full DSN, or any credential in a
    message — only the parsed, already-redacted host/database/dialect.
    """
    try:
        url = make_url(dsn)
    except Exception as exc:
        raise UnsafePostgresTargetError(
            "PG_TEST_DSN could not be parsed as a database URL. Refusing to "
            "run any destructive PostgreSQL operation."
        ) from exc

    backend = url.get_backend_name()
    if backend != "postgresql":
        raise UnsafePostgresTargetError(
            f"PG_TEST_DSN dialect is {backend!r}, not 'postgresql'. Refusing "
            "to run any destructive PostgreSQL operation."
        )

    host = (url.host or "").lower()
    database = url.database or ""

    if not database:
        raise UnsafePostgresTargetError(
            "PG_TEST_DSN names no database. Refusing to run any destructive "
            "operation against an unnamed/default database."
        )

    for suffix in _KNOWN_PRODUCTION_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            raise UnsafePostgresTargetError(
                f"PG_TEST_DSN host ends with {suffix!r}, a known production "
                "-hosting suffix for this project. Refusing to run any "
                "destructive operation against it."
            )

    if not _TEST_MARKER_RE.search(database):
        raise UnsafePostgresTargetError(
            f"PG_TEST_DSN database name {database!r} has no unambiguous "
            "test marker ('test'). Refusing to run any destructive "
            "operation against a database not clearly named as disposable."
        )

    if os.environ.get(_ALLOW_DESTRUCTIVE_ENV) != "1":
        raise UnsafePostgresTargetError(
            f"{_ALLOW_DESTRUCTIVE_ENV}=1 is required, in addition to a "
            "disposable-looking PG_TEST_DSN, to run any destructive "
            "PostgreSQL operation. Refusing to proceed without it."
        )


def _guard_if_postgres(engine) -> None:
    """No-op for SQLite; runs the full disposable-target guard for anything
    else. Call this at the top of every function in this file that performs
    a destructive operation (drop_all, DROP TABLE, ...) against `engine`."""
    if engine.dialect.name != "sqlite":
        assert_disposable_postgres_target(str(engine.url))


# --------------------------------------------------------------------------
# Engines — SQLite is always a fresh, disposable file; Postgres only when
# PG_TEST_DSN names a disposable database AND passes the guard above (never
# production; see AGENTS.md "Production / Security Safety" and the design
# doc's own test plan).
# --------------------------------------------------------------------------

def _sqlite_engine():
    path = os.path.join(tempfile.gettempdir(), f"floormap_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_con, _rec):          # noqa: ANN001
        dbapi_con.execute("PRAGMA foreign_keys=ON")

    return engine


def _fresh_schema(engine):
    """Full current model shape — every column, including the two new ones."""
    _guard_if_postgres(engine)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def _drop_map_columns(engine):
    """Simulate a database that predates this slice: the two new columns are
    absent (create_all() ran once, before the model gained them)."""
    _guard_if_postgres(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE restaurant_table DROP COLUMN map_x_per_mille"))
        conn.execute(text("ALTER TABLE restaurant_table DROP COLUMN map_y_per_mille"))


def _has_map_columns(engine) -> bool:
    with engine.connect() as conn:
        return (
            migrate._column_exists(conn, "restaurant_table", "map_x_per_mille")
            and migrate._column_exists(conn, "restaurant_table", "map_y_per_mille")
        )


def _seed_floor_zone(engine, floor_name="1st floor", zone_name="Main"):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO floor (name, sort_order, is_active) VALUES (:n, 0, TRUE)"
        ), {"n": floor_name})
        floor_id = conn.execute(
            text("SELECT id FROM floor WHERE name = :n"), {"n": floor_name}
        ).scalar_one()
        conn.execute(text(
            "INSERT INTO zone (floor_id, name, sort_order, is_active, pos_x, pos_y, width, height) "
            "VALUES (:f, :n, 0, TRUE, 60, 60, 360, 300)"
        ), {"f": floor_id, "n": zone_name})
        zone_id = conn.execute(
            text("SELECT id FROM zone WHERE floor_id = :f AND name = :n"),
            {"f": floor_id, "n": zone_name},
        ).scalar_one()
    return floor_id, zone_id


def _seed_table(engine, number, zone_id, pos_x, pos_y, is_active=True):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO restaurant_table "
            "(number, zone_id, zone, capacity, status, is_active, pos_x, pos_y, shape) "
            "VALUES (:num, :z, 'Main', 4, 'free', :active, :x, :y, 'round')"
        ), {"num": number, "z": zone_id, "active": is_active, "x": pos_x, "y": pos_y})
        return conn.execute(
            text("SELECT id FROM restaurant_table WHERE number = :n"), {"n": number}
        ).scalar_one()


def _map_pos(engine, table_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT map_x_per_mille, map_y_per_mille FROM restaurant_table WHERE id = :id"),
            {"id": table_id},
        ).one()


def _expected(pos_x, pos_y, grid_cols, grid_rows):
    """Reference implementation of the approved formula, independent of
    app.migrate's own code, so the test does not just re-check the
    implementation against itself."""
    x = min(max(pos_x, 0), grid_cols - 1)
    y = max(pos_y, 0)
    map_x = max(0, min(1000, math.floor((x + 0.5) / grid_cols * 1000 + 0.5)))
    map_y = max(0, min(1000, math.floor((y + 0.5) / grid_rows * 1000 + 0.5)))
    return map_x, map_y


# --------------------------------------------------------------------------
# 1. Fresh SQLite database
# --------------------------------------------------------------------------

def test_fresh_sqlite_has_null_columns():
    engine = _sqlite_engine()
    _fresh_schema(engine)
    check(_has_map_columns(engine), "fresh SQLite: create_all() already has both columns")
    floor_id, zone_id = _seed_floor_zone(engine)
    tid = _seed_table(engine, 1, zone_id, 3, 0)
    applied = migrate.run(engine, strict=True)
    x, y = _map_pos(engine, tid)
    check(x is not None and y is not None, "fresh SQLite: eligible table backfilled on first run()")
    check(any("map position" in a for a in applied), f"backfill reported in applied log: {applied}")


# --------------------------------------------------------------------------
# 2/3. Legacy database (SQLite / Postgres) missing the two columns
# --------------------------------------------------------------------------

def test_legacy_missing_columns_gets_backfilled(engine, label):
    _fresh_schema(engine)
    _drop_map_columns(engine)
    check(not _has_map_columns(engine), f"{label}: legacy schema starts without the two columns")
    floor_id, zone_id = _seed_floor_zone(engine)
    tid = _seed_table(engine, 1, zone_id, 4, 1)
    migrate.run(engine, strict=True)
    check(_has_map_columns(engine), f"{label}: columns added by migrate.run()")
    x, y = _map_pos(engine, tid)
    check(x is not None and y is not None, f"{label}: pre-existing table backfilled, not left NULL")


# --------------------------------------------------------------------------
# 4. Proof the backfill is actually invoked on the PostgreSQL path (not just
#    column creation) — the exact gap the design's first version had.
# --------------------------------------------------------------------------

def test_postgres_backfill_actually_runs(engine):
    _fresh_schema(engine)
    _drop_map_columns(engine)
    floor_id, zone_id = _seed_floor_zone(engine)
    tid = _seed_table(engine, 1, zone_id, 2, 1)
    check(engine.dialect.name != "sqlite", "sanity: this engine is the Postgres dialect")
    migrate.run(engine, strict=True)          # dispatches to _run_postgres() internally
    x, y = _map_pos(engine, tid)
    check(
        x is not None and y is not None,
        "PostgreSQL path: map position is NOT NULL after migrate.run() — "
        "the backfill's Postgres call site actually executed",
    )


# --------------------------------------------------------------------------
# 5. Formula — multi-row, multi-floor
# --------------------------------------------------------------------------

def test_formula_multi_row_multi_floor(engine, label):
    _fresh_schema(engine)
    f1, z1 = _seed_floor_zone(engine, "Floor 1", "Main")
    f2, z2 = _seed_floor_zone(engine, "Floor 2", "Patio")
    # Floor 1: three tables at rows 0, 1, 2 (so GRID_ROWS_SEEN = 3).
    t1 = _seed_table(engine, 1, z1, 0, 0)
    t2 = _seed_table(engine, 2, z1, 5, 1)
    t3 = _seed_table(engine, 3, z1, 9, 2)
    # Floor 2: one table at row 0 alone (GRID_ROWS_SEEN = 1).
    t4 = _seed_table(engine, 4, z2, 3, 0)
    migrate.run(engine, strict=True)

    for tid, (pos_x, pos_y), grid_rows, tag in (
        (t1, (0, 0), 3, "floor1/row0"),
        (t2, (5, 1), 3, "floor1/row1"),
        (t3, (9, 2), 3, "floor1/row2"),
        (t4, (3, 0), 1, "floor2/alone"),
    ):
        exp_x, exp_y = _expected(pos_x, pos_y, migrate.GRID_COLS, grid_rows)
        got_x, got_y = _map_pos(engine, tid)
        check(
            (got_x, got_y) == (exp_x, exp_y),
            f"{label} {tag}: expected ({exp_x},{exp_y}), got ({got_x},{got_y})",
        )
    # The two floors must not mix into one GRID_ROWS_SEEN (the reason for the
    # join through Zone.floor_id rather than a bare GROUP BY).
    check(t1 != t4, "sanity: distinct table ids")


# --------------------------------------------------------------------------
# 6. Bounds and clamps
# --------------------------------------------------------------------------

def test_clamps(engine, label):
    _fresh_schema(engine)
    floor_id, zone_id = _seed_floor_zone(engine)
    # Out-of-range grid values that "cannot occur today" through the app's own
    # writers, but the backfill must not crash or overflow on them anyway.
    t_neg_x = _seed_table(engine, 1, zone_id, -5, 0)
    t_over_x = _seed_table(engine, 2, zone_id, 999, 0)
    t_neg_y = _seed_table(engine, 3, zone_id, 0, -3)
    migrate.run(engine, strict=True)
    for tid, tag in ((t_neg_x, "pos_x<0"), (t_over_x, "pos_x>=GRID_COLS"), (t_neg_y, "pos_y<0")):
        x, y = _map_pos(engine, tid)
        check(
            x is not None and 0 <= x <= 1000 and y is not None and 0 <= y <= 1000,
            f"{label} {tag}: clamped into [0,1000] ({x},{y}), no crash",
        )


# --------------------------------------------------------------------------
# 7/8. Not eligible: no zone, or inactive
# --------------------------------------------------------------------------

def test_ineligible_tables_stay_null(engine, label):
    _fresh_schema(engine)
    floor_id, zone_id = _seed_floor_zone(engine)
    migrate.run(engine, strict=True)          # baseline: columns present, no zone-less rows yet
    inactive_id = _seed_table(engine, 101, zone_id, 3, 0, is_active=False)
    # Zone-less: is_active TRUE but zone_id NULL. Calling
    # _backfill_table_map_positions directly (not the full migrate.run())
    # deliberately: run()'s SQLite path also calls the pre-existing, unrelated
    # _backfill_locations() first, which auto-resolves ANY zone_id IS NULL
    # row for legacy free-text zones (by design, for that function) before
    # this one would ever see it — so a genuinely zone-less row is only
    # observable to THIS function in isolation, which is exactly what this
    # test needs to check.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO restaurant_table (number, zone_id, zone, capacity, status, "
            "is_active, pos_x, pos_y, shape) VALUES (100, NULL, 'Ghost', 4, 'free', TRUE, 3, 0, 'round')"
        ))
        zoneless_id = conn.execute(
            text("SELECT id FROM restaurant_table WHERE number=100")
        ).scalar_one()
        migrate._backfill_table_map_positions(conn)
    zx, zy = _map_pos(engine, zoneless_id)
    ix, iy = _map_pos(engine, inactive_id)
    check(zx is None and zy is None, f"{label}: zone-less table stays NULL, not eligible")
    check(ix is None and iy is None, f"{label}: inactive table stays NULL, not eligible")


# --------------------------------------------------------------------------
# 9. Idempotency, both dialects
# --------------------------------------------------------------------------

def test_idempotent(engine, label):
    _fresh_schema(engine)
    floor_id, zone_id = _seed_floor_zone(engine)
    tid = _seed_table(engine, 1, zone_id, 4, 2)
    migrate.run(engine, strict=True)
    first = _map_pos(engine, tid)
    second_applied = migrate.run(engine, strict=True)
    second = _map_pos(engine, tid)
    check(first == second, f"{label}: re-running migrate.run() does not change an already-backfilled position")
    check(
        not any("map position" in a for a in second_applied),
        f"{label}: second run's applied log reports no new backfill ({second_applied})",
    )


# --------------------------------------------------------------------------
# 10. A table that becomes eligible later
# --------------------------------------------------------------------------

def test_becomes_eligible_later(engine, label):
    _fresh_schema(engine)
    floor_id, zone_id = _seed_floor_zone(engine)
    migrate.run(engine, strict=True)             # baseline; nothing to backfill yet
    # Direct calls to _backfill_table_map_positions here, not the full
    # migrate.run() — same reason as test_ineligible_tables_stay_null: the
    # pre-existing, unrelated _backfill_locations() auto-resolves any
    # zone_id IS NULL row before this function would see it.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO restaurant_table (number, zone_id, zone, capacity, status, "
            "is_active, pos_x, pos_y, shape) VALUES (200, NULL, 'Ghost', 4, 'free', TRUE, 2, 1, 'round')"
        ))
        tid = conn.execute(
            text("SELECT id FROM restaurant_table WHERE number=200")
        ).scalar_one()
        migrate._backfill_table_map_positions(conn)   # still zone-less: still NULL
    x, y = _map_pos(engine, tid)
    check(x is None and y is None, f"{label}: still zone-less on this run — correctly still NULL")
    with engine.begin() as conn:
        conn.execute(text("UPDATE restaurant_table SET zone_id = :z WHERE id = :id"),
                     {"z": zone_id, "id": tid})
        migrate._backfill_table_map_positions(conn)   # NOW eligible
    x, y = _map_pos(engine, tid)
    check(
        x is not None and y is not None,
        f"{label}: backfilled on the run AFTER it became eligible (assigned a zone), not before",
    )


# --------------------------------------------------------------------------
# 11. Post-condition failure rolls back, both dialects (per-dialect shape)
# --------------------------------------------------------------------------

def test_postcondition_failure_rolls_back(engine, label, is_postgres):
    """Two distinct failure shapes, both required by the design:

    (a) a failure DURING the write loop (step 2) — injected here by making
        `_round_half_up` return None, so the per-table clamp raises a
        TypeError — on a legacy database where the columns do not exist yet,
        so this also exercises the dialect-specific column/backfill
        transaction relationship (§4.2, §4.5).
    (b) the in-transaction post-condition itself (step 3/4) catching a
        pre-existing corrupted value that step 2 never touched this run —
        proven by corrupting an already-backfilled value AFTER a first,
        successful run, then re-running with nothing "pending".
    """
    _fresh_schema(engine)
    _drop_map_columns(engine)
    floor_id, zone_id = _seed_floor_zone(engine)
    tid = _seed_table(engine, 1, zone_id, 3, 0)

    real_round = migrate._round_half_up
    migrate._round_half_up = lambda v: None       # forces a TypeError inside step 2's clamp
    try:
        raised = False
        try:
            migrate.run(engine, strict=True)
        except Exception:
            raised = True
        check(raised, f"{label}: a failure during the backfill write step (2) raises rather than completing silently")
    finally:
        migrate._round_half_up = real_round

    if is_postgres:
        # Columns were already committed (their own per-column transactions)
        # before the backfill's transaction ran and failed — they remain.
        check(_has_map_columns(engine), f"{label}: PostgreSQL — columns remain committed after the backfill failed")
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM restaurant_table WHERE map_x_per_mille IS NOT NULL"
            )).scalar_one()
        check(n == 0, f"{label}: PostgreSQL — zero rows show a partially-written value")
    else:
        # SQLite — corrected from the design's stated assumption, against
        # measured pysqlite/SQLAlchemy behavior (see app/migrate.py's comment
        # at this call site): `ALTER TABLE ADD COLUMN` auto-commits the
        # instant it executes, even inside `engine.begin()` — DDL is NOT
        # part of the enclosing transaction on this driver. Only the DML
        # (this backfill's UPDATEs) actually rolls back. Net effect is still
        # safe: the columns exist but every row's value rolled back to NULL,
        # which is exactly the "not yet backfilled" state the next run
        # expects — not "everything vanished together".
        check(_has_map_columns(engine), f"{label}: SQLite — the ADD COLUMN itself already committed (DDL, not part of the transaction)")
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM restaurant_table WHERE map_x_per_mille IS NOT NULL"
            )).scalar_one()
        check(n == 0, f"{label}: SQLite — the backfill's own DML (UPDATEs) rolled back correctly; every row is NULL, not partially written")

    # Retry after "fixing the cause" (restoring the real function) must be
    # safe and idempotent, on both dialects.
    migrate.run(engine, strict=True)
    check(_has_map_columns(engine), f"{label}: retry after the fix succeeds and adds the columns")
    x, y = _map_pos(engine, tid)
    check(x is not None and 0 <= x <= 1000, f"{label}: retry backfills correctly ({x},{y})")

    # (b) Corrupt an already-good value out of range, with nothing pending —
    # the post-condition must still catch it, not skip the check because
    # this run had nothing new to write.
    with engine.begin() as conn:
        conn.execute(text("UPDATE restaurant_table SET map_x_per_mille = 5000 WHERE id = :id"), {"id": tid})
    raised = False
    try:
        migrate.run(engine, strict=True)
    except RuntimeError:
        raised = True
    check(raised, f"{label}: post-condition (step 3/4) catches a corrupted value even when nothing was pending")
    with engine.connect() as conn:
        val = conn.execute(
            text("SELECT map_x_per_mille FROM restaurant_table WHERE id=:id"), {"id": tid}
        ).scalar_one()
    check(val == 5000, f"{label}: the corrupted value itself is untouched by the failed run (no silent 'fix')")

    # Fix and retry once more — still safe and idempotent.
    with engine.begin() as conn:
        conn.execute(text("UPDATE restaurant_table SET map_x_per_mille = NULL WHERE id = :id"), {"id": tid})
    migrate.run(engine, strict=True)
    x, y = _map_pos(engine, tid)
    check(x is not None and 0 <= x <= 1000, f"{label}: idempotent retry after fixing the corruption")


# --------------------------------------------------------------------------
# 12. Orphaned zone reference, via a deliberately FK-less legacy scratch
#     schema — never by disabling constraints on the real schema (fourth
#     independent review's non-blocking note 1).
# --------------------------------------------------------------------------

_LEGACY_NO_FK_SQLITE = """
CREATE TABLE zone (
    id INTEGER PRIMARY KEY, floor_id INTEGER, name VARCHAR(60),
    sort_order INTEGER, is_active BOOLEAN, color VARCHAR(7),
    pos_x INTEGER, pos_y INTEGER, width INTEGER, height INTEGER
);
CREATE TABLE restaurant_table (
    id INTEGER PRIMARY KEY, number INTEGER, zone_id INTEGER,
    zone VARCHAR(40), capacity INTEGER, status VARCHAR(20),
    is_active BOOLEAN, pos_x INTEGER, pos_y INTEGER, shape VARCHAR(10),
    current_waiter_id INTEGER
);
"""

_LEGACY_NO_FK_POSTGRES = """
CREATE TABLE zone (
    id SERIAL PRIMARY KEY, floor_id INTEGER, name VARCHAR(60),
    sort_order INTEGER, is_active BOOLEAN, color VARCHAR(7),
    pos_x INTEGER, pos_y INTEGER, width INTEGER, height INTEGER
);
CREATE TABLE restaurant_table (
    id SERIAL PRIMARY KEY, number INTEGER, zone_id INTEGER,
    zone VARCHAR(40), capacity INTEGER, status VARCHAR(20),
    is_active BOOLEAN, pos_x INTEGER, pos_y INTEGER, shape VARCHAR(10),
    current_waiter_id INTEGER
);
"""


def test_orphaned_reference_raises(engine, label, is_postgres):
    """A hand-built, deliberately legacy schema — restaurant_table.zone_id
    has NO REFERENCES clause at all, unlike the real model — so an orphaned
    row can exist without ever disabling a constraint on a real schema."""
    _guard_if_postgres(engine)
    ddl = _LEGACY_NO_FK_POSTGRES if is_postgres else _LEGACY_NO_FK_SQLITE
    with engine.begin() as conn:
        # This engine may already carry the full create_all() schema from an
        # earlier test in this run, whose other tables (orders, etc.) hold
        # real FKs into restaurant_table/zone. On Postgres, CASCADE drops
        # only those FK constraints, not the referencing tables — fine here
        # since this test rebuilds both tables from scratch immediately and
        # is always the last scenario run against a given engine. SQLite has
        # no CASCADE keyword for DROP TABLE; PRAGMA foreign_keys=ON there
        # does not block dropping a referenced table outright (only DML that
        # violates a live FK), so the plain form is enough on that dialect.
        cascade = " CASCADE" if is_postgres else ""
        conn.execute(text(f"DROP TABLE IF EXISTS restaurant_table{cascade}"))
        conn.execute(text(f"DROP TABLE IF EXISTS zone{cascade}"))
        for stmt in ddl.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
        conn.execute(text(
            "INSERT INTO zone (floor_id, name, sort_order, is_active, pos_x, pos_y, width, height) "
            "VALUES (1, 'Main', 0, TRUE, 60, 60, 360, 300)"
        ))
        # zone_id=999 matches no zone row — orphaned by construction, on a
        # table with no FK to violate.
        conn.execute(text(
            "INSERT INTO restaurant_table (number, zone_id, zone, capacity, status, "
            "is_active, pos_x, pos_y, shape) VALUES (1, 999, 'Ghost', 4, 'free', TRUE, 3, 0, 'round')"
        ))

    raised = False
    message = ""
    try:
        migrate.run(engine, strict=True)
    except RuntimeError as exc:
        raised = True
        message = str(exc)
    check(raised, f"{label}: orphaned zone_id raises rather than being silently excluded")
    check("orphaned" in message.lower() or "999" in message, f"{label}: error names the orphaned table ({message!r})")
    # Step 0 runs before step 1 (the eligible-row SELECT) or step 2 (the
    # write loop) — no map position VALUE is ever computed or written for
    # any row before this raise. (The ADD COLUMN itself, from the earlier,
    # separate ADDED_COLUMNS loop, may already have committed by this point
    # regardless of dialect — that is that loop's own DDL-commit behaviour,
    # not something step 0's ordering controls; see this file's DDL-rollback
    # findings above.)
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT COUNT(*) FROM restaurant_table WHERE map_x_per_mille IS NOT NULL "
            "OR map_y_per_mille IS NOT NULL"
        )).scalar_one()
    check(n == 0, f"{label}: no row's map position was ever written before the step-0 raise")


# --------------------------------------------------------------------------
# Guard unit tests — pure string/URL logic, no network I/O, no real
# database, run unconditionally (even without PG_TEST_DSN, even on a machine
# with no PostgreSQL reachable at all).
# --------------------------------------------------------------------------

def run_guard_unit_tests():
    real_env = os.environ.get(_ALLOW_DESTRUCTIVE_ENV)

    def _set_allow(value):
        if value is None:
            os.environ.pop(_ALLOW_DESTRUCTIVE_ENV, None)
        else:
            os.environ[_ALLOW_DESTRUCTIVE_ENV] = value

    def _rejects(dsn) -> str:
        """Returns the exception message if assert_disposable_postgres_target
        raises UnsafePostgresTargetError, else "" (accepted)."""
        try:
            assert_disposable_postgres_target(dsn)
        except UnsafePostgresTargetError as exc:
            return str(exc)
        return ""

    try:
        safe_dsn = "postgresql+psycopg://user:pw@localhost:5433/floormap_test_db"

        # 1. Disposable-looking DSN + explicit opt-in: allowed.
        _set_allow("1")
        check(_rejects(safe_dsn) == "", "guard: disposable DSN + ALLOW_DESTRUCTIVE_PG_TESTS=1 is allowed")

        # 2. Same DSN, opt-in absent: rejected.
        _set_allow(None)
        msg = _rejects(safe_dsn)
        check(msg != "", "guard: same DSN without ALLOW_DESTRUCTIVE_PG_TESTS is rejected")
        check(_ALLOW_DESTRUCTIVE_ENV in msg, f"guard: rejection names the missing opt-in ({msg!r})")

        # 3. Database name without a test marker: rejected, even with opt-in.
        _set_allow("1")
        msg = _rejects("postgresql+psycopg://user:pw@localhost:5433/postgres")
        check(msg != "", "guard: database name without a 'test' marker is rejected")
        check("test" in msg.lower(), f"guard: rejection mentions the test-marker requirement ({msg!r})")

        # 3b. The "test" marker must be a delimited token, not any substring
        # match — re-review found the first version (`re.compile(r"test")`)
        # would have accepted "contest"/"latest"/"testament"/"protest" as if
        # they were disposable-test-named databases. Explicit, individual
        # coverage for all ten names, opt-in already set from above.
        _accept_names = ("test", "floor_test", "test_floor", "floor-test-db", "floor_test_01")
        _reject_names = ("contest", "latest", "testament", "protest", "restaurant")
        for name in _accept_names:
            msg = _rejects(f"postgresql+psycopg://user:pw@localhost:5433/{name}")
            check(msg == "", f"guard: database name {name!r} — delimited 'test' token — is accepted")
        for name in _reject_names:
            msg = _rejects(f"postgresql+psycopg://user:pw@localhost:5433/{name}")
            check(msg != "", f"guard: database name {name!r} — 'test' only as a substring, not a "
                              f"delimited token — is rejected")

        # 4. URL with no database name: rejected.
        msg = _rejects("postgresql+psycopg://user:pw@localhost:5433/")
        check(msg != "", "guard: URL with no database name is rejected")

        # 5. Non-PostgreSQL dialect: rejected.
        msg = _rejects("sqlite:///some_test.db")
        check(msg != "", "guard: non-PostgreSQL dialect (sqlite) is rejected")
        msg = _rejects("mysql+pymysql://user:pw@localhost/some_test_db")
        check(msg != "", "guard: non-PostgreSQL dialect (mysql) is rejected")

        # 6. Known production-hosting suffix: rejected, even with a
        # test-named database and the opt-in set — the host check is not
        # overridable by the other checks passing.
        msg = _rejects("postgresql+psycopg://user:pw@mydb.us-east-1.rds.amazonaws.com:5432/floormap_test")
        check(msg != "", "guard: known production-hosting suffix is rejected even with a test-named database + opt-in")
        check("amazonaws.com" in msg, f"guard: rejection names the offending suffix ({msg!r})")
        msg = _rejects("postgresql+psycopg://user:pw@my-app-db.oregon-postgres.render.com/floormap_test")
        check(msg != "", "guard: this project's documented Render Postgres host suffix is rejected")

        # 7. No destructive operation — no I/O of any kind — occurs on
        # rejection. assert_disposable_postgres_target is pure string/URL
        # parsing (make_url never resolves DNS or opens a socket); proven
        # two ways: (a) the raised type is exactly this guard's own
        # exception, never a network/connection error class that a real
        # connection attempt to an unreachable host would raise instead,
        # and (b) the check completes near-instantly — a real attempt to
        # reach a nonexistent AWS RDS host would hang for a TCP-connect
        # timeout (seconds), not return immediately.
        import time
        dangerous_dsn = "postgresql+psycopg://user:pw@mydb.us-east-1.rds.amazonaws.com:5432/floormap_test"
        started = time.perf_counter()
        exc_type = None
        try:
            assert_disposable_postgres_target(dangerous_dsn)
        except Exception as exc:                       # noqa: BLE001 — capture whatever type, then inspect it
            exc_type = type(exc)
        elapsed = time.perf_counter() - started
        check(exc_type is UnsafePostgresTargetError,
              f"guard: rejecting a real-shaped-but-unreachable production host raises this guard's own "
              f"exception type, not a network error ({exc_type})")
        check(elapsed < 1.0,
              f"guard: rejection is near-instant ({elapsed:.4f}s) — no network I/O was attempted "
              f"(a real connection attempt to an unreachable host would take much longer)")
    finally:
        _set_allow(real_env)


# --------------------------------------------------------------------------
# Regression — existing migration/Floor/Reservations suites still pass.
# --------------------------------------------------------------------------

def run_regression():
    import subprocess
    py = sys.executable
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("test_migrate.py", "test_floor_spatial.py", "test_reservations_map.py"):
        path = os.path.join(here, name)
        if not os.path.exists(path):
            check(False, f"regression: {name} not found")
            continue
        result = subprocess.run([py, path], cwd=os.path.dirname(here), capture_output=True, text=True)
        check(result.returncode == 0, f"regression: {name} exit code {result.returncode}")
        if result.returncode != 0:
            print("    ---", name, "stdout ---")
            print("    " + result.stdout.replace("\n", "\n    "))
            print("    ---", name, "stderr ---")
            print("    " + result.stderr.replace("\n", "\n    "))


if __name__ == "__main__":
    print("== SQLite ==")
    test_fresh_sqlite_has_null_columns()
    test_legacy_missing_columns_gets_backfilled(_sqlite_engine(), "SQLite")
    test_formula_multi_row_multi_floor(_sqlite_engine(), "SQLite")
    test_clamps(_sqlite_engine(), "SQLite")
    test_ineligible_tables_stay_null(_sqlite_engine(), "SQLite")
    test_idempotent(_sqlite_engine(), "SQLite")
    test_becomes_eligible_later(_sqlite_engine(), "SQLite")
    test_postcondition_failure_rolls_back(_sqlite_engine(), "SQLite", is_postgres=False)
    test_orphaned_reference_raises(_sqlite_engine(), "SQLite", is_postgres=False)

    if not pg_dsn():
        print("SKIP: PostgreSQL-specific scenarios (PG_TEST_DSN not set)")
    else:
        # Fail-closed guard, run once here for a single clear message before
        # anything else touches Postgres at all — never prints the DSN,
        # password, or username; individual test helpers (_guard_if_postgres)
        # re-check this independently before each destructive operation, so
        # this is the friendly early exit, not the only enforcement point.
        try:
            assert_disposable_postgres_target(pg_dsn())
        except UnsafePostgresTargetError as exc:
            print(f"SKIP: PostgreSQL-specific scenarios — target rejected by the "
                  f"disposable-database guard: {exc}")
            pg_url = None
        else:
            pg_url = make_url(pg_dsn())

        if pg_url is not None:
            safe = f"{pg_url.host}/{pg_url.database}"     # never the password or username
            print(f"== PostgreSQL ({safe}) ==")
            pg = lambda: create_engine(pg_dsn(), future=True)
            test_legacy_missing_columns_gets_backfilled(pg(), "PostgreSQL")
            test_postgres_backfill_actually_runs(pg())
            test_formula_multi_row_multi_floor(pg(), "PostgreSQL")
            test_clamps(pg(), "PostgreSQL")
            test_ineligible_tables_stay_null(pg(), "PostgreSQL")
            test_idempotent(pg(), "PostgreSQL")
            test_becomes_eligible_later(pg(), "PostgreSQL")
            test_postcondition_failure_rolls_back(pg(), "PostgreSQL", is_postgres=True)
            test_orphaned_reference_raises(pg(), "PostgreSQL", is_postgres=True)

    print("== PostgreSQL guard unit tests ==")
    run_guard_unit_tests()

    print("== Regression ==")
    run_regression()

    if _failures:
        print(f"\n{len(_failures)} FAILED:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall Floor Plan Builder coordinate tests passed")
