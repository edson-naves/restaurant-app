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
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            if not rows:
                continue                       # table not created yet; create_all owns it
            if any(r[1] == column for r in rows):
                continue                       # already migrated
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            applied.append(f"{table}.{column}")

        applied.extend(_backfill_locations(conn))
    # SQLite does not enforce VARCHAR length, so no column ever needs widening
    # there — the model's new size applies to fresh databases via create_all.
    applied.extend(_migrate_payment_hardening(engine, strict))
    return applied


def _run_postgres(engine: Engine, strict: bool = False) -> list[str]:
    """Additive ADD COLUMN for an existing Postgres database (Render).

    Only columns that are genuinely missing are added, checked against
    information_schema. Every column already present (a fresh DB has them all
    from create_all) is skipped, so in practice this only ever adds the newest
    columns — all of which use cross-compatible DDL (INTEGER NOT NULL DEFAULT 0).
    Each ALTER runs in its own transaction and is guarded, so one failure can't
    abort the others or block startup.
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
                    text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS {column} {ddl}')
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
