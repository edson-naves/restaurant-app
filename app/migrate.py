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

import math
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services.zone_geometry import place_new_zone_rect

# (table, column, DDL type + default). The default matters: existing rows are
# backfilled with it, so it has to be the value that preserves current
# behaviour — every table that exists today is an active one.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("restaurant_table", "is_active", "BOOLEAN NOT NULL DEFAULT 1"),
    ("restaurant_table", "zone_id", "INTEGER REFERENCES zone(id)"),
    # Nullable on purpose: Zone.swatch falls back to the palette, so existing
    # zones are colour-coded without having to backfill a value.
    ("zone", "color", "VARCHAR(7)"),
    # Sales tax (GST + PST combined). Defaulting to 0 keeps pre-tax payments'
    # stored total consistent (items - discount + tip) so reconciliation still
    # ties. The two fact columns exist because ETL reloads into a table
    # create_all already made.
    ("payment", "tax_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("fact_payment", "tax_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("fact_order_header", "tax_cents", "INTEGER NOT NULL DEFAULT 0"),
    # Void trail. Defaulting voided to 0 leaves every existing payment live.
    ("payment", "voided", "BOOLEAN NOT NULL DEFAULT 0"),
    ("payment", "voided_at", "TIMESTAMP"),
    ("payment", "voided_by_id", "INTEGER REFERENCES staff(id)"),
    ("payment", "void_reason", "VARCHAR(200) DEFAULT ''"),
    # Post-settlement refunds. Header column defaults 0 so pre-refund orders'
    # net still equals their gross and reconciliation ties.
    ("fact_order_header", "refund_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("day_close", "refund_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("day_close", "cash_refund_cents", "INTEGER NOT NULL DEFAULT 0"),
    # 86 / out-of-stock. Defaults available so every existing item stays sellable.
    ("menu_item", "available", "BOOLEAN NOT NULL DEFAULT 1"),
    # Coursing. Default 2 (Mains) so existing lines fire as before.
    ("order_item", "course", "INTEGER NOT NULL DEFAULT 2"),
    ("order_item", "merged_from_order_id", "INTEGER REFERENCES \"order\"(id)"),
    ("order_item", "allergens", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("menu_item", "image_url", "VARCHAR(300) NOT NULL DEFAULT ''"),
    ("payment", "service_charge_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("fact_payment", "service_charge_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("fact_order_header", "service_charge_cents", "INTEGER NOT NULL DEFAULT 0"),
    # Optional card surcharge. Defaulting to 0 keeps every existing payment's
    # stored total (items - discount + tax + tip + service_charge) unchanged and
    # reconciling until a rate is set.
    ("payment", "card_surcharge_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("fact_payment", "card_surcharge_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("fact_order_header", "card_surcharge_cents", "INTEGER NOT NULL DEFAULT 0"),
    # Schedule positions. The shift table was created without this column in an
    # earlier deploy; add it (the position table itself is created by create_all).
    ("shift", "position_id", "INTEGER REFERENCES position(id)"),
    # A staff member's default/usual position, so their dragged shifts auto-colour.
    ("staff", "position_id", "INTEGER REFERENCES position(id)"),
    # Phase 2: hourly pay (labor cost) and an optional avatar (data-URI TEXT, so
    # it works on SQLite and Postgres alike and needs no upload disk).
    ("staff", "wage_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("staff", "photo", "TEXT"),
    ("staff", "availability_note", "VARCHAR(60) NOT NULL DEFAULT ''"),
    # Day-menu (prix fixe) grouping on an order line. NULL combo_id = an ordinary
    # à-la-carte line, so existing rows keep behaving exactly as before.
    ("order_item", "combo_id", "INTEGER"),
    ("order_item", "combo_name", "VARCHAR(80) NOT NULL DEFAULT ''"),
    # Day-menu happy-hour pricing + timeframe. Defaulting discount_type to
    # 'fixed' keeps every existing menu at its stored price_cents; the percent
    # and time columns are NULL (all-day, no discount) until a menu opts in.
    ("day_menu", "discount_type", "VARCHAR(10) NOT NULL DEFAULT 'fixed'"),
    ("day_menu", "discount_percent", "INTEGER"),
    ("day_menu", "start_time", "VARCHAR(5)"),
    ("day_menu", "end_time", "VARCHAR(5)"),
    # A swap request can now propose a new day/time for the shift (reschedule).
    # TIMESTAMP (not DATETIME) — DATETIME is not a Postgres type; TIMESTAMP works
    # on both Postgres and SQLite, so the ADD COLUMN succeeds on the live DB.
    ("swap_request", "new_starts_at", "TIMESTAMP"),
    ("swap_request", "new_ends_at", "TIMESTAMP"),
    # Happy-hour pricing snapshot on an order line (services/happyhour). All NULL /
    # false on existing rows = an ordinary line, so pre-happy-hour orders bill
    # exactly as before. The happy_hour* tables themselves are made by create_all.
    ("order_item", "hh_id", "INTEGER REFERENCES happy_hour(id)"),
    ("order_item", "hh_percent", "INTEGER"),
    ("order_item", "hh_full_cents", "INTEGER"),
    ("order_item", "hh_hold_until", "TIMESTAMP"),
    ("order_item", "hh_reverted", "BOOLEAN NOT NULL DEFAULT 0"),
    # Kitchen Stations (Stage A). The `station` table itself is created by
    # create_all; these two columns route/snapshot items to it. Both NULL by
    # default = Unassigned, so existing menus and open orders are untouched (no
    # item is silently auto-routed — the manager assigns stations explicitly).
    ("menu_item", "station_id", "INTEGER REFERENCES station(id)"),
    ("order_item", "station_id", "INTEGER REFERENCES station(id)"),
    # Kitchen Stations (Stage B1) — modifier-level routing config (NULL = prepared
    # with the item). The preparation_task / preparation_task_modifier tables are
    # created by create_all. Additive; existing menus/orders untouched.
    ("modifier", "station_id", "INTEGER REFERENCES station(id)"),
    ("modifier_option", "station_id", "INTEGER REFERENCES station(id)"),
    # Pluggable payment providers. Defaulting to 'manual' leaves every existing
    # instrument settling exactly as before (staff-recorded, no processor).
    ("payment_instrument", "provider", "VARCHAR(30) NOT NULL DEFAULT 'manual'"),
    # PaymentAttempt hardening (review Stages 2a/2b): intent fingerprint,
    # processor-confirmed amount/currency, and reconciliation evidence. New
    # UNIQUE(provider, provider_*_id) constraints ship on fresh databases via
    # create_all; an already-created payment_attempt table only needs the columns.
    ("payment_attempt", "intent_fingerprint", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("payment_attempt", "processor_amount_cents", "INTEGER"),
    ("payment_attempt", "processor_currency", "VARCHAR(3)"),
    ("payment_attempt", "reconciled_at", "TIMESTAMP"),
    ("payment_attempt", "reconciled_by", "VARCHAR(60) NOT NULL DEFAULT ''"),
    ("payment_attempt", "reconciliation_note", "VARCHAR(300) NOT NULL DEFAULT ''"),
    ("refund_attempt", "intent_fingerprint", "VARCHAR(64) NOT NULL DEFAULT ''"),
    # Zone rectangle on the floor map (per-mille-ish pixel box) and table shape,
    # for the reservation picker's map. Defaults match the model's Python-level
    # defaults, so an existing zone/table lands in the same place create_all
    # would have put a brand-new one.
    ("zone", "pos_x", "INTEGER NOT NULL DEFAULT 60"),
    ("zone", "pos_y", "INTEGER NOT NULL DEFAULT 60"),
    ("zone", "width", "INTEGER NOT NULL DEFAULT 360"),
    ("zone", "height", "INTEGER NOT NULL DEFAULT 300"),
    ("restaurant_table", "shape", "VARCHAR(10) NOT NULL DEFAULT 'round'"),
    # Waiter colour on the floor plan (nullable — falls back to STAFF_PALETTE by
    # id, so an existing waiter is coloured without a backfill) and the
    # seated->attended lead-time stamp (nullable — no historical value to backfill).
    ("staff", "color", "VARCHAR(7)"),
    ("order", "attended_at", "TIMESTAMP"),
    # Floor Plan Builder — free-placement (per-mille) coordinates. Nullable, no
    # default: NULL means "not placed on the free map yet", never reinterpreted
    # from pos_x/pos_y (the existing grid columns, left untouched). Column
    # creation alone leaves every row NULL on both dialects; the separate
    # backfill below (_backfill_table_map_positions, wired into run() and
    # _run_postgres() independently) is what fills eligible existing tables in.
    # See docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md §3-4.
    ("restaurant_table", "map_x_per_mille", "INTEGER"),
    ("restaurant_table", "map_y_per_mille", "INTEGER"),
)

# (table, column, min_length, new DDL type). Columns whose type/length GREW
# after the database was first created. Postgres enforces VARCHAR length (SQLite
# does not), so an existing narrow column must be widened or a longer write
# overflows with "value too long". Widening never truncates, so it is safe.
WIDENED_COLUMNS: tuple[tuple[str, str, int, str], ...] = (
    # PINs are stored as ~119-char salted PBKDF2 hashes now, not 4–8 digit
    # plaintext, so the old VARCHAR(8) overflows on the first login that upgrades
    # a legacy PIN to a hash.
    ("staff", "pin_code", 128, "VARCHAR(128)"),
    # PaymentAttempt.provider grew VARCHAR(20) -> VARCHAR(30) (shared
    # PROVIDER_KEY_LEN) when 'square' stopped being a silent default.
    ("payment_attempt", "provider", 30, "VARCHAR(30)"),
)

# Provider-scoped uniqueness added to *existing* payment tables. create_all()
# builds these on a fresh DB but never adds a constraint to a table that already
# exists, so an upgraded Stage-2a database needs them applied explicitly. Each is
# duplicate-scanned and fail-closed before creation (see _migrate_payment_hardening).
UNIQUE_CONSTRAINTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("payment_attempt", "uq_attempt_provider_payment", ("provider", "provider_payment_id")),
    ("payment_attempt", "uq_attempt_provider_checkout", ("provider", "provider_checkout_id")),
)

# Canonical provider backfill: the retired 'square' default becomes the real
# registry key before the provider-scoped constraints are applied.
PROVIDER_BACKFILL = (("payment_attempt", "square", "square_terminal"),)

DEFAULT_FLOOR = "1st floor"

# Must match app/routers/admin.py's GRID_COLS (10) — the Floor Plan Builder
# backfill derives per-mille positions from the same grid the current admin
# table editor uses. Not imported from there: app/routers/admin.py pulls in
# the full FastAPI/router/template stack, and this module has no app-internal
# imports today — a single stable integer isn't worth a layering violation.
GRID_COLS = 10


class MigrationError(RuntimeError):
    """A schema migration failed. Raised in strict mode so deployment aborts
    rather than starting the app against a mismatched schema."""


def run(engine: Engine, strict: bool = False) -> list[str]:
    """Apply any missing columns, then backfill. Returns what changed.

    These additive migrations exist to evolve an *existing* database whose
    schema predates a column. SQLite (dev) is introspected with PRAGMA; Postgres
    (prod on Render) with information_schema. A *fresh* database gets the full
    current schema from Base.metadata.create_all(), so the checks below simply
    find every column present and do nothing — but an existing Postgres created
    before a column was added still needs it, which is what the Postgres branch
    handles (previously it was skipped, so new columns never reached prod).

    ``strict`` (production): a genuine ALTER failure raises ``MigrationError``
    instead of being recorded as ``SKIPPED …`` and swallowed, so the app never
    starts expecting a newer schema than the database actually has.
    """
    if engine.dialect.name != "sqlite":
        return _run_postgres(engine, strict=strict)
    applied: list[str] = []
    with engine.begin() as conn:
        for table, column, ddl in ADDED_COLUMNS:
            # Quoted: "order" (and any future reserved word) is a valid table
            # name here but not valid unquoted SQL — PRAGMA/ALTER both need it.
            rows = conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
            if not rows:
                continue                       # table not created yet; create_all owns it
            if any(r[1] == column for r in rows):
                continue                       # already migrated
            conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {column} {ddl}'))
            applied.append(f"{table}.{column}")

        applied.extend(_backfill_locations(conn))
        # Floor Plan Builder — same nominal transaction as the column ALTERs
        # above, fail-closed rather than best-effort (function docstring;
        # docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md §4.2). MEASURED
        # CAVEAT the design doc did not anticipate: under this driver
        # (pysqlite via SQLAlchemy), `ALTER TABLE ADD COLUMN` auto-commits
        # the instant it executes — DDL is not actually rolled back by this
        # `with engine.begin()` block on an exception, only plain DML is
        # (verified directly; the same is true of every other ADD COLUMN in
        # this file, not something introduced here). So a raise here leaves
        # the two new columns in place but every row NULL — the DML (this
        # function's UPDATEs) does roll back correctly. That end state is
        # still safe and idempotent (identical to "columns exist, nothing
        # backfilled yet" — exactly what the next run expects), just not by
        # the "everything vanishes together" mechanism the design describes.
        applied.extend(_backfill_table_map_positions(conn))
    # Its own transaction, deliberately separate from the column-ALTER block
    # above: pure DML (no ALTER involved), so unlike that block it has no
    # auto-commit quirk to share — a raise here rolls back cleanly on its
    # own and never touches the already-committed columns. Narrowly
    # fail-closed (a genuine internal defect still halts startup), but an
    # unresolved zone never does — see _backfill_zone_overlap's own
    # docstring, "Availability policy".
    with engine.begin() as conn:
        _zone_overlap = _backfill_zone_overlap(conn)
        applied.extend(_zone_overlap.moved)
        applied.extend(_zone_overlap.unresolved)
    # The fire-batch unique index is a REQUIRED invariant — run it in its own
    # transaction (after the additive column work has committed) so that if it
    # halts on pre-existing duplicates, it does not roll back the column adds.
    with engine.begin() as conn:
        _ensure_prep_task_index(conn)
    # The backfill is best-effort but OBSERVABLE: a real failure is reported in the
    # applied log (not silently converted to no rows), while it never blocks startup.
    try:
        with engine.begin() as conn:
            applied.extend(_backfill_prep_tasks(conn))
    except Exception as exc:                    # noqa: BLE001 — surface, never block
        applied.append(f"SKIPPED prep-task backfill: {exc}")
    # SQLite does not enforce VARCHAR length, so no column ever needs widening
    # there — the model's new size applies to fresh databases via create_all.
    applied.extend(_migrate_payment_hardening(engine, strict))
    return applied


def _pg_boolean_ddl(ddl: str) -> str:
    """Rewrite a SQLite-style boolean default to valid Postgres for the PG branch.

    The ADDED_COLUMNS DDL is authored for SQLite, where a BOOLEAN column takes an
    integer default (`DEFAULT 0` / `DEFAULT 1`). Postgres has a real boolean type
    and rejects an integer default on it ("column is of type boolean but default
    expression is of type integer", DatatypeMismatch) — which silently SKIPs the
    ADD COLUMN, so the column never lands and every query touching it 500s. Here
    we translate the default to its boolean literal (`false` / `true`) for boolean
    columns only; non-boolean DDL and already-boolean literals pass through
    untouched. Applied on the Postgres branch alone — SQLite keeps the raw DDL.
    """
    if "BOOLEAN" not in ddl.upper():
        return ddl
    ddl = re.sub(r"(DEFAULT\s+)1\b", r"\1true", ddl, flags=re.IGNORECASE)
    ddl = re.sub(r"(DEFAULT\s+)0\b", r"\1false", ddl, flags=re.IGNORECASE)
    return ddl


def _run_postgres(engine: Engine, strict: bool = False) -> list[str]:
    """Additive ADD COLUMN for an existing Postgres database (Render).

    Only columns that are genuinely missing are added, checked against
    information_schema. Every column already present (a fresh DB has them all
    from create_all) is skipped, so in practice this only ever adds the newest
    columns. The ADDED_COLUMNS DDL is authored for SQLite, so boolean defaults
    are translated to Postgres literals via _pg_boolean_ddl before each ALTER (an
    integer default on a boolean column is a Postgres DatatypeMismatch). Each
    ALTER runs in its own transaction and is guarded, so one failure can't abort
    the others or block startup.
    """
    applied: list[str] = []
    for table, column, ddl in ADDED_COLUMNS:
        with engine.connect() as conn:
            exists = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = :t AND table_schema = current_schema()"
                ),
                {"t": table},
            ).first()
            if not exists:
                continue                       # create_all owns a not-yet-made table
            has_col = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c "
                    "AND table_schema = current_schema()"
                ),
                {"t": table, "c": column},
            ).first()
            if has_col:
                continue                       # already migrated
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS {column} {_pg_boolean_ddl(ddl)}')
                )
            applied.append(f"{table}.{column}")
        except Exception as exc:               # noqa: BLE001
            if strict:
                raise MigrationError(f"failed to add {table}.{column}: {exc}") from exc
            applied.append(f"SKIPPED {table}.{column}: {exc}")

    # Widen any column that outgrew its original length (e.g. pin_code now holds
    # a hash). Guarded on the current max length so it only runs once.
    for table, column, min_len, ddl in WIDENED_COLUMNS:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c "
                    "AND table_schema = current_schema()"
                ),
                {"t": table, "c": column},
            ).first()
        if row is None:
            continue                           # column/table absent; create_all owns it
        cur_len = row[0]
        if cur_len is None or cur_len >= min_len:
            continue                           # already wide enough (or unbounded TEXT)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(f'ALTER TABLE "{table}" ALTER COLUMN {column} TYPE {ddl}')
                )
            applied.append(f"widened {table}.{column} -> {ddl}")
        except Exception as exc:               # noqa: BLE001
            if strict:
                raise MigrationError(f"failed to widen {table}.{column}: {exc}") from exc
            applied.append(f"SKIPPED widen {table}.{column}: {exc}")
    # Required invariant — NOT swallowed (halts if pre-existing duplicates make
    # the unique index impossible).
    with engine.begin() as conn:
        _ensure_prep_task_index(conn)
    # Floor Plan Builder — its own transaction, deliberately NOT wrapped in a
    # try/except: the ADD COLUMNs above are already committed (each in its
    # own transaction) by the time this runs, so a raise here only rolls back
    # this backfill's own writes, never the columns — but it must still raise,
    # not log "SKIPPED" and continue, on both dialects (fail-closed; see the
    # function's docstring and docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md
    # §4.2). There is no existing Postgres call site to piggyback on for this
    # backfill — added here from scratch, the same way _backfill_prep_tasks's
    # own Postgres call (below) was added alongside its SQLite one rather than
    # assumed.
    with engine.begin() as conn:
        applied.extend(_backfill_table_map_positions(conn))
    # Its own transaction, same reasoning as the SQLite call site in run():
    # pure DML, no ALTER, so no auto-commit quirk to worry about on either
    # dialect — a raise here rolls back cleanly on its own. Narrowly
    # fail-closed, never for an honestly-unresolved zone (see
    # _backfill_zone_overlap's own docstring, "Availability policy").
    with engine.begin() as conn:
        _zone_overlap = _backfill_zone_overlap(conn)
        applied.extend(_zone_overlap.moved)
        applied.extend(_zone_overlap.unresolved)
    # The backfill itself stays best-effort (a missing legacy task is caught by
    # the B2 readiness invariant, not a silent unprotected duplicate risk).
    try:
        with engine.begin() as conn:
            applied.extend(_backfill_prep_tasks(conn))
    except Exception as exc:                    # noqa: BLE001 — never block startup
        applied.append(f"SKIPPED prep-task backfill: {exc}")

    applied.extend(_migrate_payment_hardening(engine, strict))
    return applied


def _column_exists(conn, table: str, column: str) -> bool:
    if conn.engine.dialect.name == "sqlite":
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == column for r in rows)
    return conn.execute(
        text("SELECT 1 FROM information_schema.columns WHERE table_name=:t "
             "AND column_name=:c AND table_schema=current_schema()"),
        {"t": table, "c": column},
    ).first() is not None


def _constraint_exists(conn, table: str, name: str) -> bool:
    if conn.engine.dialect.name == "sqlite":
        # A named UNIQUE constraint surfaces as an index (from create_all's inline
        # constraint) or as our upgrade index — either proves enforcement exists.
        got = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name=:t "
                 "AND (name=:n OR sql LIKE :like)"),
            {"t": table, "n": name, "like": "%UNIQUE%"},
        ).fetchall()
        # Fall back to reading the table's own UNIQUE constraint list.
        idx = conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
        return bool(got) and any(r[2] for r in idx)  # any unique index present
    return conn.execute(
        text("SELECT 1 FROM information_schema.table_constraints WHERE constraint_name=:n "
             "AND table_name=:t AND table_schema=current_schema()"),
        {"n": name, "t": table},
    ).first() is not None


def _duplicates(conn, table: str, cols: tuple[str, ...]) -> list[tuple]:
    """Rows that would violate a unique constraint on ``cols`` (NULLs excluded,
    since NULL keeps a row distinct on both engines)."""
    collist = ", ".join(cols)
    notnull = " AND ".join(f"{c} IS NOT NULL" for c in cols)
    q = (f"SELECT {collist}, COUNT(*) AS n FROM {table} WHERE {notnull} "
         f"GROUP BY {collist} HAVING COUNT(*) > 1")
    return conn.execute(text(q)).fetchall()


def _migrate_payment_hardening(engine: Engine, strict: bool) -> list[str]:
    """Upgrade an existing payment_attempt table to the hardened schema:
    canonical provider backfill, retire the old provider_refund_id column, and
    add the provider-scoped UNIQUE constraints — duplicate-scanned and fail-closed
    so financial data is never silently rewritten. No-op on a fresh DB where
    create_all already produced everything.

    **Atomicity (#2):** the whole hardening runs in a *single* transaction
    (Postgres has transactional DDL), so a failure at any step — a duplicate
    constraint, non-null legacy data under strict — rolls back every earlier step
    (default drop, backfills). The database is never left partially hardened. It
    is also idempotent: a second run finds everything already applied and changes
    nothing.
    """
    applied: list[str] = []
    pg = engine.dialect.name != "sqlite"
    q = (lambda s: f'"{s}"') if pg else (lambda s: s)

    with engine.begin() as conn:
        # 1. Canonical provider backfill (before the provider-scoped constraints).
        for table, old, new in PROVIDER_BACKFILL:
            if not _column_exists(conn, table, "provider"):
                continue
            n = conn.execute(text(f"UPDATE {q(table)} SET provider=:new WHERE provider=:old"),
                             {"new": new, "old": old}).rowcount
            if n:
                applied.append(f"backfilled {table}.provider {old}->{new} ({n} rows)")

        # 1b. A terminal-card instrument that predates the provider column takes the
        # 'manual' default; repair ONLY that legacy state (#1). Narrowing to
        # provider='manual' means a deliberately-chosen provider (e.g. a future
        # stripe_terminal) is never overwritten, and a re-run is a no-op.
        if _column_exists(conn, "payment_instrument", "provider"):
            n = conn.execute(text(
                "UPDATE payment_instrument SET provider='square_terminal' "
                "WHERE code='card_terminal' AND provider='manual'")).rowcount
            if n:
                applied.append(f"backfilled payment_instrument card_terminal->square_terminal ({n})")

        # 1c. Remove the retired legacy DEFAULT 'square' on payment_attempt.provider
        # (widening the type does not drop it). Postgres only; SQLite's model has no
        # server default and cannot DROP DEFAULT without a table rebuild.
        if pg and _column_exists(conn, "payment_attempt", "provider"):
            has_default = conn.execute(text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name='payment_attempt' AND column_name='provider' "
                "AND table_schema=current_schema()")).scalar_one_or_none()
            if has_default is not None:
                conn.execute(text('ALTER TABLE payment_attempt ALTER COLUMN provider DROP DEFAULT'))
                applied.append("dropped legacy default on payment_attempt.provider")

        # 2. Retire the old provider_refund_id. Drop only when empty; non-null under
        # strict fails closed (rolling back the whole tx).
        if _column_exists(conn, "payment_attempt", "provider_refund_id"):
            leftover = conn.execute(
                text("SELECT COUNT(*) FROM payment_attempt WHERE provider_refund_id IS NOT NULL")
            ).scalar_one()
            if leftover:
                msg = (f"payment_attempt.provider_refund_id still holds {leftover} non-null "
                       "row(s); migrate them into refund_attempt before upgrading. Financially "
                       "meaningful legacy data must not be left behind.")
                if strict:
                    raise MigrationError(msg)          # fail closed (rolls back — #6)
                applied.append(f"KEPT (non-strict): {msg}")
            else:
                try:
                    conn.execute(text('ALTER TABLE payment_attempt DROP COLUMN '
                                      + ("IF EXISTS " if pg else "") + "provider_refund_id"))
                    applied.append("dropped retired payment_attempt.provider_refund_id")
                except Exception as exc:  # noqa: BLE001 — SQLite <3.35 can't drop
                    if strict:
                        raise MigrationError(f"failed to drop provider_refund_id: {exc}") from exc
                    applied.append(f"SKIPPED drop provider_refund_id: {exc}")

        # 3. Provider-scoped uniqueness — dup-scan, fail closed, then create.
        for table, name, cols in UNIQUE_CONSTRAINTS:
            if not all(_column_exists(conn, table, c) for c in cols):
                continue
            if _constraint_exists(conn, table, name):
                continue
            dupes = _duplicates(conn, table, cols)
            if dupes:
                msg = (f"cannot add {name}: {len(dupes)} duplicate group(s) in "
                       f"{table}({', '.join(cols)}) — e.g. {dupes[0]}. Resolve before upgrading.")
                if strict:
                    raise MigrationError(msg)          # fail closed (rolls back)
                applied.append(f"BLOCKED {msg}")
                continue
            if pg:
                conn.execute(text(f'ALTER TABLE {q(table)} ADD CONSTRAINT {name} '
                                  f'UNIQUE ({", ".join(cols)})'))
            else:
                conn.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS {name} '
                                  f'ON {table} ({", ".join(cols)})'))
            applied.append(f"added {name} on {table}({', '.join(cols)})")

    return applied


def _table_exists(conn, name: str) -> bool:
    if conn.dialect.name == "sqlite":
        return conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}
        ).first() is not None
    return conn.execute(
        text("SELECT 1 FROM information_schema.tables "
             "WHERE table_name=:n AND table_schema=current_schema()"), {"n": name}
    ).first() is not None


def _index_exists(conn, name: str, table: str | None = None) -> bool:
    """Does an index named `name` exist? On Postgres this MUST be scoped to
    current_schema() (and the given table, when supplied) so an identically-named
    index in another schema — or on another table — is not mistaken for this one:
    the same scope `_prep_task_index_ok` uses. Otherwise `_ensure_prep_task_index`
    could see a foreign-schema index as a same-name/wrong-definition collision and
    halt startup when nothing is wrong with the current schema."""
    if conn.dialect.name == "sqlite":
        sql = "SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"
        params = {"n": name}
        if table is not None:
            sql += " AND tbl_name=:t"          # SQLite has no schemas; scope by table
            params["t"] = table
        return conn.execute(text(sql), params).first() is not None
    sql = "SELECT 1 FROM pg_indexes WHERE schemaname=current_schema() AND indexname=:n"
    params = {"n": name}
    if table is not None:
        sql += " AND tablename=:t"
        params["t"] = table
    return conn.execute(text(sql), params).first() is not None


_EXPECTED_INDEX_KEYS = ["order_item_id", "fire_seq", "coalesce(station_id,-1)"]


def _sqlite_index_keylist(sql: str) -> list[str] | None:
    """Paren-aware split of a CREATE INDEX's key list, so COALESCE(station_id,-1)
    stays one key (its inner comma is not a separator). Returns the ordered key
    expressions, or None if the statement can't be parsed."""
    m = re.search(r"\bon\b\s+\S+\s*\(", sql, re.IGNORECASE)
    if not m:
        return None
    depth = 0
    parts: list[str] = []
    cur: list[str] = []
    for ch in sql[m.end() - 1:]:
        if ch == "(":
            depth += 1
            if depth == 1:
                continue                     # skip the outer '('
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            if depth == 0:
                parts.append("".join(cur).strip())
                return [p for p in parts if p]
            cur.append(ch)
        elif ch == "," and depth == 1:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    return None                              # unbalanced parens


def _strip_wrapping_parens(s: str) -> str:
    """Drop paren pair(s) that enclose the WHOLE expression, so
    '(coalesce(station_id,-1))' == 'coalesce(station_id,-1)'. A paren that does
    not wrap the entire string (an arithmetic sub-group like the '(...+course)'
    in a tampered key) is left intact, so that key stays distinguishable."""
    while len(s) >= 2 and s.startswith("(") and s.endswith(")"):
        depth = 0
        wraps = True
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    wraps = False           # closed before the end → not a full wrap
                    break
        if not wraps:
            break
        s = s[1:-1]
    return s


def _pg_norm_key(defn: str) -> str:
    """Normalise a single pg_get_indexdef(idx, n, true) key expression for an
    EXACT compare: lowercase, drop spaces, strip ::type casts and quotes, then
    remove parens that wrap the whole expression. So `COALESCE(station_id,
    '-1'::integer)` and `(COALESCE(station_id, -1))` both become
    `coalesce(station_id,-1)`, while `(COALESCE(station_id,-1) + course)` and
    `ABS(COALESCE(station_id,-1))` normalise to something else and are rejected."""
    s = (defn or "").lower().replace(" ", "")
    s = re.sub(r"::[a-z0-9_\[\]]+", "", s)      # '-1'::integer -> '-1'
    s = s.replace("'", "").replace('"', "")
    return _strip_wrapping_parens(s)


def _prep_task_index_ok(conn) -> bool:
    """Prove the index enforces EXACTLY the fire-batch identity invariant —
    UNIQUE, non-partial, on preparation_task, keyed on exactly
    (order_item_id, fire_seq, COALESCE(station_id, -1)) with no extra key column
    and no other expression. Verifies the actual key structure, not substrings —
    so an extra key column, a partial index, or a wrong COALESCE expression is
    rejected even though the expected words appear in the DDL."""
    if conn.dialect.name == "sqlite":
        found = unique = partial = False
        for row in conn.execute(text("PRAGMA index_list('preparation_task')")):
            # (seq, name, unique, origin, partial)
            if row[1] == "uq_prep_task_batch":
                found, unique = True, bool(row[2])
                partial = bool(row[4]) if len(row) > 4 and row[4] is not None else False
                break
        if not found or not unique or partial:
            return False
        sql = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='uq_prep_task_batch' AND tbl_name='preparation_task'"
        )).scalar_one_or_none()
        if not sql or " where " in sql.lower():          # reject partial (belt)
            return False
        keys = _sqlite_index_keylist(sql)
        if keys is None:
            return False
        return [k.lower().replace(" ", "") for k in keys] == _EXPECTED_INDEX_KEYS

    # Postgres — inspect each index KEY individually via the catalog, never a
    # substring of the whole indexdef. `indnkeyatts` is the number of uniqueness
    # key columns (Postgres >= 11); `indnatts` includes any INCLUDE columns.
    # Requiring both = 3 means exactly three key columns and NO INCLUDE columns —
    # so an extra key or an INCLUDE payload is rejected.
    row = conn.execute(text(
        "SELECT i.indisunique, (i.indpred IS NULL) AS not_partial, "
        "       i.indnkeyatts, i.indnatts, i.indexrelid "
        "FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE c.relname = 'uq_prep_task_batch' AND t.relname = 'preparation_task' "
        "AND n.nspname = current_schema()"
    )).first()
    if row is None:
        return False
    is_unique, not_partial, nkeyatts, natts, idxoid = row
    if not is_unique or not not_partial or nkeyatts != 3 or natts != 3:
        return False
    # Reconstruct each key on its own — an arithmetic wrap, a wrapping function,
    # an extra fallback, or a wrong fallback all change one of these three and fail.
    keys = conn.execute(text(
        "SELECT pg_get_indexdef(:oid, 1, true), "
        "       pg_get_indexdef(:oid, 2, true), "
        "       pg_get_indexdef(:oid, 3, true)"
    ), {"oid": idxoid}).first()
    if keys is None or any(k is None for k in keys):
        return False
    k1, k2, k3 = (_pg_norm_key(k) for k in keys)
    if k1 != "order_item_id" or k2 != "fire_seq":
        return False
    return k3 in ("coalesce(station_id,-1)", "coalesce(station_id,(-1))")


def _ensure_prep_task_index(conn) -> None:
    """Establish the REQUIRED fire-batch uniqueness index — the durable guard B1
    depends on. NOT best-effort:

      * pre-existing duplicate identities → HALT (raise), nothing deleted/merged;
      * a same-name index with the WRONG definition → HALT (do not silently drop);
      * after CREATE, verify the index BY DEFINITION (unique + the exact columns),
        not merely that a name exists → HALT if it isn't right.

    (create_all builds this index on a fresh DB; here we add it to an existing one.)
    """
    if not _table_exists(conn, "preparation_task"):
        return                                   # create_all owns a not-yet-made table
    dups = conn.execute(text(
        "SELECT COUNT(*) FROM (SELECT 1 FROM preparation_task "
        "GROUP BY order_item_id, fire_seq, COALESCE(station_id, -1) HAVING COUNT(*) > 1) d"
    )).scalar_one()
    if dups:
        raise RuntimeError(
            f"Kitchen Stations B1: {dups} duplicate PreparationTask fire-batch "
            "identity(ies) already exist — the required unique index "
            "uq_prep_task_batch (order_item_id, fire_seq, COALESCE(station_id,-1)) "
            "cannot be created. No rows were changed. Resolve the duplicates, then "
            "restart. Startup halted."
        )
    # A name collision with a wrong definition would make CREATE IF NOT EXISTS a
    # no-op, leaving the invariant unenforced — halt instead of silently dropping.
    if _index_exists(conn, "uq_prep_task_batch", "preparation_task") and not _prep_task_index_ok(conn):
        raise RuntimeError(
            "Kitchen Stations B1: an index named uq_prep_task_batch exists but does "
            "NOT enforce UNIQUE(order_item_id, fire_seq, COALESCE(station_id,-1)). "
            "It was not dropped automatically. Resolve it manually, then restart. "
            "Startup halted."
        )
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_prep_task_batch "
        "ON preparation_task (order_item_id, fire_seq, COALESCE(station_id, -1))"
    ))
    if not _prep_task_index_ok(conn):
        raise RuntimeError(
            "Kitchen Stations B1: the required unique fire-batch index is not present "
            "by definition after creation — the duplicate-task guard is not in place. "
            "Startup halted."
        )


def _backfill_prep_tasks(conn) -> list[str]:
    """Kitchen Stations B1 — create one base PreparationTask per already-fired
    OrderItem (preparing/ready) that has none yet, from its legacy station_id
    snapshot. Idempotent (guarded on NOT EXISTS); never touches order_item, never
    re-reads current menu routing, preserves Unassigned as NULL station. Safe to
    run repeatedly. (The preparation_task table itself is made by create_all.)
    """
    # A legitimately absent table is a safe no-op; any OTHER database error is
    # allowed to propagate to the caller (surfaced/logged), not hidden as "[]".
    if not _table_exists(conn, "preparation_task"):
        return []
    pending = conn.execute(text(
        "SELECT COUNT(*) FROM order_item oi "
        "WHERE oi.kitchen_status IN ('preparing','ready') "
        "AND NOT EXISTS (SELECT 1 FROM preparation_task pt WHERE pt.order_item_id = oi.id)"
    )).scalar_one()
    if not pending:
        return []
    now = datetime.now()
    # Concurrency-safe against two startup processes racing: NOT EXISTS is the
    # sequential guard; a conflict-safe INSERT (OR IGNORE on SQLite / ON CONFLICT
    # DO NOTHING on Postgres) against the uq_prep_task_batch index is the durable
    # one — so a duplicate base task can never be inserted, even under a race.
    is_sqlite = conn.dialect.name == "sqlite"
    head = "INSERT OR IGNORE INTO" if is_sqlite else "INSERT INTO"
    tail = "" if is_sqlite else " ON CONFLICT DO NOTHING"
    conn.execute(text(
        head + ' preparation_task '
        '(order_item_id, order_id, station_id, course, kitchen_status, quantity, '
        ' item_label, is_base, fire_seq, fired_at, ready_at, served_at, created_at, updated_at) '
        'SELECT oi.id, oi.order_id, oi.station_id, oi.course, oi.kitchen_status, oi.quantity, '
        "       COALESCE((SELECT name FROM menu_item WHERE id = oi.menu_item_id), ''), :is_base, 1, "
        '       COALESCE((SELECT sent_to_kitchen_at FROM "order" WHERE id = oi.order_id), :now), '
        "       CASE WHEN oi.kitchen_status = 'ready' THEN :now ELSE NULL END, "
        '       NULL, :now, :now '
        'FROM order_item oi '
        "WHERE oi.kitchen_status IN ('preparing','ready') "
        'AND NOT EXISTS (SELECT 1 FROM preparation_task pt WHERE pt.order_item_id = oi.id)'
        + tail
    ), {"now": now, "is_base": True})
    return [f"backfilled up to {pending} preparation_task(s) from fired order items"]


def _backfill_locations(conn) -> list[str]:
    """Give tables that predate floors and zones a home.

    Zones used to be a free-text column. Every existing distinct value becomes
    a real Zone on a single default floor, keeping its name so no report
    changes meaning — "Patio" stays "Patio", it just now sits on the 1st floor.

    Guarded on tables that still have no zone_id, so this is a no-op once the
    backfill has run and safe if new floors were added afterwards.
    """
    orphans = conn.execute(text(
        "SELECT COUNT(*) FROM restaurant_table WHERE zone_id IS NULL"
    )).scalar_one()
    if not orphans:
        return []

    floor_id = conn.execute(
        text("SELECT id FROM floor WHERE name = :n"), {"n": DEFAULT_FLOOR}
    ).scalar_one_or_none()
    if floor_id is None:
        conn.execute(
            text("INSERT INTO floor (name, sort_order, is_active) "
                 "VALUES (:n, 0, 1)"),
            {"n": DEFAULT_FLOOR},
        )
        floor_id = conn.execute(
            text("SELECT id FROM floor WHERE name = :n"), {"n": DEFAULT_FLOOR}
        ).scalar_one()

    names = [r[0] for r in conn.execute(text(
        "SELECT DISTINCT COALESCE(NULLIF(zone, ''), 'Main') FROM restaurant_table "
        "WHERE zone_id IS NULL ORDER BY 1"
    ))]
    for i, name in enumerate(names):
        exists = conn.execute(
            text("SELECT id FROM zone WHERE floor_id = :f AND name = :n"),
            {"f": floor_id, "n": name},
        ).scalar_one_or_none()
        if exists is None:
            conn.execute(
                text("INSERT INTO zone (floor_id, name, sort_order, is_active) "
                     "VALUES (:f, :n, :s, 1)"),
                {"f": floor_id, "n": name, "s": i},
            )

    conn.execute(text(
        "UPDATE restaurant_table SET zone_id = ("
        "  SELECT z.id FROM zone z WHERE z.floor_id = :f"
        "    AND z.name = COALESCE(NULLIF(restaurant_table.zone, ''), 'Main')"
        ") WHERE zone_id IS NULL"
    ), {"f": floor_id})

    return [f"backfilled {orphans} tables onto '{DEFAULT_FLOOR}' ({len(names)} zones)"]


def _round_half_up(value: float) -> int:
    """Deterministic round-half-up for a non-negative value.

    Deliberately not Python's built-in round() (round-half-to-even) and never
    a SQL-side ROUND() on either dialect (SQLite and PostgreSQL round
    differently in edge cases, and neither is guaranteed to match Python) —
    the arithmetic happens exactly once, here, and the already-rounded
    integer is the only thing either dialect ever sees. See
    docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md §4.3.
    """
    return math.floor(value + 0.5)


def _backfill_table_map_positions(conn) -> list[str]:
    """Floor Plan Builder — give existing tables a starting position on the
    free (per-mille, 0-1000) map, deterministically derived from their
    current grid cell. Additive and one-directional: `pos_x`/`pos_y` (the
    grid columns) are only ever read here, never written or reinterpreted —
    docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md §3.

    ELIGIBLE(table, zone) := table.is_active AND table.zone_id = zone.id —
    an INNER JOIN, not a separately-worded `zone_id IS NOT NULL` clause (the
    join structurally excludes a zone-less table; there is nothing for it to
    equal). A soft-retired or zone-less table is never backfilled and keeps
    both columns NULL indefinitely — deliberate, not a gap (§4.4): neither
    ever renders on a floor plan. This single relation is used, unchanged,
    everywhere below — the eligible-row selection, the per-floor
    GRID_ROWS_SEEN, and the in-transaction post-condition all query it the
    same way, so the algorithm and its own correctness check cannot disagree
    on which rows they mean (§4.2).

    Step 0 — integrity check: an *orphaned* reference (an active table whose
    `zone_id` matches no existing `Zone` row) is a data-integrity fault, not
    a row the join gets to quietly exclude without comment. Raises, naming
    the table id(s), before anything else runs. Expected to find nothing on
    this codebase's own write paths (`app/database.py` enables `PRAGMA
    foreign_keys=ON` for SQLite, and `zone_id` is a real FK on both
    dialects; `Zone` rows are never hard-deleted) — it exists for a database
    this migration cannot assume was only ever touched through those paths.

    Step 1-2: for each eligible row still missing a map position, compute
    one from its grid cell — `pos_x`/`pos_y` defensively clamped to the
    grid's actual bounds, per-mille position computed and rounded once in
    Python (`_round_half_up`), then clamped again to [0, 1000] — and write
    it. Every eligible-and-not-yet-placed row is processed; none are
    silently skipped.

    Step 3-4: in this SAME transaction, immediately after step 2, re-check
    (over the identical `ELIGIBLE` relation) that every eligible row now has
    both coordinates, in range. Raise if not — do not log "SKIPPED" and
    continue. Fail-closed, not best-effort, unlike this module's other
    backfills: nothing downstream needs this one to succeed except the Floor
    Plan Builder itself, so there is no broader system to protect by
    swallowing a coding defect here (§4.2, Correction 3).

    Idempotent: the only rows ever touched are eligible-and-NULL; once none
    are, every later run is a no-op, and a table that becomes newly eligible
    afterward (a zone-less table assigned a zone; a reactivated table) is
    picked up on ITS OWN later run, without redoing an already-placed row
    (§4.5).
    """
    # A legitimately absent table is a safe no-op — same guard convention as
    # _backfill_locations/_backfill_prep_tasks. In the real app, create_all()
    # always creates restaurant_table/zone before migrate.run() ever executes
    # (module docstring), so this is normally trivially true; it matters for
    # a narrower harness (e.g. tests/test_pg_migration.py's payment-only
    # schema) that legitimately never creates these tables at all — found by
    # running that suite's regression against this backfill.
    if not _table_exists(conn, "restaurant_table") or not _table_exists(conn, "zone"):
        return []

    orphans = [
        r[0] for r in conn.execute(text(
            "SELECT restaurant_table.id FROM restaurant_table "
            "LEFT JOIN zone ON restaurant_table.zone_id = zone.id "
            "WHERE restaurant_table.is_active "
            "AND restaurant_table.zone_id IS NOT NULL "
            "AND zone.id IS NULL"
        )).fetchall()
    ]
    if orphans:
        raise RuntimeError(
            f"Floor Plan Builder backfill: table id(s) {orphans} are active "
            "with a zone_id that matches no existing Zone row (orphaned "
            "reference). Not backfilled, not silently excluded — this is a "
            "data-integrity fault outside this migration's own write paths. "
            "Resolve it, then restart. Startup halted."
        )

    eligible = conn.execute(text(
        "SELECT restaurant_table.id, restaurant_table.pos_x, restaurant_table.pos_y, "
        "zone.floor_id, restaurant_table.map_x_per_mille, restaurant_table.map_y_per_mille "
        "FROM restaurant_table JOIN zone ON restaurant_table.zone_id = zone.id "
        "WHERE restaurant_table.is_active"
    )).fetchall()
    if not eligible:
        return []

    pending = [row for row in eligible if row[4] is None or row[5] is None]

    # GRID_ROWS_SEEN per floor (§4.3): max(1, MAX(clamped pos_y) + 1) over the
    # WHOLE eligible set on that floor — not just the rows being backfilled
    # this run — computed once, before the per-table loop. Clamping pos_y to
    # 0 first means "+ 1" is always >= 1, so no separate max(1, ...) pass is
    # needed on top.
    rows_seen: dict[int, int] = {}
    for _id, _pos_x, pos_y, floor_id, _mx, _my in eligible:
        candidate = max(pos_y, 0) + 1
        if candidate > rows_seen.get(floor_id, 0):
            rows_seen[floor_id] = candidate

    for table_id, pos_x, pos_y, floor_id, _mx, _my in pending:
        x = min(max(pos_x, 0), GRID_COLS - 1)
        y = max(pos_y, 0)
        grid_rows = rows_seen[floor_id]
        map_x = max(0, min(1000, _round_half_up((x + 0.5) / GRID_COLS * 1000)))
        map_y = max(0, min(1000, _round_half_up((y + 0.5) / grid_rows * 1000)))
        conn.execute(
            text(
                "UPDATE restaurant_table SET map_x_per_mille = :x, "
                "map_y_per_mille = :y WHERE id = :id"
            ),
            {"x": map_x, "y": map_y, "id": table_id},
        )

    # Step 3/4 runs unconditionally whenever there is an eligible row, even if
    # `pending` was empty — the guarantee is "every eligible row is valid
    # right now", not just "whatever this run touched is valid". A row that
    # already carried a corrupted, non-NULL-but-out-of-range value (a bug
    # elsewhere, or a previous run's defect) must still be caught here, not
    # skipped because this run had nothing new to write.
    violations = [
        r[0] for r in conn.execute(text(
            "SELECT restaurant_table.id FROM restaurant_table "
            "JOIN zone ON restaurant_table.zone_id = zone.id "
            "WHERE restaurant_table.is_active "
            "AND (restaurant_table.map_x_per_mille IS NULL "
            "OR restaurant_table.map_y_per_mille IS NULL "
            "OR restaurant_table.map_x_per_mille NOT BETWEEN 0 AND 1000 "
            "OR restaurant_table.map_y_per_mille NOT BETWEEN 0 AND 1000)"
        )).fetchall()
    ]
    if violations:
        raise RuntimeError(
            f"Floor Plan Builder backfill: eligible table id(s) {violations} "
            "still missing a valid map position after the backfill ran — "
            "fail-closed, nothing left partially done. Startup halted."
        )
    return [f"backfilled map position for {len(pending)} table(s)"] if pending else []


# --------------------------------------------------------------------------
# Floor Plan Builder — the one-off zone-overlap backfill. The actual
# placement geometry (place_new_zone_rect and its helpers) lives in
# app/services/zone_geometry.py — a neutral module with no dependency on
# this file, a router, an Engine, or a Session — imported from here AND
# from app/routers/admin.py's live create-zone route, so both call sites
# share exactly one algorithm rather than two copies that could drift.
# --------------------------------------------------------------------------


@dataclass
class ZoneOverlapResult:
    """Testable outcome of _backfill_zone_overlap.

    `moved`: one human-readable line per zone the backfill successfully
    repositioned. `unresolved`: one line per zone it deliberately left
    untouched because no distinct rectangle exists for it (see
    place_new_zone_rect's `None` contract) — a real, expected outcome, not
    an error. Both lists feed straight into run()/`_run_postgres()`'s own
    flat `applied` log (unresolved entries stay clearly labelled there),
    while a caller that wants to check "did anything get stuck" can read
    `.unresolved` directly instead of parsing log text.
    """
    moved: list[str]
    unresolved: list[str]


def _backfill_zone_overlap(conn) -> ZoneOverlapResult:
    """Floor Plan Builder — de-collide active zones on the same floor that
    share an EXACTLY identical rectangle (pos_x, pos_y, width, height all
    equal).

    Why this exists: every zone (old and new, before this slice's fix to
    `create_zones`) is created without an explicit position — `Zone`'s
    pos_x/pos_y/width/height default to a fixed (60, 60, 360, 300) — so any
    two zones neither has ever been dragged/resized/hand-edited land on the
    literal same rectangle. The admin Floor Plan Builder's drop hit-test
    (`document.elementsFromPoint`, web/templates/admin_tables.html) can then
    only ever resolve to ONE of them for any point inside that shared
    rectangle, making the other permanently unreachable by drag — confirmed
    against a real production report (a table's reassignment silently never
    reaching one of two coincident zones; see docs/03_CURRENT_WORK.md).

    No reliable marker distinguishes "still at the untouched default" from
    "a manager deliberately typed/dragged two zones onto the exact same
    spot": `Zone` carries no "ever moved" flag, and the Map box's numeric
    fields on `/admin/floors` (web/templates/admin_floors.html) let a
    manager reproduce the exact default by hand anyway. So this treats
    every EXACT match the same regardless of cause, and does nothing to
    zones that merely overlap without being byte-identical — overlap
    itself is allowed by design (docs/Evidence/Floor/
    FLOOR_PLAN_BUILDER_DESIGN.md, and the historical `/tables/layout`
    docstring this project inherited: "Overlap is allowed ... the manager
    arranges the room as it really is."). Only an EXACT
    (pos_x, pos_y, width, height) match is ever touched.

    Algorithm, per (floor_id, pos_x, pos_y, width, height) group of two or
    more ACTIVE zones sharing that exact rect:
      - the zone with the lowest id is the anchor and is never moved;
      - every other zone in the group is repositioned — position only,
        width/height are preserved exactly — via place_new_zone_rect(),
        against a `taken` set seeded with every active zone's rect
        currently on that floor (the whole floor, not just this group), so
        a moved zone can land on neither the anchor nor any other zone
        already there, and is added to `taken` immediately after, so two
        zones from the same group can't be placed on top of each other
        either. If `place_new_zone_rect` returns `None` (no distinct
        rectangle exists at that size — see the availability policy
        below), that one zone is left completely unchanged and reported
        in `.unresolved` instead of moved — never raised;
      - each moved zone's own active tables that already carry a real
        (non-NULL) map_x_per_mille/map_y_per_mille are translated by the
        exact same (new_x - old_x, new_y - old_y) delta, clamped to
        [0, 1000] — width/height are unchanged, so this is a pure
        translation, not a proportional rescale: the zone's shape did not
        change, only its position;
      - inactive tables are never read or written — retired tables' stale
        coordinates don't matter, the same convention as every other Floor
        Plan Builder backfill/route in this codebase
        (_backfill_table_map_positions above; _free_cells,
        move_zone_layout in app/routers/admin.py);
      - a table with NULL map_x_per_mille/map_y_per_mille is left NULL —
        there is nothing real to translate, and `_table_map_positions()`
        (app/routers/admin.py) computes a fresh ring-fallback position
        around the zone's rectangle at render time regardless, from
        whatever that rectangle currently is. A never-placed table
        therefore "follows" the zone's corrected rectangle for free, with
        no write needed here.

    Pure DML (UPDATE only) — no ALTER TABLE, unlike the ADDED_COLUMNS work
    elsewhere in this file. That matters: on this driver, an ALTER auto
    -commits immediately on SQLite even inside `engine.begin()` (see
    _backfill_table_map_positions's own docstring) — but plain DML does
    not have that quirk on either dialect. A raised exception anywhere in
    this function rolls back every UPDATE it already issued, completely
    and identically, on both SQLite and PostgreSQL. Called inside its own
    `engine.begin()` block on both call sites (run(), _run_postgres()).

    Idempotent: only groups still sharing an exact rect right now are
    touched. Once a group has been de-collided, its zones no longer match
    exactly (position differs, width/height do not), so a later run finds
    nothing left to do for it — and an unresolved zone (below) stays
    reported as unresolved on every later run too, until someone actually
    changes its size or its colliding neighbour's.

    **Availability policy — read this before changing the exception
    handling below.** `place_new_zone_rect` returns `None` when a zone's
    exact size leaves no rectangle distinct from what is already occupied
    on its floor — mathematically exhausted, not a bug (the canonical
    example: two zones sized 1000x1000 leave exactly one legal position,
    (0, 0), and both already sit there). Independent review of an earlier
    version of this function found that treating this the same as every
    other failure here — raise, halt startup — was itself the more severe
    defect: it let a data state that is merely inconvenient (one zone
    stays visually indistinguishable from another) escalate into the
    entire application refusing to boot, in any environment, including
    the very admin UI a manager would need to fix it. A zone this function
    cannot place is instead left completely unchanged, reported in
    `.unresolved` (surfaced in the boot log, and readable programmatically
    by degrees — no `RuntimeError` involved), and every other group keeps
    being processed normally. **Application availability takes priority
    over fully resolving an impossible geometry.** The fail-closed
    `RuntimeError` below is narrowed accordingly: it still fires — halting
    startup, as every other backfill in this file does on a genuine
    internal defect — only if a zone this function believed it moved (or
    never even considered) is found still coincident afterward; it never
    fires for a zone honestly reported as unresolved.

    **Legacy/corrupted-data defense.** `Zone.width`/`Zone.height` are
    declared `NOT NULL` by the normal ORM schema (`app/models/oltp.py`) —
    this function does not doubt that contract for data written through
    it. It guards against rows this codebase's own write paths could never
    produce: a database touched outside those paths (a hand-run migration,
    an external tool, a pre-schema-hardening artifact), where `width`/
    `height` could be `NULL`, non-numeric, `<= 0`, or `> 1000`. A whole
    GROUP's width/height (the group key requires every member to match
    exactly, so validity is a per-group property, never mixed within one
    group) is validated BEFORE any member is passed to
    `place_new_zone_rect` — never `int(None)`, never arithmetic against
    `None`, no `TypeError` reaching the caller. An invalid group is
    reported unresolved in full (every member, anchor included — there is
    no meaningful "anchor" to keep when the group's own size cannot be
    trusted) and left completely untouched: not resized, not repositioned,
    not defaulted to a guessed size. Every other, validly-sized group in
    the same run is still processed normally.
    """
    def _valid_dimension(v: object) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 1000

    if not _table_exists(conn, "zone"):
        return ZoneOverlapResult([], [])

    zones = conn.execute(text(
        "SELECT id, floor_id, pos_x, pos_y, width, height FROM zone "
        "WHERE is_active ORDER BY floor_id, pos_x, pos_y, width, height, id"
    )).fetchall()
    if not zones:
        return ZoneOverlapResult([], [])

    # x/y/w/h are `object`, not `int`, in this key type: a legacy/corrupted
    # row can carry None or another non-int value here (see the function's
    # own docstring, "Legacy/corrupted-data defense") — never assumed int
    # until _valid_dimension has confirmed it, below.
    by_group: dict[tuple[int, object, object, object, object], list[int]] = {}
    # Same caveat as by_group above: entries here may echo an invalid
    # legacy (x, y, w, h) verbatim (harmless bookkeeping — see the comment
    # at the point they're added, below) — never read back out as if
    # guaranteed valid.
    taken_by_floor: dict[int, set[tuple[object, object, object, object]]] = {}
    for zid, floor_id, x, y, w, h in zones:
        by_group.setdefault((floor_id, x, y, w, h), []).append(zid)
        # An invalid w/h is still recorded here for bookkeeping (it can
        # never accidentally match a real candidate — place_new_zone_rect
        # only ever returns valid ints — so it is inert, not a hazard),
        # but is never read back out and passed to place_new_zone_rect.
        taken_by_floor.setdefault(floor_id, set()).add((x, y, w, h))

    tables_exist = _table_exists(conn, "restaurant_table")
    moved: list[str] = []
    unresolved: list[str] = []
    unresolved_ids: set[int] = set()

    def _group_sort_key(item):
        # Deterministic processing order without ever comparing an invalid
        # (possibly None, possibly non-numeric) x/y/w/h against a valid
        # int one — `<` between None and int raises. floor_id is always a
        # real int (never legacy/corrupted in this scenario); every other
        # key component is compared as its str() instead.
        (floor_id, x, y, w, h), _ids = item
        return (floor_id, str(x), str(y), str(w), str(h))

    for (floor_id, x, y, w, h), ids in sorted(by_group.items(), key=_group_sort_key):
        if len(ids) < 2:
            continue
        if not (_valid_dimension(w) and _valid_dimension(h)):
            # Legacy/corrupted data (see the function's own docstring):
            # this group's shared width/height cannot be trusted enough to
            # even attempt de-collision. No credentials, no DSN — only
            # this project's own zone/floor ids and the raw stored values,
            # safe to log.
            for zid in sorted(ids):
                unresolved_ids.add(zid)
                unresolved.append(
                    f"UNRESOLVED: zone.{zid} on floor {floor_id} has an "
                    f"invalid stored size (width={w!r}, height={h!r} — "
                    "expected an integer in [1,1000]) and was left "
                    "completely unchanged; not resized or repositioned. "
                    "This indicates legacy/corrupted data outside this "
                    "zone's normal write path — correct the size directly "
                    "before this can be de-collided."
                )
            continue
        anchor_id, *movers = sorted(ids)
        taken = taken_by_floor[floor_id]
        for idx, zid in enumerate(movers):
            placed = place_new_zone_rect(taken, idx, w, h)
            if placed is None:
                # No credentials, no DSN, no row content beyond ids/sizes
                # that are already this project's own internal numbers —
                # safe to log as-is.
                unresolved_ids.add(zid)
                unresolved.append(
                    f"UNRESOLVED: zone.{zid} on floor {floor_id} has no "
                    f"rectangle distinct from its current {w}x{h} size "
                    f"available — still exactly coincident with zone."
                    f"{anchor_id} at ({x},{y}), left unchanged. Resolve by "
                    "resizing or repositioning one of them (the Map box on "
                    "/admin/floors, or dragging on /admin/tables)."
                )
                continue
            new_x, new_y, new_w, new_h = placed
            taken.add((new_x, new_y, new_w, new_h))
            conn.execute(
                text("UPDATE zone SET pos_x = :x, pos_y = :y WHERE id = :id"),
                {"x": new_x, "y": new_y, "id": zid},
            )
            moved.append(
                f"zone.{zid} de-collided from zone.{anchor_id} on floor "
                f"{floor_id}: ({x},{y}) -> ({new_x},{new_y})"
            )
            if tables_exist:
                dx, dy = new_x - x, new_y - y
                movable = conn.execute(text(
                    "SELECT id, map_x_per_mille, map_y_per_mille FROM restaurant_table "
                    "WHERE zone_id = :zid AND is_active "
                    "AND map_x_per_mille IS NOT NULL AND map_y_per_mille IS NOT NULL"
                ), {"zid": zid}).fetchall()
                for tid, mx, my in movable:
                    nmx, nmy = max(0, min(1000, mx + dx)), max(0, min(1000, my + dy))
                    conn.execute(
                        text("UPDATE restaurant_table SET map_x_per_mille = :x, "
                             "map_y_per_mille = :y WHERE id = :id"),
                        {"x": nmx, "y": nmy, "id": tid},
                    )

    # Narrowed fail-closed post-condition (see the availability policy
    # above): a remaining coincident group is fine — expected, even — if
    # every member beyond one (the anchor) was explicitly reported as
    # unresolved. It is only a genuine internal defect, and only then
    # still halts startup, if MORE than one member of a remaining group is
    # NOT accounted for in `unresolved_ids` — a zone this function should
    # have moved (or should have flagged) but silently didn't do either.
    remaining = conn.execute(text(
        "SELECT id, floor_id, pos_x, pos_y, width, height FROM zone WHERE is_active"
    )).fetchall()
    remaining_groups: dict[tuple[int, int, int, int, int], list[int]] = {}
    for zid, floor_id, x, y, w, h in remaining:
        remaining_groups.setdefault((floor_id, x, y, w, h), []).append(zid)
    unexplained = [
        ids for ids in remaining_groups.values()
        if len(ids) > 1 and sum(1 for zid in ids if zid not in unresolved_ids) > 1
    ]
    if unexplained:
        raise RuntimeError(
            f"Floor Plan Builder zone-overlap backfill: {len(unexplained)} "
            "floor/rectangle group(s) still have more than one active zone "
            "sharing an exact rectangle, beyond what this run reported as "
            "unresolved — that indicates a defect in the backfill itself "
            "(a zone it should have moved or flagged was silently left "
            "alone), not a data limitation. Fail-closed. Startup halted."
        )
    return ZoneOverlapResult(moved, unresolved)
