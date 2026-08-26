# Restaurant App — Architecture

## Purpose

Describe current structural architecture, important runtime flows, migration states, branch distinctions, and evidence limits.

This file describes structure; it does not authorize work.

## Runtime Stack

- FastAPI
- SQLAlchemy 2.0
- Jinja2
- vanilla JavaScript
- SQLite in development/test
- PostgreSQL in production
- Gunicorn + Uvicorn workers in production
- custom additive migration/bootstrap logic
- polling-based Kitchen UI
- custom `httpx` Square Terminal client on the current Floor/Kitchen line

Observed parity note: local/dev artifacts target Python 3.14 while the production Docker image uses Python 3.13.

## Repository Areas

Major areas:

```text
app/
db/
mvp_menu_upload/
outbox/
scripts/
tests/
web/
docs/
```

Important ownership includes OLTP models, star-schema models, routers, services, custom migrations, DB/session configuration, auth/permissions, security, ETL/reporting, templates/static assets, and tests.

There is no formal repository layer. Significant business logic exists both in services and routers.

## Architectural Layering

Actual structure is pragmatic:

```text
Browser / Jinja UI
        ↓
FastAPI routers
        ↓
services + router-owned orchestration
        ↓
SQLAlchemy models/session
        ↓
SQLite / PostgreSQL
```

Current external card path:

```text
pay router
   ↓
Square service
   ↓
Square Terminal API
```

Structural observations:

- Kitchen state logic is concentrated heavily in `sales.py`;
- Square orchestration is invoked directly from payment routing on the current line;
- env/config reads are scattered on the current line;
- templates are not a valid sole enforcement point for invariants.

## Environment / Deployment Model

Local/test primarily uses SQLite. Production uses PostgreSQL with Gunicorn/Uvicorn.

Repository deployment scaffolding supports multiple targets; do not infer one vendor as permanent architecture without current evidence.

Current Floor/Kitchen production risks include fail-open defaults for `SECRET_KEY`, `DATABASE_URL`, and cookie security when environment configuration is missing.

Approved-but-unmerged Payment/Security Stage 1 establishes the fail-closed target direction.

## Startup / Migration Architecture

Schema evolution is custom rather than Alembic-driven.

It uses:

- SQLAlchemy `create_all` for missing tables;
- guarded additive schema changes;
- SQLite/PostgreSQL-specific introspection;
- targeted backfills.

Important distinction:

```text
required schema invariant
≠
best-effort data backfill
```

PreparationTask fire-batch uniqueness is a required verified invariant. PreparationTask backfill is observable/best-effort, with readiness used to detect incompleteness.

## Main Domains

Implemented or partially implemented domains include Menu, Orders, Seats, Floor/Tables, Stations, PreparationTask, Payments, Refunds, Reservations, Staff/Auth/Permissions, Channel, partial Delivery, BusinessDay/Closeout, reporting/star schema, scheduling, happy hour/day menu/upsell, and imports/exports.

Customer/loyalty is not currently first-class.

## Order Lifecycle

```text
create
→ add items/modifiers
→ hold/course
→ fire/send
→ prepare
→ READY
→ SERVED
→ payment
→ completion/close
```

`READY != SERVED`.

Key compatibility/derived state includes `Order.status`, `Order.kitchen_status`, and `OrderItem.kitchen_status`.

Payment eligibility/service semantics must not be weakened by Kitchen migration.

## Kitchen Stations — Stage A

Stage A introduced Station, explicit MenuItem routing, `OrderItem.station_id`, snapshot-at-fire, Unassigned, station-filtered KDS, Expo/Floor station display, and CSV/XLSX routing import.

Rules:

- no heuristic routing;
- historical fired routing is immutable;
- inactive stations with live work remain reachable;
- filter before LIMIT.

Stage A is APPROVED / CLOSED.

## PreparationTask — Stage B1

B1 introduced:

```text
Order
  ↓
OrderItem
  ↓
PreparationTask(s)
```

with `PreparationTaskModifier`, nullable modifier routing, multi-station tasks at fire, snapshots, Unassigned support, rollup, `fire_seq`, fire-batch uniqueness, conflict-safe/idempotent fire behavior, backfill/readiness, and `fired_items_without_tasks(db)`.

Compatibility retained:

- `OrderItem.station_id`;
- `OrderItem.kitchen_status`;
- PreparationTask does not own SERVED.

B1 is APPROVED / CLOSED. It was authored on `feat/floor-map`, reconstructed onto
`045dfd5` for Release A, and is now integrated into local `main` at `7225c6e`.
Integrated locally only — not pushed, not deployed.

## Kitchen Stage B2 — Current Approved Architecture

B2 v3 **design** is APPROVED WITH NON-BLOCKING NOTES. The B2.2 implementation is
committed and, following an independent re-audit at `f4dd079` (APPROVED WITH
NON-BLOCKING NOTES), **Kitchen Stage B2 / B2.2 are APPROVED / CLOSED**. The
architecture below is the approved, committed behavior.

B2 moves KDS station reads/actions for task-backed work to PreparationTask while preserving the OrderItem sale line and compatibility state.

### Task-mode read authority

When task mode is active:

- station membership comes only from snapshotted `PreparationTask.station_id`;
- selected station = exact `station_id`;
- Unassigned = `station_id IS NULL`;
- All = no station predicate;
- a sale `OrderItem` renders once, with task/station subrows;
- task badge counts are station-work counts, not sale-item quantity;
- top-level `kstatus` remains order-level;
- `OrderItem.kitchen_status` remains the visible compatibility/rollup status;
- inactive stations with live tasks remain reachable;
- filtering occurs before LIMIT.

### Activation gate

Task-based KDS is active only when all required gates pass:

```text
B2_2_ACTIVE
AND kitchen_b2_active
AND fired_items_without_tasks(db) == 0
```

The operational setting is manual. A blocked activation serves the complete Stage A board rather than a hybrid board.

B2.1 introduced the task-read path while remaining inert until B2.2 capability existed. B2.2 enables the capability but does not itself imply the manual operational flag is on.

### Task READY mutation

Task READY route:

```text
POST /kitchen/tasks/{task_id}/ready
```

Allowed task transition contract:

```text
PREPARING → READY
READY → READY   (idempotent; preserve ready_at)
anything else → reject
```

The mutation synchronizes on the parent Order:

```text
identify parent Order
→ SELECT Order ... FOR UPDATE
→ reselect/refresh task and siblings AFTER lock
→ validate
→ mutate task
→ roll up OrderItem.kitchen_status
→ _recompute_kitchen(order)
→ commit
```

The pre-lock task view is not authoritative after lock acquisition.

### Legacy write compatibility bridge

The shared legacy kitchen-status write route remains for Stage A/Expo compatibility, but task-backed items do not independently write `OrderItem.kitchen_status`.

For task-backed targets:

- READY routes through active tasks, preserving existing READY timestamps;
- PREPARING is a coarse destructive un-ready operation that resets active tasks to PREPARING and clears `ready_at`;
- PENDING is rejected;
- validation occurs for every target before any mutation;
- invalid mixed requests are all-or-nothing;
- unsupported task states are rejected before Phase 2.

The task-mode UI explicitly labels the destructive coarse action as `Un-ready (all stations)`.

### SERVED and payment compatibility

PreparationTask still does not own SERVED.

Task readiness never sets OrderItem/Order to SERVED.

Payment gate/service semantics remain unchanged.

### B2 PostgreSQL concurrency evidence

A real PostgreSQL 16 two-session proof validated the parent-Order `FOR UPDATE` contract:

- session B blocked on the same Order lock while A held it;
- `pg_stat_activity` observed B waiting on a Lock;
- after A committed, B's post-lock read observed A's READY state;
- final persisted state had both sibling tasks READY, OrderItem READY, Order not SERVED;
- no lost update or stale rollup was observed.

This evidence is scoped to the B2.2 concurrency contract; it is not broad production certification.

### Deferred Kitchen work

Not part of B2:

- B3/B4;
- Expo/Floor task-read migration beyond the B2 compatibility bridge;
- task-owned SERVED/reporting;
- remake/re-fire model;
- `OrderItem.station_id` removal;
- richer task states;
- WebSocket/SSE.

## Current Payment Architecture (unchanged by this release)

Current persisted model:

```text
Payment
PaymentAllocation
Refund
```

Characteristics:

- integer-cent money;
- seat-ledger settlement;
- split/partial/multi-tender support;
- order row locking for local settlement;
- derived outstanding balance.

Current Square flow:

```text
compute payable
→ create Square checkout
→ process/poll terminal
→ observe COMPLETED
→ create local Payment + allocations
→ commit
```

Evidence-supported limitations on this line:

- no durable PaymentAttempt before external charge I/O;
- no persisted full provider checkout/payment identity;
- no general provider abstraction;
- incomplete provider amount/currency reconciliation;
- processor success can precede durable local settlement;
- processor-level retry/idempotency is incomplete.

## Refunds on Current Line

Current `Refund` is a durable local reversal record constrained by refundable amount.

Important:

```text
A local card Refund does NOT automatically execute a Square refund.
```

Void and refund are distinct.

## Approved-but-Unmerged Payment/Security Architecture

Git reconciliation established:

```text
fix/p0-security-and-payments
```

contains committed/pushed remediation that was never merged into main, into
`feat/floor-map`, or into this release. It is Release B, separately designed.

Status:

- Stage 1 — APPROVED BUT UNMERGED
- Stage 2a — APPROVED BUT UNMERGED
- Stage 2b — APPROVED BUT UNMERGED
- Stage 2c — WIP / AUTHORIZED-NOT-CLOSED

Approved Stage 1/2a/2b target architecture includes fail-closed production config, readiness/liveness separation, durable `PaymentAttempt`, independent `RefundAttempt`, provider abstraction, durable provider evidence, stronger idempotency/state transitions, and stronger amount/currency/reconciliation foundations.

Do not describe these as current runtime on any line, including this release.

Do not merge the branch tip wholesale because it also contains Stage 2c WIP.

## Financial Arithmetic

Persisted financial values use integer minor units. Integer distribution/largest-remainder behavior supports exact splits. A percentage helper currently uses floating arithmetic before conversion to cents; treat that as an implementation note, not a durable money model.

Settled Payment fields and receipt payloads are snapshots.

## Authentication / Authorization

Current security includes:

- signed HMAC-SHA256 staff session cookies;
- PBKDF2-HMAC-SHA256 PIN hashing;
- per-PIN salt;
- legacy plaintext-PIN migration;
- backend role→permission checks;
- deactivated-staff rejection.

Backend authorization is authoritative.

## Time / BusinessDay

Current app uses restaurant-local time, with observed default `America/Vancouver`. Timestamps are generally naive local datetimes. DayClose uses local calendar date; no custom business-day cutoff was confirmed.

## Reporting

Reporting uses:

```text
OLTP
→ ETL
→ star schema
→ reports / analytics / closeout
```

Any financial lifecycle change must consider downstream ETL/reporting effects.

## Tables / Reservations / Channels

Table occupancy is persisted in RestaurantTable state. Current table-open path has an unresolved concurrency risk.

Reservations exist with status/time/no-show behavior; no explicit concurrency-safe overlap guard was confirmed.

Channel distinguishes dine-in/takeout/delivery semantics. Delivery is partial.

## Observability / Auditability

Current observability includes console-style logging, `/healthz`, and a narrow persistent `AuditEntry` used for selected table actions.

Not established as general current architecture: structured metrics, tracing, broad alerting, full financial/config audit stream, or separate readiness on the current Floor/Kitchen line.

## Performance / Capacity

No runtime benchmark/load test was performed.

Structural protections include filtering-before-limit, bounded board queries, SQL aggregation, eager/select-in loading in important paths, and star-schema reporting.

Do not claim index use without query-plan evidence.

## Responsive / Device Architecture

UI is server-rendered Jinja + vanilla JS.

No real-device/browser validation was performed. Desktop/tablet/phone/touch support is therefore NOT VERIFIED unless later evidence exists.

## Evidence Boundaries

- SQLite functional/regression tests executed for Kitchen slices;
- real PostgreSQL 16 concurrency evidence executed and passed for B2.2 parent-Order lock/post-lock refresh;
- no production execution;
- no production data mutation;
- no live payment-provider execution during the fact audit;
- no load benchmark;
- no real-device/browser certification.
