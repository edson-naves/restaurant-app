# Kitchen Stations — v6 (Stage B1: three robustness fixes)

**Version:** v6 · **Scope:** the three targeted robustness fixes on the v5 B1
foundation. No redesign, **no B2**, no re-fire/remake behavior. `fire_seq` stays
effectively `1` (a line fires once in B1). KDS/Expo/floor reads, payment gate,
READY ≠ SERVED, serving, coursing, snapshot-at-fire, Unassigned, modifier-routing
UI, the legacy `OrderItem.station_id`, and the B2 readiness invariant are all
unchanged. **Pair:** this handoff + `KITCHEN_STATIONS_v6.diff` (full cumulative).

## Files changed vs v5

- `app/migrate.py` — unique-index creation is now a **required, verified
  invariant** (pre-existing-duplicate detection + halt; post-create verification).
- `app/routers/sales.py` — the fire `IntegrityError` handler now **verifies DB
  state** before treating a conflict as idempotent, else re-raises.
- `tests/test_prep_tasks.py` — new invariant + discrimination tests; the backfill
  concurrency test renamed/documented accurately.

## 1. Unique-index creation is a required invariant (not silently ignored)

`_ensure_prep_task_index` no longer does `try: create(); except: pass`. It now:

1. skips only if the table doesn't exist yet (create_all owns it);
2. **detects pre-existing duplicate identities** —
   `GROUP BY order_item_id, fire_seq, COALESCE(station_id,-1) HAVING COUNT(*)>1`;
3. if any exist, **raises a clear `RuntimeError` and halts** — it does **not**
   delete or merge them, so the app can never run believing it is protected when
   it is not;
4. creates the index (`CREATE UNIQUE INDEX IF NOT EXISTS`);
5. **verifies the index exists** afterwards (dialect-aware) and raises if missing.

It runs in its **own transaction**, after the additive column work has committed,
in both the SQLite and Postgres migration paths (so a halt doesn't roll back the
column adds). The backfill remains best-effort (a missing legacy task is caught by
the B2 readiness invariant — not a silent unprotected-duplicate risk).

*Tests:* `test_index_invariant_halts_on_duplicates` (seed duplicates with the
index dropped → invariant raises, duplicates reported not deleted) and
`test_index_invariant_present_normal` (clean case → present & verified).

## 2. `IntegrityError` on fire is verified, not blanket-swallowed

Both fire routes still catch `IntegrityError` around commit, but now:

1. roll back;
2. reload the affected lines in clean session state;
3. treat it as an **idempotent no-op only if** `_fire_conflict_is_idempotent`
   confirms the state a competing fire would have produced — every fired line is
   **no longer PENDING and already has ≥1 PreparationTask**;
4. otherwise **re-raise** — an unrelated integrity failure (bad FK, other
   constraint, bug) is surfaced, never hidden as a successful fire.

The decision is **state-based**, not parsed from the error string.

*Tests:* `test_fire_conflict_discrimination` (pending/no-task → surface;
non-pending+tasks → idempotent) and `test_unrelated_integrity_error_surfaced`
(an injected unrelated `IntegrityError` propagates; no partial tasks remain).
`test_concurrent_fire_creates_one_batch` still proves the expected-conflict path
via a real overlapping-session race on the index.

## 3. Backfill concurrency test is honestly scoped

`test_concurrent_backfill_no_duplicate` → renamed
**`test_backfill_competing_sessions_no_duplicate`**, with a docstring stating it
proves **competing-session + repeated-execution** safety (SQLite serializes
writers, so it is not a truly simultaneous overlap). It explicitly documents that
the **durable guarantee is the unique index + conflict-safe insert**
(`INSERT OR IGNORE` / `ON CONFLICT DO NOTHING`), proven by
`test_db_rejects_duplicate_within_batch` and the real overlap in
`test_concurrent_fire_creates_one_batch`; a true simultaneous-overlap backfill
belongs in a Postgres integration environment.

## Test results

`tests/test_prep_tasks.py` — **all pass** (19 cases). Regression:
`test_stations.py`, `test_floormap.py`, `test_reservations_map.py` all green;
`app.main` imports; dev DB migrated and **`uq_prep_task_batch` is present** (7225
backfilled tasks intact, no duplication). e2e is a live-server harness (not run).

## Confirmation

No B2 and no user-visible behavior introduced. Snapshot-at-fire, the fire-batch
identity concept, the PreparationTask model direction, the legacy
`OrderItem.station_id`, and the B2 readiness invariant are unchanged. This patch
only makes B1 safe under retries, concurrency, and a pre-existing-duplicate DB.

Stopping after these fixes for re-audit — **not** proceeding to B2.
