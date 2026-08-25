# Kitchen Stations — B2.2 PostgreSQL Concurrency Evidence

Real-PostgreSQL proof of the approved B2 v3 §4/§4.1 concurrency contract for sibling
`PreparationTask` READY: parent-Order `FOR UPDATE` serialization + post-lock
reselect/refresh + rollup, with no lost update. **Executed against a real PostgreSQL
server (not SQLite, not simulated). RESULT: PASS.** No B2.2 implementation code was
changed to produce this evidence.

> This file records the executed proof. It does **not** mark B2.2 approved — that is
> the independent reviewer's decision.

## 1. PostgreSQL environment
- Server: **PostgreSQL 16.15** (Debian 16.15-1.pgdg13+2), local, port 5432.
- Driver: `psycopg` 3.3.4 (already in the venv; no dependency added).
- DSN: `postgresql+psycopg://postgres:***@localhost:5432/restaurant_scratch`
  (local scratch DB; credentials redacted).
- Proof script: `tests/pg_b2_2_concurrency_proof.py` (standalone; fails closed on a
  non-PostgreSQL DSN; reproduces the `task_ready` route's lock+refresh+rollup sequence).

## 2. Exact command
```
PYTHONIOENCODING=utf-8 \
DATABASE_URL=postgresql+psycopg://postgres:***@localhost:5432/restaurant_scratch \
  .venv/Scripts/python.exe tests/pg_b2_2_concurrency_proof.py
```
(`PYTHONIOENCODING=utf-8` only so the Windows console can print a `⇒` in a log line;
it does not affect the proof. Exit code 0.)

## 3. Scenario executed
Seeded one on-board `OrderItem` (id=2) with two sibling `PreparationTask`s (id=3 Grill,
id=4 Fryer), both PREPARING. Two independent sessions/transactions:
1. **Session A** acquires `SELECT "order".id … FOR UPDATE` on the parent Order and
   **holds the lock 2.0s**.
2. **Session B** attempts the same `FOR UPDATE` on the same Order and **blocks**.
3. A transitions task A → READY, rolls up, recomputes, **commits** (releasing the lock).
4. B unblocks, does the **post-lock reselect/refresh**, reads task A's committed state,
   transitions task B → READY, rolls up, recomputes, commits.
Each step uses the app's real `rollup_item_kitchen_status` and `_recompute_kitchen`.

## 4. Actual lock-contention evidence
- **B wait duration:** `b_wait_seconds = 1.809 s` while A held the lock for 2.0 s
  (≈ the full hold) — B was genuinely serialized behind A, not merely sequential.
- **pg_stat_activity (mid-hold snapshot)** showed B blocked on a lock:
  ```
  wait_event_type = 'Lock'
  wait_event      = 'transactionid'
  query           = SELECT "order".id FROM "order" WHERE "order".id = $1::INTE…
  ```
  i.e. B's `FOR UPDATE` on the parent Order was waiting on A's transaction.

## 5. Session B observed task A READY after the lock
After B acquired the lock (post A commit), B's post-lock read reported
`b_observed_task_a = "ready"` — B saw A's committed transition before proceeding.

## 6. Final persisted state
```
task_a       = ready   ready_at = 2026-08-22 22:11:57.514475
task_b       = ready   ready_at = 2026-08-22 22:11:57.599632
order_item   = ready
order_status = ready   (NOT served)
```
Both tasks READY with valid distinct `ready_at`; the OrderItem rolled up to READY; the
Order is not SERVED. No lost update, no stale rollup, no contradictory state.

## 7. PASS / FAIL
**PASS** (script exit 0). All contract conditions met:
B waited on the Order lock (≥ 0.7×hold), B observed A's committed READY, both tasks
READY with `ready_at`, OrderItem READY, Order not SERVED.

## 8. Evidence limitations
- Single local single-node PostgreSQL 16.15; one execution.
- Contention is proven by (a) B's measured wait ≈ A's 2.0s hold and (b) the
  `pg_stat_activity` `wait_event_type='Lock'` snapshot on B's `FOR UPDATE`.
- The proof reproduces the `task_ready` route's exact lock→refresh→rollup→recompute
  sequence with a deterministic hold window (needed to force contention); it does not
  invoke the HTTP handler literally. On SQLite this guarantee is NOT demonstrable
  (`FOR UPDATE` is a no-op) — this is why a real PostgreSQL run was required.

## 9. Files changed
None for this evidence run. The proof script `tests/pg_b2_2_concurrency_proof.py` was
added in the prior slice; no B2.2 implementation code, dependency, or infrastructure
was changed here.

---
This closes the outstanding **PostgreSQL concurrency evidence** acceptance item for
B2.2. B2.2 remains **AWAITING INDEPENDENT AUDIT / final approval** — not self-approved.
No B3/B4 started.
