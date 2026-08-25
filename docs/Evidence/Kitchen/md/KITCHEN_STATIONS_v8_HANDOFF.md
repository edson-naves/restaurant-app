# Kitchen Stations — v8 (Stage B1: exact index-definition verification)

**Version:** v8 · **Scope:** the single v7 blocker — `_prep_task_index_ok()` now
verifies the **exact key structure** of the fire-batch unique index, not
substring presence. Nothing else from v7 is reopened. No redesign, **no B2**, no
user-visible change; `fire_seq` stays 1 (one fire per line in B1). **Pair:** this
handoff + `KITCHEN_STATIONS_v8.diff` (clean, full cumulative).

## Files changed vs v7

- `app/migrate.py` — `_prep_task_index_ok` rewritten to check the actual indexed
  key structure; added `_sqlite_index_keylist` (paren-aware) and `import re`.
- `tests/test_prep_tasks.py` — new strict-definition regression test.

## The fix — verify the exact fire-batch key, not substrings

`_prep_task_index_ok` must prove the index enforces exactly
`UNIQUE(order_item_id, fire_seq, COALESCE(station_id, -1))` — the final DB defense
against duplicate kitchen work. v7 checked substrings, which would wrongly accept
an index with an **extra key column** or a **partial** predicate. v8 verifies the
structure:

**SQLite**
- `PRAGMA index_list('preparation_task')` → the index exists, is **UNIQUE**, and is
  **not partial**.
- `sqlite_master.sql` → rejected if it carries a `WHERE` (partial) clause; the key
  list is split **paren-aware** (`_sqlite_index_keylist`, so `COALESCE(station_id,
  -1)`'s inner comma is not a separator) and must equal **exactly**
  `["order_item_id", "fire_seq", "coalesce(station_id,-1)"]` — same count, same
  order, same expression. An extra 4th key, a wrong `COALESCE(station_id, 0)`, or
  different columns all fail.

**Postgres**
- Catalog-based (`pg_index` + `pg_class` + `pg_namespace` + `pg_get_indexdef`),
  scoped to `current_schema()` and `tablename='preparation_task'`: **unique**
  (`indisunique`), **non-partial** (`indpred IS NULL`), and **exactly 3** key
  attributes (`array_length(indkey,1)=3` → rejects an extra key column). The
  definition must be ordered `(order_item_id, fire_seq, COALESCE(station_id, …))`
  with the second COALESCE arg the `-1` literal (any of its renderings), rejecting
  `COALESCE(station_id, 0)`.

`_ensure_prep_task_index` is unchanged in shape and still: halts on pre-existing
duplicates (no delete/merge); halts on a **same-name but wrong** index (now
including extra-column / partial / wrong-expression, not just non-unique) without
dropping it; creates the index; then **verifies by definition** and halts if not
correct.

## Tests (all pass)

New `test_index_definition_strict_rejects_bad_shapes` proves `_prep_task_index_ok`
returns False AND `_ensure_prep_task_index` **halts** (without dropping) for:
- **extra key column** — `(order_item_id, fire_seq, COALESCE(station_id,-1), course)`;
- **partial index** — `… WHERE station_id IS NOT NULL` (would drop Unassigned protection);
- **wrong COALESCE** — `COALESCE(station_id, 0)`.
Plus (kept): correct index accepted (`test_index_invariant_present_normal`,
now also asserting by-definition ok), same-name **non-unique** rejected
(`test_index_wrong_definition_halts`), and pre-existing duplicates halt.

## Accepted from v7 (unchanged)

Backfill observability (explicit `_table_exists` guard; unexpected errors
propagate and are logged as `SKIPPED prep-task backfill: <error>`, absent table =
safe no-op); duplicate detection before index creation (halt, no auto-delete/
merge); state-based `_fire_conflict_is_idempotent` (no error-string parsing);
clean diff generation.

## Test results

`tests/test_prep_tasks.py` — **all pass** (25 cases). Regression:
`test_stations.py`, `test_prep_tasks.py`, `test_floormap.py`,
`test_reservations_map.py` all **exit 0** (run with UTF-8 stdout; a bash cp1252
console can crash the harness's `print` of a "→" label — a console-encoding
artifact, not a test failure). `app.main` imports; dev DB migrates and the index
verifies **by definition**.

## `git diff --check`

`KITCHEN_STATIONS_v8.diff` generated with **stdout → file, stderr discarded**:
grep for `warning:`/CRLF text → **0**; `git diff --check` → **exit 0**; file starts
with a valid `diff --git` header.

## Confirmation

No B2 and no user-visible behavior introduced. Snapshot-at-fire, legacy
`OrderItem.station_id`, the fire-batch identity design, `OrderItem.kitchen_status`
compatibility, READY ≠ SERVED, serving, payment gate, coursing, Unassigned,
KDS/Expo/floor Stage A reads, modifier-routing UI, one-fire-per-line, and the B2
readiness invariant are all unchanged.

Stopping after this for final B1 re-audit — **not** proceeding to B2.
