# Payments & Security — Map of Content

> [!important]
> This MOC is an index, not a source of truth.
> - Current authorization/checkpoint: [[docs/03_CURRENT_WORK]]
> - Current structural architecture: [[docs/01_ARCHITECTURE]]
> - Durable accepted decisions: [[docs/02_DECISIONS]]
> - Git/branch evidence: [[PAYMENT_BRANCH_RECONCILIATION]] (HISTORICAL — 2026-08)
>
> Never equate approved, or even deployed, architecture with active runtime behavior.

## Domain entry points
- [[docs/00_PROJECT_CONTEXT]]
- [[docs/01_ARCHITECTURE]]
- [[docs/02_DECISIONS]]
- [[docs/03_CURRENT_WORK]]
- [[docs/04_HISTORY]]
- [[docs/05_AI_HANDOFF]]

## Branch reconciliation evidence
- [[PAYMENT_BRANCH_RECONCILIATION]] — **HISTORICAL** (2026-08, pre-Release B)

Current branch and deployment reality lives in [[docs/03_CURRENT_WORK]]. This MOC
does not restate it.

The reconciliation document remains useful for one thing only: the provenance of
the remediation line (`fix/p0-security-and-payments`, common ancestor `07ee4f1`,
tip `58e0324`) and the rule that absence on a line is branch divergence, not
supersession.

## Whole-system factual evidence
- [[PROJECT_ARCHITECTURE_FACTS_FOR_DOCS]]

Use this for evidence-supported current-line payment, refund, Square, security, environment, reporting, and risk facts.

## Status

Stage status lives in [[docs/03_CURRENT_WORK]]. This MOC does not restate it.

## Current payment reality and risks

See [[docs/03_CURRENT_WORK]] and [[docs/01_ARCHITECTURE]].

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
- Stage 1/2a/2b approval status
- durable external payment evidence
- separate refund attempt lifecycle
- provider abstraction
- fail-closed production target
- Stage 2c not being an approved baseline
