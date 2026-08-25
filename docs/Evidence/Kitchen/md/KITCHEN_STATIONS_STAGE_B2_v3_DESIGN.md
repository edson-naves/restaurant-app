# Kitchen Stations — Stage B2 Design v3 (KDS reads/acts on PreparationTask)

**Status:** design only — no code, no diff, B2.1 NOT authorized. Revises v2 per the
independent review (verdict: DESIGN REVISION REQUIRED; central architecture accepted,
no redesign). Same goal: move KDS station reads + the per-line READY action from
legacy `OrderItem.station_id` to `PreparationTask`, preserving all approved Stage A/B1
behavior. **No B1 redesign.**

**What changed vs v2 — the review items (all resolved in this doc):**
- **[Blocker] §3 station predicate** — removed the ambiguous `= :sid OR IS NULL`; the
  station filter is now one unambiguous, mutually-exclusive rule per mode.
- **[Major] §5 bridge `ready_at`** — the bridge now sets `ready_at=now` **only** on
  `PREPARING→READY` transitions and preserves already-READY tasks' timestamps.
- **[Major] §5 mixed-request atomicity** — the legacy route now **validates all
  targets first and rejects the whole request before any mutation**; no partial commit.
- **[Major] §4 SQLite ≠ PostgreSQL** — dropped the "same serialization" claim; the
  row-lock/post-lock-refresh guarantee is a **PostgreSQL** property requiring PG
  evidence; SQLite is described separately and never as equivalent.
- **[Major] §3 badge counts** — station badges are now explicitly computed over the
  **same order universe and same view/mine/kstatus filters** as the rendered board.
- **[Open decisions] §6** — the three decisions are now **locked** (reviewer's calls),
  including the coarse PREPARING override documented as an explicit **destructive
  station-readiness operation**.

Grounding (unchanged): `kitchen_display` (`GET /kitchen`, `sales.py:1798`), station
EXISTS over `OrderItem.station_id` (`:1901`), `_ticket_courses`/`_line_in_station`
(`:1986`), legacy prep-state action `kitchen_status` (`POST /kitchen/{order_id}/status`,
`:2152`; also Expo's "Mark course ready", `expo.html:60`), `expo_display` (`:2073`),
B1 `rollup_item_kitchen_status` (`:1628`), `fired_items_without_tasks` (`:1677`).

---

## 0. Core principle (unchanged, accepted)

Once an `OrderItem` has PreparationTasks, **kitchen prep state is owned by the tasks**;
`OrderItem.kitchen_status` is a **rollup result**, never independently written by a
competing kitchen path. `PreparationTask` state and the `OrderItem` rollup must never
contradict each other. Legacy items with **no** tasks keep Stage A behavior as a
fallback until migration completes.

---

## 1. Rollout & deployment boundary (unchanged, accepted)

```
B2 Design v3 → approve
 → B2.1 implementation (task query / grouping / counts only)  → audit
 → B2.2 implementation (task READY + compatibility bridge + rollup) → audit
 → verify fired_items_without_tasks(db) == 0
 → ACTIVATE task-based KDS
```

B2.1 is an implementation/audit checkpoint, **NOT** an independently deployable
task-based KDS. Until B2.2 is approved, production KDS runs the Stage A path.
Activation is a single event after B2.2. No B3/B4 here.

---

## 2. Activation gate — hard, no hybrid (unchanged, accepted; decision (a) now locked)

One routing authority at a time — never a board mixing task station membership with
`OrderItem.station_id`-attributed lines.

```
KDS mode = TASK   if  b2_active AND fired_items_without_tasks(db) == 0
         = STAGE_A otherwise      (full legacy board — "B2 not active")
```

- `b2_active` is an explicit operational flag (§6 decision (b): **manual**).
- If `b2_active` is on but `fired_items_without_tasks(db) > 0`: the KDS serves the
  **complete Stage A board** + a prominent operator notice *"B2 activation blocked: N
  fired items without kitchen tasks."* — never a hybrid board.
- **Decision (a) locked — activation failure mode = runtime gate + logged startup
  warning.** The condition `b2_active AND invariant>0` does **not** make startup fatal;
  the app boots, logs a startup warning, and the runtime gate keeps the full Stage A
  board until the invariant clears. (Rationale: a KDS-scoped data condition must not
  take the whole app down; the gate already prevents any unsafe task-board read.)

---

## 3. B2.1 — KDS reads PreparationTask (task mode)

**Order qualification (unchanged gate):** on the board when
`Order.kitchen_status ∈ (PREPARING, READY)` and `Order.status` not in
served/paid/closed/cancelled. Only *station scoping* and *line rendering* move to
tasks; the order-level gate is unchanged, so no fired order vanishes.

### 3.1 Station scoping — one unambiguous rule [Blocker fix]

Station membership for fired work comes **only** from `PreparationTask.station_id`
(the fire snapshot; never re-read live MenuItem/Modifier routing). The three board
modes are **mutually exclusive** and select **exactly one** station predicate — there
is no combined `= :sid OR IS NULL`:

| Mode | Station predicate applied to the active-task EXISTS |
|---|---|
| **all** (`station_filter = None`) | *(no station predicate)* |
| **a station** (`station_filter = :sid`, an int) | `pt.station_id = :sid` |
| **Unassigned** (`station_filter = "unassigned"`) | `pt.station_id IS NULL` |

Canonical qualifying subquery (the `<station-predicate>` is filled from the table
above — never both branches):

```
EXISTS ( SELECT 1
         FROM preparation_task pt
         JOIN order_item oi ON oi.id = pt.order_item_id
         WHERE oi.order_id = "order".id
           AND pt.kitchen_status IN ('preparing','ready')
           [ AND <station-predicate> ] )   -- omitted entirely for 'all'
```

Consequence, stated explicitly to remove the v2 ambiguity: an **Unassigned** task
(`station_id IS NULL`) qualifies an order **only** under the Unassigned tab and under
**all** — **never** under a selected station tab. A selected station tab shows **only**
`pt.station_id = :sid`. Unassigned is never hidden (it always appears on its own tab
and on all), satisfying ADR-004.

### 3.2 Line rendering scope

The same single rule governs which task sub-rows render on a station board: on a
selected station, render only that item's task(s) with `pt.station_id = :sid`; on
Unassigned, only `pt.station_id IS NULL`; on all, all active tasks. An order that
qualifies but whose visible task set is empty under the active filter is dropped from
the page (mirrors today's `_ticket_courses` returning no courses).

### 3.3 Station badge counts — same universe as the board [Major fix]

Badges must never diverge from what the board shows. They are computed over the
**identical order universe and filters** as the rendered set, differing **only** by
grouping on station instead of applying a single station filter:

- Same base predicate as the board: `_on_board` (the `Order.kitchen_status`/`Order.status`
  gate) **and** the same `_view_mine` (channel view + "my tables") **and** the same
  `kstatus` filter (§3.5). The **station** filter is the only thing not applied — it is
  replaced by `GROUP BY COALESCE(pt.station_id, <unassigned-key>)` over the active tasks
  of those same orders.
- Therefore each tab's badge = exactly the count of active station-work units that the
  board would show if that tab were selected; the "all" badge = the sum over the same
  universe; the Unassigned badge = the `station_id IS NULL` group over the same universe.
- Counting unit is **active PreparationTasks** (station-work units), so a multi-station
  Burger contributes Grill +1 and Fryer +1 (accepted). This is **not** a sale-item
  count (§3.4).

### 3.4 Count terminology (accepted)

Two distinct quantities, never conflated in the UI:
- **`total_items`** — sale-item quantity (`OrderItem.quantity`); ticket header / expo
  tray count. Unchanged meaning.
- **station work count** — the task badges of §3.3. Labeled as station work, never
  "items".

### 3.5 `kstatus` stays ORDER-level (accepted)

The top-level Preparing/Ready filter continues to mean `Order.kitchen_status`, and
`status_counts` continues to come from `Order.kitchen_status`. B2 does **not** repoint
`kstatus` at `PreparationTask.kitchen_status`. A per-station task-status filter is a
separate future change.

### 3.6 Ordering / limit / total (accepted)

Station EXISTS + `kstatus` applied in SQL **before** `order_by(Order.sent_to_kitchen_at)`
(oldest first) and `.limit(80)`. `board_total` counts the same filtered set (subquery
COUNT, no LIMIT). Inactive stations with live tasks keep their tab: `tab_ids` = station
keys present in the §3.3 counts ∪ active stations. Coursing: group by snapshotted
`task.course`; held courses have no fired tasks. N+1 avoided via
`selectinload(Order.items → OrderItem.tasks → PreparationTask.station)`.

**No mutation in B2.1.**

### 3.7 Ticket presentation (accepted)

Render the sale line once, tasks as sub-rows/chips — never duplicate items:

```
Burger ×1
├── Grill      READY
├── Fryer      PREPARING
└── Cold Prep  READY
```

- **Station board:** show only that station's task; only that task is actionable (B2.2).
- **all board:** course → `OrderItem` → per-station task chips.
- **Course / line status on `all`:** from the rolled-up `OrderItem.kitchen_status`
  (decision #3), so `all` and station boards agree with order/payment state.

---

## 4. B2.2 — per-task READY (task mode)

**Route:** `POST /kitchen/tasks/{task_id}/ready`, `require("kitchen.update")`, redirect
via `_safe_next_board(next)`.

**Sequence (post-lock refresh):**
1. `db.get(PreparationTask, task_id)` — only to learn its `order_id`; 404 if missing.
2. **Lock parent Order** `SELECT … FROM "order" WHERE id=:oid FOR UPDATE`.
3. **Refresh from post-lock committed state:** re-select the task **and** the sibling
   `OrderItem.tasks` with `populate_existing=True` (or `db.refresh(..., ['kitchen_status','ready_at'])`
   + refresh the item's `tasks`), so the transition and rollup act on current rows, not
   pre-lock identity-map objects.
4. **Explicit transition (per-task):**
   - `PREPARING → READY` — set `ready_at = now`.
   - `READY → READY` — idempotent no-op; **preserve the original `ready_at`**.
   - `SERVED → READY` (or anything else) — **rejected** (409/400). Not "anything not
     READY becomes READY".
5. Never set task/OrderItem/Order to SERVED.
6. `rollup_item_kitchen_status(task.order_item)` — approved B1 fn, unchanged.
7. `_recompute_kitchen(order, now)` — Order/table state + ready-to-pay derive exactly
   as today; **payment gate byte-for-byte unchanged**.
8. `db.commit()`.

### 4.1 Concurrency evidence — PostgreSQL is the authority [Major fix]

The correctness argument rests on the parent-Order **row lock** + **post-lock refresh**:
two concurrent READYs on sibling tasks each take `FOR UPDATE` on the same Order row, so
the second waits, then refreshes and sees the first's committed task state; the final
rollup is correct regardless of arrival order, and duplicate READY is idempotent
(step 4 preserves `ready_at`).

**This guarantee is a PostgreSQL property and must be evidenced on PostgreSQL.**
- On **PostgreSQL** (production): `SELECT … FOR UPDATE` takes a real row lock; the
  serialize-then-refresh sequence is the actual mechanism. Concurrency correctness for
  B2.2 must be proven by a **PostgreSQL integration test** (two overlapping sessions),
  not inferred from SQLite.
- On **SQLite** (dev/tests): `FOR UPDATE` is a **no-op** (SQLAlchemy omits the clause).
  SQLite serializes *writers* at the database level, which prevents interleaved commits,
  but this is a **coarser, different** guarantee — it does **not** exercise row-lock
  semantics or the post-lock-refresh-under-contention path. SQLite tests demonstrate the
  transition/rollup/idempotency logic; they are **not** evidence of the production
  row-lock behavior and must not be described as equivalent.
- Consistent with ADR-010 (SQLite passing ≠ PostgreSQL proof). B2.2 acceptance should
  therefore carry an explicit PostgreSQL concurrency-evidence item (see §9).

---

## 5. Compatibility bridge for legacy prep-state writes (BLOCKER 1 from v2 — refined)

The shared legacy action `kitchen_status` (`POST /kitchen/{order_id}/status`; used by
the KDS **and** Expo's "Mark course ready") currently writes `OrderItem.kitchen_status`
directly. Once B2 is active this can diverge a task-backed item from its tasks. The
bridge makes those writes task-consistent while Expo remains **visually** Stage A
(no rendering change until B3).

### 5.1 Two-phase, all-or-nothing handling [Major fix: atomicity]

The route may target a mix of task-backed and legacy (no-task) items (item / course /
whole-order). It runs in **two phases under the Order lock**, so there is never a
partial commit:

**Phase 1 — validate every target BEFORE any mutation.** Resolve the target set, then
check each item against the requested status. If **any** target is unsupported, **abort
the entire request with no mutation and no commit** (`rollback`, 4xx). Unsupported =:
- **PENDING requested on a task-backed item** — not supported in B2 (un-firing tasked
  work needs the deferred re-fire/remake model). The UI never offers PENDING for these
  lines; a crafted/mixed request that includes one is rejected wholesale here.
- any transition disallowed by §5.2 (e.g. a task-backed target whose only tasks are
  SERVED under a READY request → rejected, mirroring §4 step 4).

Legacy (no-task) items accept PENDING/PREPARING/READY as today; they never trigger a
Phase-1 rejection on their own.

**Phase 2 — apply all mutations, then one `_recompute_kitchen` + one `commit`.** Reached
only if Phase 1 passed for every target. No target is mutated before all are validated.

### 5.2 Per-target translation (task-backed items) [Major fix: preserve `ready_at`]

For a task-backed item, do **not** write `item.kitchen_status`; translate through its
**active** (non-served) tasks using the **same per-task transition rule as §4**, then
rollup:

- request **READY** → for each active task: `PREPARING → READY` sets `ready_at = now`;
  a task already **READY is left untouched (its original `ready_at` is preserved)**;
  a SERVED task is not modified. Then `rollup_item_kitchen_status(item)` → item READY.
  *(This is the fix for the v2 defect where the bridge set `ready_at=now` on all active
  tasks and could overwrite an existing READY timestamp.)*
- request **PREPARING** → **coarse override**, see §5.3.
- request **PENDING** → rejected in Phase 1 (§5.1).

Legacy no-task items: unchanged Stage A behavior (`item.kitchen_status = status`).

Invariant preserved everywhere: **tasks and the `OrderItem` rollup never contradict.**
Because activation is gated on `fired_items_without_tasks == 0`, every fired item on an
active B2 board is task-backed and flows through the task branch.

### 5.3 Coarse PREPARING override = explicit DESTRUCTIVE station-readiness op [decision (c) locked]

**Decision (c) locked:** a legacy/Expo PREPARING request on a task-backed item resets
**all** of the item's **active** tasks to PREPARING (clearing each task's `ready_at`),
then rolls up → item PREPARING.

This is **documented and surfaced as a destructive station-readiness operation**, not
implicit behavior: it **discards per-task READY progress at every station of that item**
(e.g. a Grill task already READY is knocked back to PREPARING along with the Fryer).
Requirements:
- the action must be explicit/audited — a coarse "un-ready this item" gesture, not a
  side effect; the operator-facing control and any log/notice must say it clears all
  stations' readiness for the item;
- it clears `ready_at` on the reset tasks (they are no longer ready);
- it never touches SERVED tasks and never sets SERVED.

Rationale: this matches the semantics of the existing coarse legacy/Expo button (which
today flips the whole line) while keeping task state consistent. Fine-grained,
non-destructive control is the per-task route (§4); the coarse override is deliberately
all-or-nothing for the item.

---

## 6. Decisions — now locked (was "open" in v2)

1. **Activation strictness → hard gate** (§2). No hybrid station routing.
2. **`all` grouping → OrderItem with task sub-rows/chips** (§3.7).
3. **Course/status source on `all` → rolled-up `OrderItem.kitchen_status`** (§3.7).
4. **Task SERVED on serve → leave tasks READY** in B2; no task-SERVED propagation;
   serving lifecycle already clears the board; task-served/reporting deferred to B3/B5.
5. **Lock granularity → parent Order row lock** + post-lock refresh (§4).
6. **(a) Activation failure mode → runtime gate + logged startup warning** (not
   startup-fatal) (§2).
7. **(b) `b2_active` → manual** operational flag, flipped only after verifying
   `fired_items_without_tasks == 0`; no automatic "activate on first zero" latch (§2).
8. **(c) Coarse PREPARING override → reset all active tasks of the item**, explicitly
   documented/surfaced as a **destructive station-readiness operation** (§5.3).

No decisions remain open.

---

## 7. Backward compatibility (unchanged in B2)

Payment gate; serving semantics; READY ≠ SERVED; coursing; held courses create no
active tasks; **Expo & Floor stay on the Stage A read path** (B3) — only Expo's *write*
goes through the bridge; menu-routing UI; modifier-routing UI (none yet);
`OrderItem.station_id` retained (ADR-006); PreparationTask creation/backfill rules;
`kstatus` order-level semantics.

---

## 8. Files expected to change (design intent; not authorized to implement)

**B2.1 (read-only):** `app/routers/sales.py` — `kitchen_display`: single-rule station
EXISTS (§3.1), badge counts over the shared universe (§3.3), `selectinload` tasks→station,
`tab_ids` from task counts, task-based grouping helper (legacy `_ticket_courses`/
`_line_in_station` retained for the Stage-A / `b2_active`-off path), `task_kds_active(db)`
mode selector. `web/templates/kitchen.html` — item→task sub-rows, badges labeled station
work.

**B2.2:** `app/routers/sales.py` — new `POST /kitchen/tasks/{task_id}/ready` (post-lock
refresh + explicit transition); **`kitchen_status` two-phase bridge** (§5) with Order
lock + refresh; `b2_active` flag surface. `web/templates/kitchen.html` — per-task READY
button; explicit "un-ready item (all stations)" control per §5.3.

**No model change, no migration** — `PreparationTask` already has `kitchen_status` and
`ready_at`; `b2_active` reuses the existing settings mechanism.

---

## 9. Lean test boundaries (implementation-time; reuse Stage A/B1 regression)

**B2.1**
- station query uses the single-rule PreparationTask predicate (§3.1): selected station
  shows only `= :sid`; **Unassigned tasks do NOT appear under a selected station**;
  Unassigned visible on its own tab and on all;
- badge counts equal the board content for each tab (same universe, §3.3);
- filter before LIMIT; `board_total` matches; inactive station with live tasks keeps its
  tab; multi-station item renders once (quantities not inflated);
- activation blocked (`fired_items_without_tasks > 0` with `b2_active`) → full Stage A
  board + notice + logged startup warning, never a hybrid.

**B2.2**
- one task READY does not ready a sibling; all required tasks READY → OrderItem READY;
- duplicate READY preserves original `ready_at`;
- **PostgreSQL** concurrency proof: two overlapping sessions READYing sibling tasks →
  correct final rollup (explicitly a PG integration test; SQLite is not accepted as
  equivalent evidence, §4.1);
- **legacy/Expo bridge:** READY routes through tasks and **preserves already-READY
  `ready_at`** (§5.2); a mixed request containing an unsupported target (e.g. PENDING on
  a task-backed item) is **rejected wholesale with no partial mutation** (§5.1);
- **coarse PREPARING override** resets all active tasks of the item and is surfaced as
  destructive (§5.3);
- tasks never cause SERVED; payment behavior unchanged; `SERVED→READY` rejected.

---

## 10. Evidence gaps / non-blocking notes carried forward

- PostgreSQL concurrency evidence for B2.2 is **required at B2.2 audit**, not at design
  time (no live PG in the current environment; ADR-010).
- The audit-flagged pre-existing items (SECRET_KEY fail-open, card-terminal durability,
  `test_admin` failure) are **out of B2 scope** and tracked separately; B2 must not
  touch payment or security behavior.

Design only. B2.1 remains **NOT AUTHORIZED** until this v3 is approved.
