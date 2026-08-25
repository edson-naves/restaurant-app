# Kitchen Stations — B2.1 Activation-Guard Fix Review

Scope: the single audit blocker — *manually setting `kitchen_b2_active=true` with
readiness==0 could activate TASK mode during B2.1*. This package covers **only** that
fix (not the full B2.1 package). No code was changed to produce this file. No B2.2.

## 1. Files changed by this fix

| File | Change |
|---|---|
| `app/routers/sales.py` | new code-level guard `B2_2_ACTIVE = False`; `task_kds_active` and `kitchen_display` gate on it |
| `app/main.py` | startup warning respects the same guard |
| `tests/test_kds_task_reads.py` | new `test_b2_1_cannot_activate_before_b2_2`; read tests flip the guard in-process via `_activate`/`_reset_guard` |

Root cause: activation depended only on the operational setting + readiness, both of
which are runtime/DB state. The fix adds a **code-level** gate that no operator, DB
edit, or UI can flip during B2.1.

## 2. Activation guard — old → new (`app/routers/sales.py`)

```diff
+# Code-level activation guard. Task-based KDS is only safe once the B2.2 slice
+# (per-task READY + legacy-write bridge + rollup) exists. While False, B2.1 can
+# NEVER operate the task board, even if `kitchen_b2_active=true` is set manually.
+# It is code, not a setting, so it cannot be flipped operationally. B2.2 flips it.
+B2_2_ACTIVE = False
+
+
 def task_kds_active(db: Session) -> bool:
-    """B2.1 activation gate (design v3 §2) — DORMANT by default. ..."""
+    """B2.1 activation gate — DORMANT and NOT activatable in B2.1. Requires
+    B2_2_ACTIVE (False throughout B2.1) AND the flag AND readiness==0. ..."""
+    if not B2_2_ACTIVE:
+        return False
     if not settings_svc.flag(db, "kitchen_b2_active"):
         return False
     return fired_items_without_tasks(db) == 0
```

And the runtime read path in `kitchen_display` (drives `task_mode` / `b2_blocked`):

```diff
-    b2_flag = settings_svc.flag(db, "kitchen_b2_active")
+    b2_flag = B2_2_ACTIVE and settings_svc.flag(db, "kitchen_b2_active")
     b2_missing = fired_items_without_tasks(db) if b2_flag else 0
     task_mode = b2_flag and b2_missing == 0
     b2_blocked = b2_flag and b2_missing > 0
```

With `B2_2_ACTIVE = False`, `b2_flag` is always False → `task_mode` and `b2_blocked`
are always False → the board is always the complete Stage A path in B2.1.

## 3. Startup-warning adjustment — old → new (`app/main.py`)

```diff
     if _settings_svc.flag(_db, "kitchen_b2_active"):
-        _missing = sales.fired_items_without_tasks(_db)
-        if _missing:
-            print(f"[kitchen-b2] activation BLOCKED: {_missing} fired item(s) without "
-                  "PreparationTasks — serving the full Stage A KDS", flush=True)
-        else:
-            print("[kitchen-b2] task-based KDS active (flag on, no missing tasks)", flush=True)
+        if not sales.B2_2_ACTIVE:
+            # B2.1: the code-level guard keeps task KDS off regardless of the flag.
+            print("[kitchen-b2] kitchen_b2_active is set but task-based KDS is INERT "
+                  "until B2.2 — serving the full Stage A KDS", flush=True)
+        else:
+            _missing = sales.fired_items_without_tasks(_db)
+            if _missing:
+                print(f"[kitchen-b2] activation BLOCKED: {_missing} fired item(s) without "
+                      "PreparationTasks — serving the full Stage A KDS", flush=True)
+            else:
+                print("[kitchen-b2] task-based KDS active (flag on, no missing tasks)", flush=True)
```

During B2.1 the startup log now says the flag is **INERT until B2.2** — it can never
claim task KDS is active.

## 4. Focused test for the guard (`tests/test_kds_task_reads.py`)

New test (shipped guard value, not flipped):

```python
def test_b2_1_cannot_activate_before_b2_2():
    # In the shipped B2.1 slice the code-level guard is off, so even a manually-set
    # flag WITH readiness == 0 must NOT activate task mode — Stage A stays authoritative.
    check(_sales_mod.B2_2_ACTIVE is False, "B2.2 guard is OFF in the shipped B2.1 slice")
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.commit()
    ch, mi = _base(db, item_station=grill.id)
    _fired_item(db, ch, mi, [grill.id])       # readiness == 0 (the item has a task)
    _flag_on(db)                              # manual flag ON, guard stays False
    check(task_kds_active(db) is False,
          "flag on + readiness 0, but B2.2 absent → gate stays OFF (not activatable)")
    c = _client(db, owner)
    html = _get(c).text
    check("k-tasks" not in html and 'name="item_id"' in html,
          "board is the full Stage A KDS (legacy actions, no task sub-rows)")
    app.dependency_overrides.clear(); db.close()
```

Supporting test-harness change (needed so the existing read tests can still exercise
task mode now that the guard blocks it): a helper that simulates B2.2 present, always
reset in a `finally`:

```python
def _activate(db):
    """Flag ON + flip the code-level B2.2 guard to simulate B2.2 present — the ONLY
    way task mode legitimately switches on. Pair with _reset_guard()."""
    db.add(Setting(key="kitchen_b2_active", value="1")); db.commit()
    _sales_mod.B2_2_ACTIVE = True

def _reset_guard():
    _sales_mod.B2_2_ACTIVE = False
```

The read/blocked tests now call `_activate` (guard on) and `_reset_guard()` in cleanup;
`test_gate_dormant_by_default` and `test_b2_1_cannot_activate_before_b2_2` use only the
plain flag (guard off). The runner wraps the loop in `try/finally: _reset_guard()` so
the in-process guard flip never leaks.

## 5. Test command + result

Environment: Windows, `.venv` (CPython 3.14), SQLite, `PYTHONIOENCODING=utf-8`,
FastAPI `TestClient`.

```
$ .venv/Scripts/python.exe tests/test_kds_task_reads.py
- test_gate_dormant_by_default
- test_b2_1_cannot_activate_before_b2_2
  ok   B2.2 guard is OFF in the shipped B2.1 slice
  ok   flag on + readiness 0, but B2.2 absent → gate stays OFF (not activatable)
  ok   board is the full Stage A KDS (legacy actions, no task sub-rows)
- test_gate_on_only_when_no_missing_tasks
  ok   guard on + flag + zero fired-without-tasks → active
  … (read-path tests) …
all KDS task-read (B2.1) tests passed        (exit 0; 11 tests)
```

Neighbour regression (unchanged, exit 0): `test_stations`, `test_prep_tasks`,
`test_templates`. `py_compile` OK; `import app.main` OK.

## 6. Confirmation

Manually setting `kitchen_b2_active=true` **can no longer activate TASK mode during
B2.1**. Activation now requires `sales.B2_2_ACTIVE` — a **code constant**, not a
setting/env/DB value — which is `False` for the entire B2.1 slice. With it False:
`task_kds_active()` returns False and `kitchen_display` computes `task_mode = False`
regardless of the flag or readiness, so the KDS always serves the complete Stage A
board (no hybrid). The startup log reports the flag as INERT, never active. Only the
future B2.2 slice (which also adds the per-task READY route + legacy-write bridge)
flips `B2_2_ACTIVE` to True.

## 7. Deviations / evidence limits

- **No deviation from B2 v3.** The design already required "no B2.1-only activation";
  this makes it enforced in code rather than relying on the flag being unset.
- The guard is a module constant flipped by the B2.2 slice; the tests flip it
  in-process to exercise the read paths and always reset it (`finally`) — a test-harness
  technique, not a runtime path.
- **Startup warning** remains verified by inspection only (the literal `print` runs at
  module import); its guard/flag logic is exercised by the gate tests. Unchanged limit
  from the prior package.
- SQLite only; no live server / PostgreSQL — acceptable, this fix is read-only gating
  with no concurrency claim.

B2.1 remains **AWAITING INDEPENDENT AUDIT — not approved.** No B2.2 started.
