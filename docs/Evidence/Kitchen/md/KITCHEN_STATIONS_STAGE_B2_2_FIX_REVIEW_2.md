# Kitchen Stations — B2.2 Fix Review 2 (bridge READY task-state contract)

Scope: the single remaining B2.2 audit finding — the legacy kitchen-status bridge
Phase 1 must enforce the **same allowed task-state contract as the per-task READY
route** (PREPARING allowed, READY allowed/idempotent, any other state rejected
all-or-nothing before mutation). Covers only this fix. No code was changed to produce
this file. No redesign, no B3/B4. PostgreSQL was not addressed by simulation and
remains a separate acceptance requirement.

Review against `KITCHEN_STATIONS_STAGE_B2_v3_DESIGN.md` §5.1 and §4 (transition
contract).

## The problem

Phase 1 only checked that at least one task was non-SERVED. So a non-served task in an
**unsupported** state (e.g. an unexpected `PENDING`) passed validation, and Phase 2's
READY loop (which only moves `PREPARING → READY`) then **silently ignored** it — a
contract mismatch with the per-task route, which rejects any state other than
PREPARING/READY.

## Files changed

| File | Change |
|---|---|
| `app/routers/sales.py` | Phase 1 rejects a task-backed READY/PREPARING target unless every non-served task is in `TASK_ACTIVE` (PREPARING/READY) |
| `tests/test_kds_task_ready.py` | +1 focused regression test |

## The fix (`app/routers/sales.py`, `kitchen_status` Phase 1) — old → new

```diff
-        # READY / PREPARING act on the item's ACTIVE (non-served) tasks. With none
-        # available (e.g. every task already SERVED) there is no valid task
-        # transition, so reject rather than silently no-op.
-        if not any(t.kitchen_status != PreparationTaskStatus.SERVED for t in item.tasks):
-            db.rollback()
-            raise HTTPException(409, "This kitchen item has no active tasks to update.")
+        # READY / PREPARING act on the item's ACTIVE (non-served) tasks, which must
+        # EACH be in a state the per-task READY route supports (PREPARING or READY).
+        # No active task (e.g. all SERVED), or any non-served task in an unsupported
+        # state (e.g. an unexpected PENDING), means there is no valid transition —
+        # reject the whole request rather than silently ignoring it in Phase 2.
+        non_served = [t for t in item.tasks if t.kitchen_status != PreparationTaskStatus.SERVED]
+        if not non_served or any(t.kitchen_status not in TASK_ACTIVE for t in non_served):
+            db.rollback()
+            raise HTTPException(409, "This kitchen item has no valid task transition.")
```

`TASK_ACTIVE = (PreparationTaskStatus.PREPARING, PreparationTaskStatus.READY)` — the
same supported set the per-task READY route enforces. The check runs entirely in
Phase 1 (validation), so any invalid target rejects the whole request with `db.rollback()`
before Phase 2 mutates anything (all-or-nothing preserved). Legacy no-task items are
unaffected (they `continue` above and keep the Stage A direct write).

## Focused test (`tests/test_kds_task_ready.py`)

```python
def test_bridge_ready_rejected_on_unsupported_task_state():
    # Phase 1 must enforce the per-task contract: a non-served task in an unsupported
    # state (here PENDING) makes the whole READY request invalid — reject all-or-
    # nothing, never silently ignore it in Phase 2. The task CHECK constraint forbids
    # 'pending', so we inject it with PRAGMA ignore_check_constraints to simulate an
    # unexpected/corrupt row.
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True)
    f = Station(name="Fryer", type="production", is_active=True)
    db.add_all([g, f]); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id, f.id])
    ts = _tasks(db, oi.id)
    db.execute(text("PRAGMA ignore_check_constraints=ON"))
    db.execute(text("UPDATE preparation_task SET kitchen_status='pending' WHERE id=:i"),
               {"i": ts[1].id})
    db.commit()
    db.execute(text("PRAGMA ignore_check_constraints=OFF"))
    db.expire_all()
    legacy = OrderItem(order_id=o.id, menu_item_id=mi.id, quantity=1,
                       unit_price_cents=mi.price_cents, kitchen_status=KitchenStatus.PREPARING, course=2)
    db.add(legacy); db.commit()
    c = _client(db, owner)
    r = _bridge(c, o.id, "ready")                     # whole order
    check(r.status_code == 409,
          "READY rejected when a task-backed target has an unsupported task state")
    fresh = {t.station_id: t for t in _tasks(db, oi.id)}
    check(fresh[g.id].kitchen_status == "preparing" and fresh[f.id].kitchen_status == "pending",
          "no mutation on rejection — both tasks untouched")
    check(db.get(OrderItem, legacy.id).kitchen_status == KitchenStatus.PREPARING,
          "the legacy sibling is NOT changed (all-or-nothing)")
    app.dependency_overrides.clear(); db.close()
```

Note: the `preparation_task` CHECK constraint (`kitchen_status IN
('preparing','ready','served')`) makes a `PENDING` task impossible via normal writes,
so the test injects it with `PRAGMA ignore_check_constraints` to simulate an
unexpected/corrupt row — exactly the defense-in-depth the finding targets.

## Test command + result

Environment: Windows, `.venv` (CPython 3.14), SQLite, `PYTHONIOENCODING=utf-8`,
FastAPI `TestClient`.

```
$ .venv/Scripts/python.exe tests/test_kds_task_ready.py   → all KDS task-ready (B2.2) tests passed   (exit 0; 13 tests)
```
Regression (exit 0): `test_kds_task_reads`, `test_stations`, `test_prep_tasks`.
`py_compile app/routers/sales.py` OK.

## Confirmation & scope

- The bridge Phase 1 now enforces the per-task task-state contract: only
  PREPARING/READY active tasks are valid; anything else rejects the **whole** request
  before any mutation (all-or-nothing). Phase 2 can no longer silently ignore an
  unsupported task state.
- No redesign of B2; no B3/B4; payment gate untouched; READY != SERVED preserved.

## Evidence gap (unchanged)

**PostgreSQL concurrency evidence remains a separate, still-open acceptance
requirement.** Not addressed here and not simulated; no SQLite-equals-PostgreSQL claim.

B2.2 remains **AWAITING INDEPENDENT AUDIT — not approved.** No B3/B4 started.
