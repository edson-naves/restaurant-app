# Restaurant App — AI Agent Entry Point

This is the canonical repository-wide instruction file for AI coding/review agents.

Do not duplicate project rules in other agent-specific files.

Before doing any design, coding, refactoring, migration, testing, Git operation, documentation change, database/schema/data change, architecture review, or deployment-related work, read the persistent project documentation first.

This file does not authorize work by itself.

For implementation prompts and inter-agent handoffs, follow the **Token-Efficient Agent Workflow** in `docs/05_AI_HANDOFF.md`. Do not duplicate repository context unnecessarily.

---

## Token-Efficient Default

Follow the Token-Efficient Agent Workflow in `docs/05_AI_HANDOFF.md`.

By default:

- do not generate full review packages unless explicitly requested or required as durable evidence;
- use concise implementation/fix handoffs;
- do not embed entire source/test files unnecessarily;
- run focused tests during iteration;
- run the relevant regression suite once before final approval/handoff;
- run a full system suite only when the scope/risk justifies it;
- reuse valid existing evidence instead of regenerating it;
- never trade evidence quality for token savings.

Independent auditors must not implement fixes during an audit unless separately authorized; use the verdict vocabulary defined in `docs/05_AI_HANDOFF.md`.

## Mandatory Read Order

Read these files in this order:

1. `docs/00_PROJECT_CONTEXT.md`
2. `docs/02_DECISIONS.md`
3. `docs/03_CURRENT_WORK.md`
4. `docs/01_ARCHITECTURE.md`
5. `docs/05_AI_HANDOFF.md`

Read `docs/04_HISTORY.md` only when historical context is needed.

After reading the docs, inspect only the code, migrations, tests, Git state, MOCs, and design/review artifacts relevant to the authorized task.

Do not begin by loading every historical handoff or old chat artifact.

---

## Current Authorization

`docs/03_CURRENT_WORK.md` is the authoritative execution checkpoint.

Do not infer authorization from existing code, branch names, uncommitted files, design documents, historical implementations, Git commit subjects, review artifacts, MOCs, or chat history.

If only documentation/handoff work is authorized, do not implement a feature.

If only one slice is authorized, do not implement adjacent slices.

Authorization for design does not imply implementation.

Authorization for implementation does not imply merge, deployment, commit, push, or production mutation.

---

## Multi-Agent Development

Claude and Codex may both act as developers, but only on explicitly authorized
modules/slices recorded in `docs/03_CURRENT_WORK.md`.

For every implementation slice:

- exactly one agent is the primary implementer;
- the other may act as the independent reviewer;
- the implementer may not independently approve its own slice;
- developer handoff is evidence, not approval;
- module ownership does not imply permission to change shared-core behavior.

Default cross-review:

```text
Claude implements → Codex reviews
Codex implements  → Claude reviews
```

Parallel implementation should use separate branches/worktrees whenever practical.

**Developer Worktree Bootstrap (required, Claude and Codex alike):** before starting any
authorized implementation slice, bootstrap it into an **isolated branch/worktree** from
the exact authorized base checkpoint — never implement in an unrelated dirty working
tree, and never stash / clean / reset / discard / move / overwrite existing work. If
isolation cannot be created safely, STOP and request a decision. Full required procedure
is in `docs/05_AI_HANDOFF.md` → "Developer Worktree Bootstrap".

**Approved-slice commit ownership (Claude and Codex alike):** after an independent
review returns `APPROVED` or `APPROVED WITH NON-BLOCKING NOTES`, the **primary
implementer** commits the approved slice — the reviewer does not commit the
implementer's work. The commit must contain **only** the approved scope/files (verify
the staged diff first). Commit authorization does **not** imply merge, push, rebase,
cherry-pick, integration, deployment, or any unrelated change. On `FIX REQUIRED` /
`DESIGN REVISION REQUIRED`, do not create the final commit — the implementer fixes and
re-review happens first. Full procedure: `docs/05_AI_HANDOFF.md` → "Approved Slice
Commit Ownership".

If a slice requires a shared-core or cross-domain change outside its authorized
boundary, stop and request explicit scope expansion or design authorization.

Detailed ownership, review, branch-isolation, shared-file, governance, and conflict
rules live in `docs/05_AI_HANDOFF.md`. `docs/03_CURRENT_WORK.md` remains the
authoritative source for current ownership and authorization.

### Governance Boundary

A separate governance agent may verify compliance with repository policy when
explicitly assigned. The governance agent:

- does not develop the slice it governs;
- does not silently fix violations;
- does not invent architecture or policy exceptions;
- reports compliance gaps and escalates unresolved exceptions.

Governance agent = NOT YET ASSIGNED. GitHub/CI governance enforcement = NOT YET
CONFIGURED. Human repository ownership remains the final authority for policy changes,
architecture exceptions, unresolved Claude↔Codex conflicts, cross-domain scope
expansion, and exceptional merge/deployment decisions. GitHub Actions / CI / rulesets
may enforce deterministic rules but do not replace independent technical review or
human authority.

---

## Status Vocabulary

Always preserve the distinction between:

- CURRENT IMPLEMENTATION
- APPROVED BASELINE
- APPROVED BUT UNMERGED
- ACTIVE DESIGN
- CURRENT RISK
- DEFERRED / NOT APPROVED
- HISTORICAL / SUPERSEDED

Do not collapse these states.

Important current status:

- Kitchen Stage A — APPROVED / CLOSED
- Kitchen Stage B1 — APPROVED / CLOSED
- Kitchen Stage B2 — APPROVED / CLOSED (v3 design: APPROVED WITH NON-BLOCKING NOTES)
- Kitchen B2.1 — APPROVED
- Kitchen B2.2 — APPROVED / CLOSED
- Kitchen B3/B4 — DEFERRED / NOT AUTHORIZED
- Kitchen Release A — INTEGRATED INTO LOCAL `main` (`7225c6e`), NOT PUSHED, NOT DEPLOYED
- Payment/Security Stage 1 — APPROVED BUT UNMERGED
- Payment/Security Stage 2a — APPROVED BUT UNMERGED
- Payment/Security Stage 2b — APPROVED BUT UNMERGED
- Payment Stage 2c — WIP / AUTHORIZED-NOT-CLOSED

Do not describe approved-but-unmerged architecture as current runtime behavior.

Do not describe Kitchen Release A as production behavior either: it is integrated
into local `main` only. `origin/main` is still `045dfd5` and production has not
been updated. Pushing to `main` triggers Render auto-deploy, so push and deploy
are one decision, gated on a verified production backup and confirmation of the
deployed SHA.

Do not interpret branch divergence as architectural rejection or supersession.

---

## Source-of-Truth Rules

For CURRENT IMPLEMENTATION, inspect:

```text
current code
+ migrations
+ tests
+ current working tree
```

For BRANCH FACTS, use:

```text
Git history
+ branch containment
+ merge-base/divergence evidence
```

For APPROVAL STATUS, use explicit review / approval artifacts.

For DURABLE APPROVED DECISIONS, use `docs/02_DECISIONS.md`.

For CURRENT AUTHORIZATION, use `docs/03_CURRENT_WORK.md`.

For CURRENT STRUCTURAL ARCHITECTURE, use `docs/01_ARCHITECTURE.md`.

MOCs are navigation aids, not authority.

Chat memory is never sufficient authority by itself.

---

## Git Safety

Before risky work, inspect:

```text
git status
git branch --show-current
```

Never automatically merge, cherry-pick, rebase, reset, clean, discard unrelated work, switch branches for integration, resolve integration conflicts, commit, push, or rewrite Git history.

The current Floor/Kitchen line and Payment/Security remediation line are divergent.

Do not merge `fix/p0-security-and-payments` wholesale because its tip includes Payment Stage 2c WIP.

Any future Payment/Security ↔ Floor/Kitchen integration must be its own explicitly authorized design/review/audit slice.

---

## Kitchen Safety

Preserve unless explicitly superseded:

```text
READY != SERVED
```

Also preserve:

- explicit station routing;
- no heuristic auto-routing;
- snapshot routing at fire;
- fired routing immutability;
- explicit Unassigned work;
- coursing semantics;
- existing payment-gate semantics;
- inactive station live-work reachability;
- filter-before-limit;
- one OrderItem remains one sale line;
- multiple PreparationTasks do not duplicate sale quantity;
- `OrderItem.station_id` remains during the current migration;
- `OrderItem.kitchen_status` remains compatibility/rollup state;
- PreparationTask does not own SERVED;
- no hybrid task/legacy KDS authority.

## Kitchen B2 Boundary

B2 v3 design, B2.1 and B2.2 are approved. The Kitchen B2 checkpoint was independently
re-audited at `f4dd079` — **APPROVED WITH NON-BLOCKING NOTES** — so Kitchen Stage B2 and
B2.2 are APPROVED / CLOSED. B3/B4 remain DEFERRED / NOT AUTHORIZED; do not reopen B2 or
start new implementation without a new authorized slice.

Current B2 invariants:

- task-mode station authority = snapshotted `PreparationTask.station_id`;
- task-backed prep-state authority = PreparationTask;
- one OrderItem remains one sale line;
- task READY supports `PREPARING → READY` and idempotent `READY → READY`;
- unsupported task state rejects;
- parent Order is the synchronization lock;
- refresh/reselect task/sibling state after lock acquisition;
- task-backed legacy writes validate all targets before mutation and route through tasks;
- coarse PREPARING clears readiness at all active stations and is explicitly destructive;
- tasks do not own SERVED;
- payment-gate semantics remain unchanged.

Activation requires:

```text
B2_2_ACTIVE
AND
kitchen_b2_active
AND
fired_items_without_tasks(db) == 0
```

A blocked activation serves the complete Stage A board, never a hybrid.

A real PostgreSQL two-session proof validated the parent-Order lock/post-lock refresh contract.

Do not start B3/B4, task SERVED/reporting, remake/re-fire, or compatibility-field cleanup without a new explicit authorization.

---

## Payment / Financial Safety

Payments and refunds are high-risk domains.

Before modifying financial behavior, inspect current Payment/PaymentAllocation behavior, Refund behavior, direct Square flow, approved-but-unmerged Stage 1/2a/2b architecture, transaction boundaries, provider side effects, idempotency, durable processor evidence, amount/currency checks, refund semantics, permissions, reporting/ETL, migrations, and concurrency.

Never assume:

```text
local Payment == processor truth
local Refund == processor refund
```

On the current Floor/Kitchen line:

- Square may report success before durable local Payment persistence;
- a local card Refund does not automatically execute a Square refund;
- full processor identity/evidence is not persisted on this line;
- durable PaymentAttempt / RefundAttempt / provider abstraction lives on the separate approved-but-unmerged remediation line.

Do not silently implement an ad hoc payment fix that conflicts with approved remediation architecture.

Do not modify payment/refund semantics incidentally during unrelated work.

---

## Database / Migration / Concurrency Safety

SQLite success does not prove PostgreSQL behavior.

Before schema changes, inspect both SQLite and PostgreSQL implications, current migration/bootstrap behavior, required invariants, backfill strategy, failure behavior, concurrency behavior, rollback/compatibility implications, and appropriate tests.

Sequential success is not concurrency proof.

For state-changing workflows inspect transaction boundaries, row locks, stale ORM state, unique constraints, compare-and-set/state guards, duplicate requests, multi-worker behavior, and multi-terminal behavior.

When a parent Order lock is the synchronization primitive, refresh/reselect state used for decisions after acquiring the lock.

---

## Production / Security Safety

Treat current production fail-open defaults as CURRENT RISK, not desired architecture.

Approved Payment/Security Stage 1 establishes the fail-closed target direction but remains unmerged.

Never expose secrets in code, documentation, logs, tests, screenshots, or handoffs.

Do not change production secrets, environment variables, deployment configuration, external service settings, production data, or production schema without explicit authorization.

Backend authorization is authoritative.

Do not weaken PIN hashing or permission enforcement.

---

## Testing / Evidence Rules

Always distinguish:

```text
TEST EXISTS
```

from:

```text
TEST EXECUTED AND PASSED
```

Do not overstate SQLite as PostgreSQL proof, local tests as production proof, mocked provider tests as live-provider proof, sequential tests as concurrency proof, or CSS inspection as device/browser verification.

Current evidence boundaries include:

- no production execution;
- no production data mutation;
- a real PostgreSQL concurrency proof exists specifically for Kitchen B2.2;
- no live provider execution during the architecture fact audit;
- no load benchmark;
- no real-device/browser validation.

At documentation consolidation time, `test_admin.py` was failing in the inspected working tree while backend authorization assertions still passed.

Do not silently fix unrelated failures during another slice.

---

## Documentation Update Rules

After an approved implementation/audit cycle:

- update `docs/01_ARCHITECTURE.md` if structural architecture changed;
- update `docs/02_DECISIONS.md` only for durable approved/superseded decisions;
- update `docs/03_CURRENT_WORK.md` when authorization/checkpoint changes;
- update `docs/04_HISTORY.md` for meaningful milestones;
- update `docs/00_PROJECT_CONTEXT.md` only for stable project-wide changes;
- update `docs/05_AI_HANDOFF.md` only when operating protocol changes;
- update relevant MOCs for navigation/evidence;
- update this `AGENTS.md` when repository-wide AI entry protocol/current global status changes.

Do not duplicate the same detailed content across agent-specific files.

---

## Standard Workflow

```text
recover context
→ inspect current evidence
→ DESIGN
→ explicit authorization
→ IMPLEMENT
→ HANDOFF
→ independent AUDIT
→ FIX if required
→ APPROVAL
→ UPDATE DOCS
→ NEXT SLICE
```

For branch integration:

```text
reconcile branches
→ define approved integration boundary
→ design integration plan
→ explicit authorization
→ integrate
→ migration/test audit
→ approval
→ update docs
```

---

## Final Self-Check Before Changing Code

Confirm:

```text
I know the current branch and working-tree state.
I read docs/03_CURRENT_WORK.md.
I know the authorized activity.
I know whether the architecture is current, approved, approved-but-unmerged, WIP, or deferred.
I know the applicable invariants.
I checked cross-domain consumers.
I checked database/migration implications.
I checked concurrency/idempotency implications.
I checked payment/security implications.
I understand the evidence boundary.
I am not expanding scope.
```

If any answer is unclear in a high-risk domain, stop and request a decision.

---

## Final Rule

Do not optimize for speed by weakening evidence, authorization boundaries, financial correctness, data integrity, migration safety, concurrency safety, security, or auditability.

When code, Git history, approval artifacts, and documentation disagree, preserve the distinction and resolve it explicitly rather than inventing a cleaner story.
