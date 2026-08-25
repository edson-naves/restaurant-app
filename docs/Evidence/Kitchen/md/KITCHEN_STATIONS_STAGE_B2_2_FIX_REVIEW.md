# Kitchen Stations — B2.2 Fix Review (audit findings 1 & 2)

Scope: the two implementation findings from the B2.2 independent audit
(verdict FIX REQUIRED). Covers **only** these fixes — not the full B2.2 package. No
code was changed to produce this file. No B3/B4, no redesign. PostgreSQL was not
addressed by simulation and remains a separate acceptance requirement.

Review against `KITCHEN_STATIONS_STAGE_B2_v3_DESIGN.md` §5.1 (finding 1) and §5.3
(finding 2).

## Files changed by these fixes

| File | Change |
|---|---|
| `app/routers/sales.py` | Phase 1 of the bridge validates every task-backed target before any mutation |
| `web/templates/kitchen.html` | operator-facing coarse "un-ready (all stations)" control |
| `tests/test_kds_task_ready.py` | +1 test (finding 1) |
| `tests/test_kds_task_reads.py` | +1 test (finding 2) |

Diff note: no baseline commit exists (whole `feat/floor-map` tree uncommitted), so the
hunks below are the exact edits (old→new); grep-verify in the live file.

---

## 1. Finding 1 — Phase 1 validates every task-backed target (§5.1)

Before, Phase 1 only rejected PENDING; a READY/PREPARING request against a task-backed
item with **no active task** (e.g. all tasks SERVED) silently no-opped. Now Phase 1
rejects it, preserving all-or-nothing.

`app/routers/sales.py`, `kitchen_status`:

```diff
-    # PHASE 1 — validate every target BEFORE any mutation (no partial commit).
-    for item in targets:
-        if item.kitchen_status == KitchenStatus.SERVED:
-            continue                        # delivered; a course-level action skips it
-        if item.tasks and status == KitchenStatus.PENDING:
-            # Un-firing tasked work needs the deferred re-fire model — reject the
-            # WHOLE request so nothing is partially mutated.
-            db.rollback()
-            raise HTTPException(409, "A fired kitchen item cannot be set back to pending.")
+    # PHASE 1 — validate EVERY task-backed target against the requested transition
+    # BEFORE any mutation, so an invalid target rejects the whole request with no
+    # partial commit (design v3 §5.1).
+    for item in targets:
+        if item.kitchen_status == KitchenStatus.SERVED:
+            continue                        # delivered; a course-level action skips it
+        if not item.tasks:
+            continue                        # legacy no-task item — Stage A direct write
+        if status == KitchenStatus.PENDING:
+            # Un-firing tasked work needs the deferred re-fire model.
+            db.rollback()
+            raise HTTPException(409, "A fired kitchen item cannot be set back to pending.")
+        # READY / PREPARING act on the item's ACTIVE (non-served) tasks. With none
+        # available (e.g. every task already SERVED) there is no valid task
+        # transition, so reject rather than silently no-op.
+        if not any(t.kitchen_status != PreparationTaskStatus.SERVED for t in item.tasks):
+            db.rollback()
+            raise HTTPException(409, "This kitchen item has no active tasks to update.")
```

Phase 2 is unchanged (only ever reached when every target validated). Legacy no-task
items still take the Stage A direct write.

### Test (finding 1) — `tests/test_kds_task_ready.py`

```python
def test_bridge_ready_rejected_when_no_active_task():
    # Design v3 §5.1: a task-backed target with no active task (all SERVED) has no
    # valid READY transition — the bridge must reject the WHOLE request, no mutation.
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True); db.add(g); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id])
    for t in _tasks(db, oi.id):
        t.kitchen_status = PreparationTaskStatus.SERVED
    legacy = OrderItem(order_id=o.id, menu_item_id=mi.id, quantity=1,
                       unit_price_cents=mi.price_cents, kitchen_status=KitchenStatus.PREPARING, course=2)
    db.add(legacy); db.commit()
    c = _client(db, owner)
    r = _bridge(c, o.id, "ready")                     # whole order
    check(r.status_code == 409, "READY rejected when a task-backed target has no active task")
    check(_tasks(db, oi.id)[0].kitchen_status == "served", "the served task is untouched")
    check(db.get(OrderItem, legacy.id).kitchen_status == KitchenStatus.PREPARING,
          "the legacy sibling is NOT changed (all-or-nothing)")
    app.dependency_overrides.clear(); db.close()
```

---

## 2. Finding 2 — operator-facing coarse un-ready control (§5.3)

Adds a task-mode control that resets readiness at **all** stations of an item, via the
existing legacy bridge (POST `status=preparing`) — no new infrastructure. Shown once
the item has a ready task.

`web/templates/kitchen.html` (inside the task-mode `k-tasks` block):

```diff
               {% endfor %}
             </div>
+            {% if can('kitchen.update') and i.display_tasks|selectattr('kitchen_status','equalto','ready')|list %}
+            {# Kitchen Stations B2.2 (design v3 §5.3) — coarse "un-ready" via the legacy
+               bridge (POST status=preparing). DESTRUCTIVE: resets readiness at EVERY
+               station of this item. The label + title + confirm make that explicit. #}
+            <form method="post" action="/kitchen/{{ t.order.id }}/status" class="k-unready"
+                  onsubmit="return confirm('Un-ready {{ i.menu_item.name }}? This clears READY at ALL of its stations.');">
+              <input type="hidden" name="status" value="preparing">
+              <input type="hidden" name="item_id" value="{{ i.id }}">
+              <button type="submit" class="btn sm" title="Resets readiness at ALL stations of this item (destructive)">↩ Un-ready (all stations)</button>
+            </form>
+            {% endif %}
             {% endif %}
           </div>
```

The button label ("Un-ready (all stations)"), the title ("Resets readiness at ALL
stations of this item (destructive)") and the `confirm()` dialog all state that it
clears readiness for every station. The backend path is the §5.3 destructive coarse
override already implemented and tested in the bridge.

### Test (finding 2) — `tests/test_kds_task_reads.py`

```python
def test_task_mode_coarse_unready_control_present():
    # Design v3 §5.3: an operator-facing coarse control that resets readiness at ALL
    # stations of the item, via the existing bridge (POST status=preparing).
    db = _session(); owner = _owner(db)
    g = Station(name="Grill", type="production", is_active=True)
    f = Station(name="Fryer", type="production", is_active=True)
    db.add_all([g, f]); db.commit()
    ch, mi = _base(db, item_station=g.id)
    o, oi = _fired_item(db, ch, mi, [g.id, f.id])
    db.refresh(oi, ["tasks"]); oi.tasks[0].kitchen_status = PreparationTaskStatus.READY; db.commit()
    _activate(db)
    c = _client(db, owner)
    html = _get(c).text
    check('name="status" value="preparing"' in html and f'name="item_id" value="{oi.id}"' in html,
          "task mode shows a coarse un-ready control posting PREPARING for the item")
    check("all stations" in html.lower(),
          "the un-ready control clearly says it resets readiness for ALL stations")
    _reset_guard(); app.dependency_overrides.clear(); db.close()
```

---

## Test command + results

Environment: Windows, `.venv` (CPython 3.14), SQLite, `PYTHONIOENCODING=utf-8`,
FastAPI `TestClient`.

```
$ .venv/Scripts/python.exe tests/test_kds_task_ready.py   → all KDS task-ready (B2.2) tests passed   (exit 0; 12 tests)
$ .venv/Scripts/python.exe tests/test_kds_task_reads.py   → all KDS task-read tests passed           (exit 0)
```
Regression (exit 0): `test_stations, test_prep_tasks, test_floormap, test_reservations_map,
test_templates, test_money, test_security, test_happyhour, test_happyhour_sales,
test_schedule, test_upsell, test_reconciliation`. `test_admin` → exit 1 (pre-existing,
unrelated). `py_compile app/routers/sales.py` OK.

## Confirmation & scope

- Finding 1: the bridge now **rejects a task-backed READY/PREPARING with no active
  task** (all-or-nothing preserved); legacy no-task items unchanged.
- Finding 2: the §5.3 coarse control is present and **clearly communicates it resets
  ALL stations**, reusing the existing bridge (no new infrastructure).
- No redesign of B2; no B3/B4; payment gate untouched; READY != SERVED preserved.

## Evidence gap (unchanged)

**PostgreSQL concurrency evidence remains a separate, still-open acceptance
requirement.** It was not addressed here and was not simulated; no SQLite-equals-
PostgreSQL claim is made.

B2.2 remains **AWAITING INDEPENDENT AUDIT — not approved.** No B3/B4 started.
