# Payments & Security — Map of Content

> [!important]
> This MOC is an index, not a source of truth.
> - Current authorization/checkpoint: [[docs/03_CURRENT_WORK]]
> - Current structural architecture: [[docs/01_ARCHITECTURE]]
> - Durable accepted decisions: [[docs/02_DECISIONS]]
> - Git/branch evidence: [[PAYMENT_BRANCH_RECONCILIATION]]
>
> Never equate approved-but-unmerged architecture with current runtime behavior.

## Domain entry points
- [[docs/00_PROJECT_CONTEXT]]
- [[docs/01_ARCHITECTURE]]
- [[docs/02_DECISIONS]]
- [[docs/03_CURRENT_WORK]]
- [[docs/04_HISTORY]]
- [[docs/05_AI_HANDOFF]]

## Branch reconciliation evidence
- [[PAYMENT_BRANCH_RECONCILIATION]]

Key branch facts:
- this release: `release/kitchen-sync` (Kitchen only, base `045dfd5`)
- historical Floor/Kitchen line: `feat/floor-map`
- remediation line: `fix/p0-security-and-payments`
- common ancestor documented as `07ee4f1`
- remediation branch tip documented as `58e0324`
- remediation was never merged into main, `feat/floor-map`, or this release
- absence on the Floor/Kitchen line is branch divergence, not supersession

## Whole-system factual evidence
- [[PROJECT_ARCHITECTURE_FACTS_FOR_DOCS]]

Use this for evidence-supported current-line payment, refund, Square, security, environment, reporting, and risk facts.

## Status

### Stage 1
**APPROVED BUT UNMERGED**

Approved target direction includes fail-closed production configuration and liveness/readiness separation.

### Stage 2a
**APPROVED BUT UNMERGED**

Approved target includes durable payment/refund attempt lifecycle, processor evidence, idempotency, and ambiguous-state handling.

### Stage 2b
**APPROVED BUT UNMERGED**

Approved target includes an explicit payment-provider boundary.

### Stage 2c
**WIP / AUTHORIZED-NOT-CLOSED**

Stage 2c is not part of the approved integration baseline.

## Current Floor/Kitchen payment reality
See [[docs/01_ARCHITECTURE]] for authoritative detail.

Current line includes:
- `Payment`
- `PaymentAllocation`
- integer-cent financial snapshots
- seat-ledger settlement
- direct Square Terminal orchestration
- local `Refund` rows

Current line does not contain the approved Stage 1/2a/2b remediation architecture.

## Known current-line risks
Tracked in canonical architecture/current-work docs and fact audit:
- processor success can precede durable local Payment persistence
- local card Refund does not itself execute a Square refund
- incomplete processor identity/reconciliation
- production-critical configuration can fail open
- audit trail is narrow

## Integration boundary
No integration is currently implied by this MOC.

Do not:
- merge the full remediation branch tip as one approved block
- treat Stage 2c WIP as approved
- casually cherry-pick payment commits into Floor/Kitchen work
- implement ad hoc payment fixes that conflict with the approved target

Future integration must be its own controlled slice:
design/reconciliation → integration plan → authorization → implementation → migration/test audit → approval.

## Relevant durable decisions
See [[docs/02_DECISIONS]], especially the ADRs covering:
- integer minor units
- approved-but-unmerged Stage 1/2a/2b
- durable external payment evidence
- separate refund attempt lifecycle
- provider abstraction
- fail-closed production target
- Stage 2c not being an approved baseline
