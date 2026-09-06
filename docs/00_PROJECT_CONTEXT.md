# Restaurant App — Project Context

## Purpose

The Restaurant App is a restaurant point-of-sale and operations system covering order entry, kitchen preparation, service, payment, closeout, reporting, and selected administrative workflows.

This file contains stable project-wide context.

For current structure, read `01_ARCHITECTURE.md`.
For durable approved decisions, read `02_DECISIONS.md`.
For the current authorization checkpoint, read `03_CURRENT_WORK.md`.
For historical milestones, read `04_HISTORY.md`.
For AI/developer operating rules, read `05_AI_HANDOFF.md`.

## Current Product Scope

The current product is:

- single tenant;
- single restaurant/location;
- server-rendered;
- intended for multi-user / multi-terminal operation;
- SQLite-backed in local development/test;
- PostgreSQL-backed in production;
- polling-based for kitchen screens rather than WebSocket/SSE.

Major implemented domains include Menu, Orders, Floor/Tables, Kitchen/Kitchen Stations, PreparationTask, Payments/PaymentAllocation, Refund records, Reservations, Staff/Auth/Permissions, BusinessDay/Closeout, reporting through OLTP-to-star ETL, scheduling, happy hour/day menu/upsell, and CSV/XLSX routing import.

Delivery is partial. Customer/loyalty is not currently a first-class implemented domain.

## Technology Baseline

Current architecture uses Python, FastAPI, SQLAlchemy 2.0, Jinja2, vanilla JavaScript, SQLite locally, PostgreSQL in production, Gunicorn/Uvicorn in production, and a custom `httpx` Square Terminal client on the current Floor/Kitchen line.

## Core Product Invariants

Kitchen lifecycle:

```text
pending
→ preparing
→ ready
→ served
```

`READY != SERVED`.

Routing is explicit, not heuristic. Station routing is snapshotted at fire. Later menu-routing changes must not move already-fired work. Unassigned work stays visible. Inactive stations with live work remain reachable. Coursing is preserved.

`OrderItem` remains the sale line. `PreparationTask` represents station work. Preparation work must not be counted as duplicate sale quantity.

Current Kitchen compatibility/authority rules:

- `OrderItem.station_id` remains as compatibility/legacy routing state;
- `PreparationTask.station_id` is the fired-work station authority in task-mode KDS;
- for task-backed items, PreparationTask owns preparation state;
- `OrderItem.kitchen_status` remains the wider compatibility/rollup state;
- `PreparationTask` does not own SERVED;
- task and legacy station-routing authority must never be mixed on one KDS board.

KDS filtering occurs before board limits. KDS remains polling-based unless explicitly changed by a later approved decision.

## Financial Architecture — Current vs Approved Target

### Current Floor/Kitchen development line

Kitchen, Floor UI, and Reservations are integrated and deployed on the current
line. Historical branches (`feat/floor-map` and the Release A reconstruction
commits) record how that integration happened, not its current status. Current
SHAs, deploy state, and checkpoint: `docs/03_CURRENT_WORK.md`.

The current development line uses:

- `Payment`;
- `PaymentAllocation`;
- integer-cent financial snapshots;
- seat-ledger settlement;
- order-row locking for local settlement;
- direct Square Terminal orchestration;
- local `Refund` records;
- no durable PaymentAttempt/RefundAttempt/provider-reconciliation layer on this line.

### Deployed but dormant Payment/Security architecture

Payment/Security Stage 1, 2a and 2b shipped as Release B and are **DEPLOYED**.

That work adds the hardened production/payment direction, including fail-closed production configuration, durable payment/refund attempt lifecycles, provider abstraction, durable processor evidence, and stronger idempotency/reconciliation foundations.

It is **deployed but dormant**: the code ships in production and no live router invokes the attempt/provider/refund services. Deployed is not the same as active runtime; do not collapse the two.

Payment Stage 2c is **WIP / NOT AUTHORIZED / NOT REVIEWED / NOT DEPLOYED** and is not part of the approved baseline.

Current authorization and deployment status: `03_CURRENT_WORK.md`.

## Financial Safety Principles

Current financial implementation persists money in integer minor units.

External processor truth and local DB truth can diverge; retries must not duplicate financial effects; provider success followed by local failure must be recoverable; refunds and voids are distinct; local cash truth and external-card truth are not equivalent.

On the current Floor/Kitchen line, a local card refund record does not automatically perform a Square processor refund.

Kitchen work must not incidentally redesign payment/refund semantics.

## Security and Production Safety

Current source includes PBKDF2-HMAC-SHA256 PIN hashing, salted PIN storage, legacy PIN upgrade-on-login, signed session cookies, and backend role/permission checks.

The current Floor/Kitchen line also has evidence-supported production configuration risks, including fail-open defaults for critical settings such as `SECRET_KEY`/database configuration and insecure cookie defaults if production variables are missing.

Payment/Security Stage 1 is deployed: absent `APP_ENV` means production, and the fail-closed `SECRET_KEY` guard runs on the import path.

`COOKIE_SECURE` and the SQLite `DATABASE_URL` fallback remain open. See `03_CURRENT_WORK.md` for the current risk register.

## Data / Deployment Principles

- SQLite success does not prove PostgreSQL behavior.
- PostgreSQL-specific concurrency/catalog logic requires PostgreSQL evidence.
- Schema evolution is additive and staged.
- DB invariants are preferred for concurrency-sensitive uniqueness.
- Migration, backfill, and feature activation are separate concerns.
- Production mutation requires explicit authorization.

## Evidence Boundaries

As of the architecture fact audit and the Kitchen B2 checkpoint (committed at `f4dd079` and independently approved):

- no production execution was performed;
- no production data was modified;
- no live payment-provider execution was performed;
- a real PostgreSQL 16 two-session concurrency proof was executed for the B2.2 parent-Order `FOR UPDATE` / post-lock refresh contract and passed;
- this PostgreSQL proof is scoped to B2.2 concurrency and is not a broad production-system certification;
- no runtime load/performance benchmark was performed;
- no real-device/browser compatibility validation was performed.

## Architecture Evolution Model

```text
DESIGN
→ IMPLEMENT
→ HANDOFF
→ AUDIT
→ FIX
→ APPROVAL
→ UPDATE DOCS
→ NEXT SLICE
```

Approved stages become compatibility baselines.

A branch containing approved work is not automatically current runtime architecture if it is unmerged. A current branch is not automatically the full approved target architecture.

## Current High-Level Status

This document carries no status snapshot. Kitchen, Payment/Security, branch and
deployment status live in `03_CURRENT_WORK.md`, and are maintained there only.

Payment Stage 2c is WIP / NOT AUTHORIZED / NOT REVIEWED / NOT DEPLOYED, and
requires explicit future authorization and independent review (ADR-029).

See `03_CURRENT_WORK.md` for the active checkpoint.
