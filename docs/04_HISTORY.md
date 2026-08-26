# Restaurant App — Project History

## Purpose

Compact chronological record of meaningful architecture transitions, audits, approvals, and governance milestones.

This file does not authorize work.

## 2026-08-08 — Payment/Floor Lines Share a Common Ancestor

Git reconciliation later established `07ee4f1` as the common ancestor from which the Payment/Security remediation line and later main/Floor/Kitchen line diverged.

## Payment / Security Remediation

### 2026-08-09 — Stage 1

Introduced hardened production-configuration direction, including fail-closed production config and liveness/readiness separation.

**Stage 1 — APPROVED**

Remained on `fix/p0-security-and-payments`.

### Stage 2a

Introduced durable payment-attempt architecture and later hardening:

- `PaymentAttempt`;
- durable payable snapshot;
- provider identifiers/evidence;
- idempotency;
- explicit attempt state;
- independent refund-attempt lifecycle through review fixes;
- concurrency/state-transition hardening.

**Stage 2a — APPROVED**

Remained unmerged.

### Stage 2b

Introduced provider abstraction and provider-contract hardening.

**Stage 2b — APPROVED**

Remained unmerged.

### Stage 2c

Began later settlement/charge integration.

Branch tip remained WIP.

```text
Stage 2c — WIP / AUTHORIZED-NOT-CLOSED
```

## Kitchen Stations — Stage A

Introduced Station, MenuItem routing, `OrderItem.station_id`, snapshot-at-fire, Unassigned, KDS filtering, Expo/Floor presentation, and CSV/XLSX routing import.

Preserved `OrderItem.kitchen_status` and lifecycle:

```text
pending → preparing → ready → served
```

Critical invariant: `READY != SERVED`.

Audit fixes included filter-before-limit, import safety, ID validation, station constraints, name normalization, inactive live-work handling, Expo limits, redirects, and relationship loading.

**Stage A — APPROVED / CLOSED**

## Stage B — Multi-Station Preparation

Architecture moved toward:

```text
OrderItem
    ↓
PreparationTask(s)
```

without duplicating the sale line.

### Stage B1

Introduced PreparationTask foundation, modifier routing, multi-station fire behavior, snapshots, Unassigned, rollup, `fire_seq`, uniqueness, concurrency/idempotency safeguards, and readiness/backfill.

Preserved `OrderItem.station_id`, `OrderItem.kitchen_status`, and no task-owned SERVED.

Reviews tightened task identity, conflict handling, exact index definition, and evidence boundaries.

**Stage B1 — APPROVED / CLOSED**

Git reconciliation later showed relevant B1 changes were still in the inspected working tree rather than fully committed to the historical `feat/floor-map` HEAD.

### Stage B2 v1

Review identified blockers around legacy READY divergence, activation, hybrid authority, stale pre-lock state, and task transition rules.

**DESIGN REVISION REQUIRED**

### Stage B2 v2

Independent review found a station-predicate ambiguity, bridge timestamp/atomicity gaps, SQLite/PostgreSQL evidence ambiguity, badge-universe ambiguity, and unresolved activation/override decisions.

**DESIGN REVISION REQUIRED**

### Stage B2 v3

Resolved the v2 findings:

- mutually exclusive station predicates;
- hard no-hybrid activation gate;
- one OrderItem with task subrows;
- task counts separated from sale quantity;
- post-lock refresh contract;
- explicit READY transition contract;
- two-phase compatibility bridge;
- preserved `ready_at`;
- destructive coarse PREPARING semantics explicitly surfaced;
- PostgreSQL-specific concurrency evidence requirement.

**B2 v3 DESIGN — APPROVED WITH NON-BLOCKING NOTES**

### B2.1

Implemented task-based KDS reads, task badge counts, task subrows, Unassigned/inactive-station handling, Stage A fallback, activation notice, and activation guards.

Independent audit found that the first B2.1 gate could still be enabled operationally before B2.2. A code-level `B2_2_ACTIVE` capability guard fixed that.

**B2.1 — APPROVED WITH NON-BLOCKING NOTES**

### B2.2

Implemented:

- `POST /kitchen/tasks/{task_id}/ready`;
- parent Order `FOR UPDATE`;
- post-lock task/sibling refresh;
- idempotent READY;
- OrderItem rollup;
- legacy kitchen-status two-phase bridge;
- coarse destructive PREPARING override;
- task-mode per-task READY controls.

Independent audit required fixes for:

- validating no-active/all-SERVED task-backed targets;
- explicit operator-facing `Un-ready (all stations)`;
- rejecting unsupported task states before any Phase-2 mutation.

All implementation findings were resolved.

### 2026-08-22 — B2.2 PostgreSQL Concurrency Proof

A real PostgreSQL 16 two-session proof executed the approved parent-Order locking contract.

Observed:

- Session A acquired and held the parent Order `FOR UPDATE`;
- Session B blocked on the same lock;
- `pg_stat_activity` showed B waiting on a Lock;
- measured B wait was approximately the deterministic hold window;
- after A committed, B's post-lock read observed task A READY;
- task B then transitioned READY;
- final persisted state: both tasks READY, OrderItem READY, Order not SERVED;
- no lost update, stale rollup, or contradictory state.

Evidence artifact:

```text
KITCHEN_STATIONS_STAGE_B2_2_POSTGRES_CONCURRENCY_EVIDENCE.md
```

**B2.2 — implementation findings resolved; PostgreSQL concurrency proof passed.**

## 2026-08-23 — Kitchen Checkpoint Committed + Independently Re-Audited

The Kitchen Stage A/B1/B2 work was committed on `feat/floor-map`. The initial
checkpoint (`13c0fa2`) was incomplete — required Kitchen/Stations code sat in shared
files — so a clean checkout broke `test_stations` and the docs over-claimed approval.
Follow-up fix commits (no amend/reset/merge/push) resolved this:

```text
9bd5b73  complete Stage A/B2 hunks from shared files (admin.py, app.css, admin_nav,
         admin_menu, base.html) + correct approval docs
a79dd95  commit required app/services/reservations dependency (imported by sales.py)
fd11ecd  include Stage A floor per-station readiness strip (floor.html + app.css)
f4dd079  remove residual B2.2 "APPROVED / CLOSED" doc claims
```

The four Kitchen tests (`test_stations`, `test_prep_tasks`, `test_kds_task_reads`,
`test_kds_task_ready`) were re-run green from a clean worktree of `f4dd079`.

The committed checkpoint `f4dd079` was then **independently re-audited: APPROVED WITH
NON-BLOCKING NOTES** (the approval reused the four already-executed Kitchen tests and
the existing PostgreSQL concurrency evidence — no new execution required).

**Kitchen Stage B2 / B2.2 — APPROVED / CLOSED.**

## 2026-08-20 — Obsidian Persistent Context Established

Created:

```text
docs/
├── 00_PROJECT_CONTEXT.md
├── 01_ARCHITECTURE.md
├── 02_DECISIONS.md
├── 03_CURRENT_WORK.md
├── 04_HISTORY.md
└── 05_AI_HANDOFF.md
```

Purpose: durable repo context, reduced chat dependency, explicit approval boundaries, and safe AI context recovery.

## 2026-08-20 — Whole-System Architecture Fact Audit

Read-only audit against the current `feat/floor-map` working tree expanded project understanding beyond Kitchen.

It confirmed current payment, refund, Square, BusinessDay, reporting, reservations, scheduling, security, environment, and deployment behavior.

Important current-line risks discovered included:

- production config fail-open risk;
- Square success/local Payment durability window;
- local card refunds do not execute Square refunds;
- incomplete processor identity/reconciliation;
- table-open concurrency;
- reservation overlap uncertainty;
- narrow audit trail;
- failing `test_admin.py` in the inspected working tree.

Evidence limits at that audit time: no production execution, live PostgreSQL, live provider transaction, load benchmark, or real-device/browser verification.

Later Kitchen B2.2 work added a focused real-PostgreSQL concurrency proof; it does not retroactively turn the fact audit into broad PostgreSQL validation.

## 2026-08-20 — Payment Branch Reconciliation

Read-only Git reconciliation confirmed:

```text
fix/p0-security-and-payments
```

contains the payment/security remediation and was never merged into main or `feat/floor-map`.

No revert/removal explained its absence; the cause is branch divergence.

No single branch currently contains both work streams.

Documentation governance conclusion:

- Stage 1/2a/2b = APPROVED BUT UNMERGED;
- Stage 2c = WIP / AUTHORIZED-NOT-CLOSED;
- Stage 1/2a/2b are neither current runtime nor abandoned history.

## 2026-08-20 — Six Persistent Docs Consolidated

The six docs were reconciled to represent current Floor/Kitchen implementation, approved Kitchen baseline, approved-but-unmerged Payment/Security architecture, Stage 2c WIP boundary, current risks, evidence limits, and whole-system architecture.

## 2026-08-22 — Kitchen Documentation Closure

After the B2.2 evidence was captured, canonical docs and MOCs were reconciled so future agents do not depend on chat history for B2 status, evidence, or authorization. (B2.2 final approval remains pending re-audit.)

The next governance milestone is independent-auditor handoff preparation.

## 2026-08-26 — Kitchen Release A integrated into local `main`

Kitchen Stage A / B1 / B2 / B2.2 was reconstructed from `feat/floor-map` onto the
`045dfd5` base — not cherry-picked — through seven enumerated cut points that
excluded ~40 unapproved Floor UI commits, two unauthorized Reservations commits,
and all uncommitted work. Integrated into local `main` by fast-forward, both
commits preserved without squash:

```text
97bed73  feat(kitchen): Release A — Stage A/B1/B2/B2.2 reconstructed onto 045dfd5
7225c6e  fix(kitchen): take the Order fire lock on the scalar id and decide post-lock
```

Each was independently reviewed (`APPROVED WITH NON-BLOCKING NOTES`); the fire fix
was reproduced by the reviewer in a separate PostgreSQL database.

`7225c6e` records a defect class worth remembering: both fire routes locked the
Order *entity*, whose eager relationships become outer joins, and PostgreSQL
refuses `FOR UPDATE` on the nullable side of one — so both routes returned HTTP 500
on the engine production runs while passing all eleven SQLite suites. SQLite treats
`FOR UPDATE` as a no-op, so no SQLite test can detect this by construction. It was
found only by the T3.5 UI smoke against PostgreSQL. The routes now lock the scalar
id and revalidate existence, ownership and the PENDING precondition after the lock.

First live-PostgreSQL execution of the B1 catalog-introspection path, closing the
largest evidence gap carried since B1 closure. Also closed the smoke item open
since the `045dfd5` hotfix: login → Home/Pedidos with no 500.

State at this entry: `origin/main` unchanged at `045dfd5`; nothing pushed;
production not updated. Verified production backup and confirmation of the
deployed SHA remain outstanding and block push/deploy.

## Deferred / Future Work

Kitchen: B3/B4, `OrderItem.station_id` cleanup, task SERVED/reporting, remake/re-fire, richer task states, WebSocket/SSE.

Payment/security: controlled integration of approved Stage 1/2a/2b; Stage 2c completion/review.

Other backlog: normalized station-name uniqueness, duplicate import IDs, CSV formula injection, table concurrency, reservation overlap, broader audit trail, PostgreSQL verification where specifically needed, backup/runbook maturity, responsive/device verification.

## History Maintenance Rules

Add entries for approvals, meaningful architecture transitions, major audits, migration milestones, branch/integration milestones, explicit supersession, or deployment-model changes.

History never authorizes work.
