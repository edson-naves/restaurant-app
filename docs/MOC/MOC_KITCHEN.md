# Kitchen Stations — Map of Content

> [!important]
> This MOC is an index, not a source of truth.
> - Current authorization/checkpoint: [[docs/03_CURRENT_WORK]]
> - Current structural architecture: [[docs/01_ARCHITECTURE]]
> - Durable accepted decisions: [[docs/02_DECISIONS]]
> - Historical chronology: [[docs/04_HISTORY]]
>
> If this MOC and a canonical document disagree, use the canonical document and flag the drift.

## Domain entry points
- [[docs/00_PROJECT_CONTEXT]]
- [[docs/01_ARCHITECTURE]]
- [[docs/02_DECISIONS]]
- [[docs/03_CURRENT_WORK]]
- [[docs/04_HISTORY]]
- [[docs/05_AI_HANDOFF]]

## Approved architecture / design
- [[KITCHEN_STATIONS_STAGE_B2_v3_DESIGN]]

Core invariants:
- `READY != SERVED`
- explicit routing; no heuristic routing
- routing snapshot at fire
- Unassigned remains visible
- inactive stations with live work remain reachable
- filter before LIMIT
- `OrderItem` remains the sale line
- `PreparationTask` represents station work
- `OrderItem.kitchen_status` remains compatibility/rollup state
- PreparationTask does not own SERVED
- no hybrid task/legacy KDS authority
- payment-gate semantics remain unchanged

## Integration state

Release A is integrated into local `main` at `7225c6e` (`97bed73` + `7225c6e`,
no squash). Not pushed, not deployed; `origin/main` remains `045dfd5`.
Authoritative checkpoint: [[docs/03_CURRENT_WORK]].

## Stage status

### Stage A
**APPROVED / CLOSED**

### Stage B1
**APPROVED / CLOSED**

### Stage B2 design
- [[KITCHEN_STATIONS_STAGE_B2_v3_DESIGN]]

**APPROVED WITH NON-BLOCKING NOTES**

### Stage B2.1
**APPROVED**

Evidence:
- [[KITCHEN_STATIONS_STAGE_B2_1_REVIEW_PACKAGE]]
- [[KITCHEN_STATIONS_STAGE_B2_1_FIX_REVIEW]]

Carry-forward notes:
- SQLite/TestClient functional evidence for the read slice
- startup warning literal was inspection-based
- pre-existing `test_admin` failure remained out of scope

### Stage B2.2
**APPROVED / CLOSED** (checkpoint `f4dd079` independently re-audited — APPROVED WITH NON-BLOCKING NOTES)

Evidence chain:
- [[KITCHEN_STATIONS_STAGE_B2_2_REVIEW_PACKAGE]]
- [[KITCHEN_STATIONS_STAGE_B2_2_FIX_REVIEW]]
- [[KITCHEN_STATIONS_STAGE_B2_2_FIX_REVIEW_2]]
- [[KITCHEN_STATIONS_STAGE_B2_2_POSTGRES_CONCURRENCY_EVIDENCE]]

Closed findings:
- all-target Phase-1 bridge validation
- all-SERVED/no-active-task rejection
- unsupported active task-state rejection
- explicit destructive `Un-ready (all stations)` UI
- real PostgreSQL parent-Order lock/post-lock refresh proof

## Current B2 architecture
Task mode uses:
- `PreparationTask.station_id` for fired-work station membership
- PreparationTask prep-state authority
- `OrderItem.kitchen_status` rollup compatibility
- one sale line with task subrows
- per-task READY
- parent Order `FOR UPDATE` + post-lock refresh
- hard no-hybrid activation gating

Activation requires:
```text
B2_2_ACTIVE
AND kitchen_b2_active
AND fired_items_without_tasks(db) == 0
```

## Audit / maintenance trail
Design → implementation → review → fix → PostgreSQL evidence is complete for B2; the
final independent **approval** of B2.2 was recorded at `f4dd079` (APPROVED WITH NON-BLOCKING NOTES; reused the four Kitchen tests + PostgreSQL evidence).

Future Kitchen work must start as a new authorized slice rather than reopening B2 implicitly.

## Deferred / later Kitchen work
Do not infer authorization from this list:
- B3/B4
- Expo/Floor task-read migration beyond current compatibility behavior
- task-owned SERVED/reporting
- remake/re-fire model
- `OrderItem.station_id` cleanup/removal
- richer task states
- WebSocket/SSE
