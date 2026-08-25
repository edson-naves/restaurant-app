# Kitchen Stations — v9 (Stage B1: exact PostgreSQL index-key verification + scoped `_index_exists`)

**Version:** v9 · **Scope:** two small items on top of the accepted v9 PostgreSQL
exact-key work — (1) scope `_index_exists()` to the current schema/table on
Postgres; (2) regenerate the review diff so the (untracked) test file is included.
No redesign, **no B2**, no user-visible change; `fire_seq` stays 1. **Pair:** this
handoff + `KITCHEN_STATIONS_v9.diff` (clean, full cumulative).

## Files changed vs v8

- `app/migrate.py` — Postgres branch of `_prep_task_index_ok` verifies each index
  key individually (added `_pg_norm_key` + `_strip_wrapping_parens`); **and**
  `_index_exists` is now schema/table-scoped.
- `tests/test_prep_tasks.py` — `test_pg_index_key_normalization_strict` and
  `test_index_exists_is_table_scoped`.

---

## 1. SHOULD-FIX — `_index_exists()` is now schema/table-scoped (matches the invariant checker)

`_index_exists` previously searched **all** schemas
(`SELECT 1 FROM pg_indexes WHERE indexname=:n`), while `_prep_task_index_ok`
scopes to `current_schema()` + `preparation_task`. An index of the same name in
**another** schema would make `_index_exists` return True while
`_prep_task_index_ok` returns False, so `_ensure_prep_task_index` would read it as
a *same-name / wrong-definition* collision and **halt startup** even though the
current schema is fine.

Fix — `_index_exists(conn, name, table=None)`:

- **Postgres:** `WHERE schemaname = current_schema() AND indexname = :n` (and
  `AND tablename = :t` when a table is given) — the same scope the definition
  checker uses.
- **SQLite:** `sqlite_master … name = :n` (and `AND tbl_name = :t` when given);
  SQLite has no schemas, so table-scoping is the meaningful axis.

The one caller now passes the table:
`_index_exists(conn, "uq_prep_task_batch", "preparation_task")`. A same-name index
in another schema, or on another table, is no longer mistaken for this one.

*Test:* `test_index_exists_is_table_scoped` — the real index counts when scoped to
`preparation_task`; a probe index created on **another** table (`station`) is
found when scoped to `station` but **not** when scoped to `preparation_task`
("same name + WRONG table → must NOT count"). SQLite has no schemas, so this
exercises the table-scoped path; the Postgres query applies the identical
`schemaname = current_schema()` filter (cross-schema isolation is a Postgres-only
axis, validated by the query below rather than by the SQLite test).

---

## 2. AUDIT BLOCKER — the test file is now in the review diff

`tests/test_prep_tasks.py` is **untracked**, so a plain `git diff` (tracked
changes only) omitted it from the v9 artifact — an artifact-generation gap, not an
application-code problem. The diff is now generated after
`git add -N tests/test_prep_tasks.py tests/test_stations.py
web/templates/admin_stations.html web/templates/expo.html` (intent-to-add only —
**nothing is staged for commit** and no content is committed; `-N` merely makes
`git diff` render the new files). `KITCHEN_STATIONS_v9.diff` now contains the full
`tests/test_prep_tasks.py`, including both new tests.

---

## PostgreSQL exact-key verification (from v9, unchanged here)

The Postgres branch reconstructs each key individually and compares exactly:

1. Catalog gates on the `uq_prep_task_batch` / `preparation_task` /
   `current_schema()` row: `indisunique`, `indpred IS NULL`, `indnkeyatts = 3`
   **and** `indnatts = 3` (exactly three key columns, **no INCLUDE** columns).
2. `pg_get_indexdef(indexrelid, 1|2|3, true)` per key, each normalised by
   `_pg_norm_key` (lowercase; drop spaces; strip `::type` casts and quotes; strip
   parens wrapping the *whole* expression via `_strip_wrapping_parens`).
3. Requires key1 `order_item_id`, key2 `fire_seq`, key3 `coalesce(station_id,-1)`
   (accepting the `(-1)` rendering). `COALESCE(station_id,-1) + course`,
   `ABS(COALESCE(station_id,-1))`, `COALESCE(station_id,0)`,
   `COALESCE(station_id,-1,course)`, and `COALESCE(station_id,-1) + 0` all
   normalise to something else and are rejected.

---

## PostgreSQL test evidence — stated precisely (no overstatement)

**No live/integration PostgreSQL was executed for this version.** Dev is SQLite;
there is no Postgres database in this environment. What was actually run:

- **Unit-tested (executed, SQLite process):** the key-normalisation /
  discrimination logic the Postgres branch depends on —
  `test_pg_index_key_normalization_strict` feeds `_pg_norm_key` /
  `_strip_wrapping_parens` the exact strings `pg_get_indexdef(idx,n,true)` emits
  and asserts the accept/reject decision (accepts the four intended `-1`
  renderings; rejects arithmetic-wrap, wrapping-function, wrong-fallback,
  extra-fallback, `+ 0`). The `_index_exists` table-scoping is likewise executed
  on SQLite.
- **NOT executed here (expected validation query, for a Postgres integration
  environment):** the catalog **gating** (`indisunique`, `indpred IS NULL`,
  `indnkeyatts = 3`, `indnatts = 3`) and the live `pg_get_indexdef` key text. The
  query to run there is:

```sql
SELECT i.indisunique, (i.indpred IS NULL) AS not_partial,
       i.indnkeyatts, i.indnatts,
       pg_get_indexdef(i.indexrelid, 1, true) AS k1,
       pg_get_indexdef(i.indexrelid, 2, true) AS k2,
       pg_get_indexdef(i.indexrelid, 3, true) AS k3
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE c.relname = 'uq_prep_task_batch' AND t.relname = 'preparation_task'
  AND n.nspname = current_schema();
```

For the correct index this is expected to return
`t | t | 3 | 3 | order_item_id | fire_seq | COALESCE(station_id, '-1'::integer)`.
Do **not** read the SQLite tests as proof of the live Postgres catalog behaviour —
they validate the Python discrimination logic, not a running server.

### SQLite exact-key check (accepted v8, unchanged)

`PRAGMA index_list` (exists / unique / non-partial) + paren-aware
`_sqlite_index_keylist` equality to
`["order_item_id","fire_seq","coalesce(station_id,-1)"]`.

---

## Test results

`tests/test_prep_tasks.py` — **all pass (25 test functions)**, including the two
new ones: `test_index_exists_is_table_scoped` and
`test_pg_index_key_normalization_strict`.
Stage A / regression: `test_stations.py`, `test_floormap.py`,
`test_reservations_map.py` all **exit 0** (run with UTF-8 stdout — a bash cp1252
console can crash the harness's `print` of a "→" label, a console artifact, not a
test failure). `app.main` imports; dev DB migrates (`schema up to date`) and the
SQLite index verifies by definition.

## `git diff --check`

`KITCHEN_STATIONS_v9.diff` generated with **stdout → file, stderr discarded**:
grep for `warning:` in the file → **0**; **`git diff --check` → exit 0**; file
starts with a valid `diff --git` header and now includes
`tests/test_prep_tasks.py`. (Git prints pre-existing LF→CRLF advisory lines to
stderr for unrelated tracked files; those are not in the diff and are not check
failures.)

## Confirmation

No B2 and no user-visible behavior introduced. Snapshot-at-fire, legacy
`OrderItem.station_id`, the fire-batch identity, the unique
`(order_item_id, fire_seq, COALESCE(station_id,-1))` invariant, duplicate-data
halt (no delete/merge), conflict-safe backfill, backfill observability,
state-based `_fire_conflict_is_idempotent`, one-fire-per-line (`fire_seq = 1`),
`OrderItem.kitchen_status` compatibility, READY ≠ SERVED, serving, payment gate,
coursing, Unassigned, KDS/Expo/floor Stage A reads, modifier-routing UI, and the
`fired_items_without_tasks()` B2 readiness invariant are all unchanged. The v9
PostgreSQL exact-key verification is untouched by these two fixes.

Stopping after this for final B1 approval — **not** proceeding to B2.
