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

import re
from datetime import datetime

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
    create_all already produced everything."""
    applied: list[str] = []
    pg = engine.dialect.name != "sqlite"
    q = (lambda s: f'"{s}"') if pg else (lambda s: s)

    # 1. Canonical provider backfill (before the provider-scoped constraints).
    for table, old, new in PROVIDER_BACKFILL:
        with engine.begin() as conn:
            if not _column_exists(conn, table, "provider"):
                continue
            n = conn.execute(text(f"UPDATE {q(table)} SET provider=:new WHERE provider=:old"),
                             {"new": new, "old": old}).rowcount
            if n:
                applied.append(f"backfilled {table}.provider {old}->{new} ({n} rows)")

    # 2. Retire the old provider_refund_id (refunds now live on refund_attempt).
    #    Drop only when it holds no data; otherwise report and keep it (never lose
    #    financial evidence silently).
    with engine.begin() as conn:
        if _column_exists(conn, "payment_attempt", "provider_refund_id"):
            leftover = conn.execute(
                text("SELECT COUNT(*) FROM payment_attempt WHERE provider_refund_id IS NOT NULL")
            ).scalar_one()
            if leftover:
                applied.append(f"KEPT payment_attempt.provider_refund_id ({leftover} non-null "
                               "rows) — retire manually after moving them to refund_attempt")
            else:
                try:
                    conn.execute(text('ALTER TABLE payment_attempt DROP COLUMN '
                                      + ("IF EXISTS " if pg else "") + "provider_refund_id"))
                    applied.append("dropped retired payment_attempt.provider_refund_id")
                except Exception as exc:  # noqa: BLE001 — SQLite <3.35 can't drop
                    if strict and pg:
                        raise MigrationError(f"failed to drop provider_refund_id: {exc}") from exc
                    applied.append(f"SKIPPED drop provider_refund_id: {exc}")

    # 3. Provider-scoped uniqueness — dup-scan, fail closed, then create.
    for table, name, cols in UNIQUE_CONSTRAINTS:
        with engine.connect() as conn:
            if not all(_column_exists(conn, table, c) for c in cols):
                continue
            if _constraint_exists(conn, table, name):
                continue
            dupes = _duplicates(conn, table, cols)
        if dupes:
            msg = (f"cannot add {name}: {len(dupes)} duplicate group(s) in "
                   f"{table}({', '.join(cols)}) — e.g. {dupes[0]}. Resolve before upgrading.")
            if strict:
                raise MigrationError(msg)
            applied.append(f"BLOCKED {msg}")
            continue
        try:
            with engine.begin() as conn:
                if pg:
                    conn.execute(text(f'ALTER TABLE {q(table)} ADD CONSTRAINT {name} '
                                      f'UNIQUE ({", ".join(cols)})'))
                else:
                    conn.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS {name} '
                                      f'ON {table} ({", ".join(cols)})'))
            applied.append(f"added {name} on {table}({', '.join(cols)})")
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise MigrationError(f"failed to add {name}: {exc}") from exc
            applied.append(f"SKIPPED {name}: {exc}")

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
