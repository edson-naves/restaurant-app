# Restaurant App — Architecture Decisions

## Purpose

Record durable approved architecture/product decisions.

Do not convert audit findings, risks, WIP design, or proposals into Accepted ADRs without approval.

## Status

Allowed statuses:

```text
Accepted
Superseded
Deprecated
Proposed
```

## ADR-001 — Repository Documentation Is Persistent Project Context
**Status:** Accepted

Repository docs under `docs/` are persistent project context. Chat history is not the sole source of truth.

## ADR-002 — READY Is Not SERVED
**Status:** Accepted

Kitchen lifecycle remains:

```text
pending → preparing → ready → served
```

READY is preparation readiness. SERVED is a distinct service action.

## ADR-003 — Kitchen Routing Is Snapshotted at Fire
**Status:** Accepted

Later routing configuration changes must not move already-fired work.

## ADR-004 — Unassigned Kitchen Work Is Explicit
**Status:** Accepted

Unassigned work stays visible and operable. Do not hide or heuristically reassign it.

## ADR-005 — PreparationTask Is the Multi-Station Preparation Model
**Status:** Accepted

`OrderItem` remains the sale line; `PreparationTask` represents station work.

## ADR-006 — Retain `OrderItem.station_id` During Kitchen Migration
**Status:** Accepted

Removal requires a later explicit approved migration.

## ADR-007 — Retain `OrderItem.kitchen_status` as Compatibility/Rollup State
**Status:** Accepted

PreparationTask state rolls up where applicable. PreparationTask does not own SERVED.

## ADR-008 — Kitchen Changes Must Not Redesign the Payment Gate
**Status:** Accepted

Payment eligibility/service semantics remain unchanged unless separately authorized.

## ADR-009 — Architecture Changes Are Delivered in Audited Slices
**Status:** Accepted

```text
DESIGN → IMPLEMENT → HANDOFF → AUDIT → FIX → APPROVAL → UPDATE DOCS
```

## ADR-010 — Respect SQLite Development and PostgreSQL Production Differences
**Status:** Accepted

SQLite success is not PostgreSQL proof.

## ADR-011 — KDS Uses Polling
**Status:** Accepted

WebSocket/SSE remains deferred.

## ADR-012 — KDS Filtering Occurs Before Board Limits
**Status:** Accepted

Filtering must happen before LIMIT.

## ADR-013 — Kitchen Routing Is Explicit, Not Heuristic
**Status:** Accepted

Do not infer stations from names/categories/text heuristics.

## ADR-014 — Station Deactivation Preserves Historical/Fired Work
**Status:** Accepted

Deactivation must not destroy historical routing.

## ADR-015 — Inactive Stations With Live Work Remain Reachable
**Status:** Accepted

Live work must remain accessible.

## ADR-016 — Current Product Is Single-Tenant / Single-Location
**Status:** Accepted

Do not add multi-tenancy speculatively.

## ADR-017 — Persist Financial Money in Integer Minor Units
**Status:** Accepted

Persisted money uses integer minor units (cents). This does not approve every current percentage-calculation implementation detail.

## ADR-018 — Payment/Security Stages 1, 2a and 2b Remain Approved Target Architecture
**Status:** Accepted — **classification superseded by ADR-029** (Stage 1/2a/2b are
deployed; the `APPROVED BUT UNMERGED` classification below is historical)

Git reconciliation confirmed Stage 1/2a/2b exist on `fix/p0-security-and-payments` and were never merged into main or `feat/floor-map`.

Classification:

```text
Stage 1  = APPROVED BUT UNMERGED
Stage 2a = APPROVED BUT UNMERGED
Stage 2b = APPROVED BUT UNMERGED
```

Their absence from the current line is branch divergence, not rejection.

This ADR does not authorize integration.

## ADR-019 — Durable External Payment Intent/Evidence Is Required in the Approved Target
**Status:** Accepted — **merge status superseded by ADR-029**

Approved Stage 2a requires durable attempt/evidence around external financial operations and explicit ambiguous-state handling.

This is approved but unmerged.

## ADR-020 — Refund Attempt Lifecycle Is Separate From Charge Attempt Lifecycle
**Status:** Accepted

Approved Stage 2a remediation uses an independent durable refund-attempt lifecycle.

Current `feat/floor-map` still uses local Refund rows without provider refund lifecycle.

## ADR-021 — Approved Target Uses an Explicit Payment Provider Boundary
**Status:** Accepted — **merge status superseded by ADR-029**

Stage 2b established a provider abstraction. Unknown/unsupported provider behavior must fail explicitly.

This is approved but unmerged.

## ADR-022 — Approved Production Configuration Direction Is Fail-Closed
**Status:** Accepted — **merge status superseded by ADR-029**

Stage 1 established the approved direction that critical production configuration fails closed and readiness/liveness are distinct.

The current Floor/Kitchen line still contains fail-open defaults; do not describe target behavior as current enforcement.

## ADR-023 — External Financial Success Requires Durable Provider Evidence in the Approved Target
**Status:** Accepted — **merge status superseded by ADR-029**

External payment/refund success must be tied to durable processor identity/evidence; ambiguous outcomes must be recoverable/reconcilable; amount/currency/provider identity are part of the integrity boundary.

Approved but unmerged.

## ADR-024 — Payment Stage 2c Is Not an Approved Baseline
**Status:** Accepted — **authorization state superseded by ADR-029**

Stage 2c remains WIP / AUTHORIZED-NOT-CLOSED.

Do not treat the payment branch tip as one fully approved block.

The `AUTHORIZED-NOT-CLOSED` wording above is preserved as the decision recorded at
the time. It is **no longer the current authorization state** — see ADR-029. The
core holding of this ADR, that Stage 2c is not an approved baseline, still stands.

## ADR-025 — Reporting Uses OLTP-to-Star-Schema ETL
**Status:** Accepted

Reporting derives from transactional OLTP data through ETL into a star schema.

## ADR-026 — Current Time Model Uses Restaurant-Local Time and Calendar-Date DayClose
**Status:** Accepted

Current behavior uses restaurant-local time, observed default `America/Vancouver`, and local calendar-date DayClose.

Changing the time model requires explicit design.

## ADR-027 — Task-Mode KDS Uses PreparationTask Authority With a Hard Activation Gate
**Status:** Accepted

For task-backed fired work in task-mode KDS:

- `PreparationTask.station_id` is the station-routing authority;
- PreparationTask owns preparation state;
- `OrderItem.kitchen_status` is derived/rollup compatibility state;
- one `OrderItem` remains one sale line;
- task/legacy station-routing authority must never be mixed on one board.

Activation requires all of:

```text
B2_2_ACTIVE
AND kitchen_b2_active
AND fired_items_without_tasks(db) == 0
```

The operational flag is manual. A blocked activation serves the complete Stage A board, not a hybrid board.

Task READY mutations synchronize on the parent Order, then refresh/reselect task/sibling state after acquiring the lock before transition and rollup.

Task READY supports only:

```text
PREPARING → READY
READY → READY  (idempotent)
```

PreparationTask still does not own SERVED. Payment-gate semantics remain unchanged.

## ADR-028 — AI Development Uses Explicit Module Ownership and Cross-Review
**Status:** Accepted

Claude and Codex may both act as developers on separately authorized modules/slices.

Each implementation slice has exactly one primary implementer and an independent
reviewer. The primary implementer may not independently approve the same slice.

Default cross-review:

```text
Claude implements → Codex reviews
Codex implements  → Claude reviews
```

Current ownership and authorization are recorded in `docs/03_CURRENT_WORK.md`.

Module ownership does not authorize cross-domain or shared-core semantic changes. Such
changes require explicit scope expansion or separate design authorization.

Parallel development should use isolated branches/worktrees whenever practical.

Integration remains a separate explicitly authorized action.

A separate governance role may verify policy compliance, and CI/rulesets may enforce
deterministic rules, but neither may invent architecture or policy exceptions.

Final authority for policy exceptions, unresolved cross-agent conflicts, and
cross-domain scope expansion remains with the repository owner.

## ADR-029 — Release B Is Deployed; Stage 2c Requires New Authorization and Independent Review
**Status:** Accepted

Supersedes the status/authorization state of ADR-018, ADR-019, ADR-021, ADR-022,
ADR-023 and ADR-024. Those ADRs are preserved unchanged as the record of what was
decided at the time; only the state below is current.

**Stage 1/2a/2b are DEPLOYED**, not approved-but-unmerged. They shipped as
Release B at `489f6d2`. They are **dormant**: the services ship in production and
no live router invokes them, so deployed must not be read as active runtime. The
substantive holdings of ADR-019, ADR-021, ADR-022 and ADR-023 — durable processor
evidence, the explicit provider boundary, fail-closed production configuration,
and the amount/currency/provider integrity boundary — remain Accepted and are now
deployed rather than pending.

**Stage 2c is WIP / NOT AUTHORIZED / NOT REVIEWED / NOT DEPLOYED.** The
`AUTHORIZED-NOT-CLOSED` classification in ADR-024 no longer describes it. Before
any Stage 2c implementation, integration or deployment, all of the following are
required:

```text
explicit written authorization for the specific slice
independent review by a reviewer who is not the implementer
the evidence that slice's gates require
```

The existence of `release/stage2c-charge`, of a design artifact, or of a
self-asserted approval inside any artifact does not satisfy any of the three.
Branch existence is never approval, and a document cannot evidence its own
independent review.

ADR-024's core holding stands: Stage 2c is not an approved baseline, and the
payment branch tip must never be treated as one fully approved block.

## Explicitly Not Accepted Yet

Do not infer approval for:

- a specific Payment/Security branch-integration implementation plan;
- Kitchen B3/B4;
- table-open concurrency repair;
- reservation-overlap design;
- generalized financial/config audit-log redesign;
- WebSocket/SSE;
- multi-tenancy;
- a specific backup platform;
- a responsive/mobile support guarantee;
- removal of `OrderItem.station_id`;
- task-owned SERVED/reporting;
- remake/re-fire model;
- Payment Stage 2c WIP.

## ADR Maintenance Rules

When an Accepted ADR changes, explicitly supersede it rather than silently rewriting history.

Implementation divergence from an Accepted ADR is not itself a new decision.
