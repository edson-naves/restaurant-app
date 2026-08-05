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
    ("payment", "voided_at", "DATETIME"),
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
)

DEFAULT_FLOOR = "1st floor"


def run(engine: Engine) -> list[str]:
    """Apply any missing columns, then backfill. Returns what changed.

    These additive migrations exist to evolve an *existing* database whose
    schema predates a column. SQLite (dev) is introspected with PRAGMA; Postgres
    (prod on Render) with information_schema. A *fresh* database gets the full
    current schema from Base.metadata.create_all(), so the checks below simply
    find every column present and do nothing — but an existing Postgres created
    before a column was added still needs it, which is what the Postgres branch
    handles (previously it was skipped, so new columns never reached prod).
    """
    if engine.dialect.name != "sqlite":
        return _run_postgres(engine)
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
    return applied


def _run_postgres(engine: Engine) -> list[str]:
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
        except Exception as exc:               # noqa: BLE001 — never block startup
            applied.append(f"SKIPPED {table}.{column}: {exc}")
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
