# Kitchen Stations — v7 (Stage B1: index-by-definition + backfill observability + clean diff)

**Version:** v7 · **Scope:** the two v6 implementation findings + the audit-artifact
(clean diff) fix. No redesign, **no B2**, no re-fire/remake; `fire_seq` stays 1
(one fire per line in B1). All B1 invariants unchanged: snapshot-at-fire, legacy
`OrderItem.station_id`, `OrderItem.kitchen_status` compatibility, READY ≠ SERVED,
serving, payment gate, coursing, Unassigned, KDS/Expo/floor Stage A reads,
modifier-routing UI, the B2 readiness invariant, and the `_fire_conflict_is_idempotent`
handling (accepted in v6, unchanged). **Pair:** this handoff +
`KITCHEN_STATIONS_v7.diff` (clean, full cumulative).

## Files changed vs v6

- `app/migrate.py` — index verified **by definition** (`_prep_task_index_ok`);
  same-name-wrong-definition halts; backfill no longer swallows arbitrary errors.
- `tests/test_prep_tasks.py` — new definition/backfill tests; a stale-connection
  test pattern fixed (it had relied on the old error-swallow).

## 1. Index verified by definition, not by name (BLOCKER 1)

`_ensure_prep_task_index` now verifies the **semantic definition**, via a new
`_prep_task_index_ok(conn)`:

- **SQLite** — `PRAGMA index_list('preparation_task')` confirms the index is
  **UNIQUE**; `sqlite_master.sql` (scoped to `tbl_name='preparation_task'`)
  confirms it covers `order_item_id`, `fire_seq`, and `COALESCE(station_id …)`.
- **Postgres** — `pg_indexes.indexdef`, scoped to `schemaname = current_schema()`
  and `tablename='preparation_task'`, confirms `UNIQUE INDEX` and the same three
  expressions.

Flow: detect pre-existing duplicates → halt; **if an index named
`uq_prep_task_batch` already exists but is NOT ok by definition → halt** (it is
not dropped automatically — `CREATE … IF NOT EXISTS` would otherwise silently
leave the wrong index and defeat the invariant); create the index; then verify
**by definition** (not just name) → halt if it isn't right. Duplicate detection
still changes no data.

*Tests:* `test_index_invariant_present_normal` (also asserts by-definition ok);
`test_index_wrong_definition_halts` (a same-name **non-unique** index → detected
as not-ok, invariant **halts**, wrong index **not** dropped);
`test_index_invariant_halts_on_duplicates` (unchanged).

## 2. Backfill no longer swallows arbitrary errors (SHOULD-FIX 1)

`_backfill_prep_tasks` replaced its blanket `try/except: return []` with an
explicit `_table_exists("preparation_task")` guard:

- **absent table → safe no-op (`[]`)**;
- **any other DB error → propagates** to the caller. The migration callers keep
  the backfill best-effort **but observable**: both the SQLite and Postgres paths
  wrap it and record `SKIPPED prep-task backfill: <actual error>` in the applied
  log (printed by `main.py`) — never a silent empty result. The unique-index
  invariant remains the only hard-halt step; the backfill never blocks startup.

*Tests:* `test_backfill_missing_table_noop` (absent table → `[]`);
`test_backfill_unexpected_error_propagates` (an injected DB error propagates,
not returned as `[]`).

## 3. Clean review diff (AUDIT BLOCKER)

The v6 diff had Git CRLF **stderr** warnings merged into the file (from `2>&1`).
`KITCHEN_STATIONS_v7.diff` is generated with **stdout → file, stderr discarded**;
verified to contain **no `warning:` / CRLF text** (grep count 0). **`git diff
--check` → exit 0** (no whitespace errors or conflict markers). No application
code was changed for the CRLF warnings — the repo has no line-ending defect; only
the artifact generation was fixed.

## Test results

`tests/test_prep_tasks.py` — **all pass** (24 cases). Regression:
`test_stations.py`, `test_floormap.py`, `test_reservations_map.py` all green (exit
0); `app.main` imports; dev DB migrated and `uq_prep_task_batch` verified **by
definition** (`_prep_task_index_ok` → True), 7225 backfilled tasks intact.
e2e is a live-server harness (not run here).

## Confirmation

No B2 and no user-visible behavior introduced. `_fire_conflict_is_idempotent`
(state-based, accepted in v6) is unchanged; no parsing of DB error strings. This
patch only strengthens the index invariant verification and backfill
observability, and delivers a clean review diff.

Stopping after this for final B1 re-audit — **not** proceeding to B2.
