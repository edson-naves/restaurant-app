# Audits & Reviews — Map of Content

> [!important]
> This MOC indexes evidence and review chains.
> It does not grant authorization or approval.
>
> Current authorization: [[docs/03_CURRENT_WORK]]
> Durable decisions: [[docs/02_DECISIONS]]
> Architecture baseline: [[docs/01_ARCHITECTURE]]
> Operating protocol: [[docs/05_AI_HANDOFF]]

## Audit operating rules
- [[AGENTS]]
- [[docs/05_AI_HANDOFF]]

Core rule:
**developer handoff is evidence, not approval.**

The independent auditor verifies claims against:
- approved design
- actual code/diff
- tests and execution evidence
- current architecture
- current authorization/scope
- relevant Git/branch state

## Architecture / factual audits
- [[PROJECT_ARCHITECTURE_FACTS_FOR_DOCS]]
- [[PAYMENT_BRANCH_RECONCILIATION]]

Use these to distinguish:
- current runtime facts
- branch facts
- approval facts
- evidence gaps

## Kitchen — design reviews

Approved final B2 design:
- [[KITCHEN_STATIONS_STAGE_B2_v3_DESIGN]]

Historical progression:
- B2 v1 — DESIGN REVISION REQUIRED
- B2 v2 — DESIGN REVISION REQUIRED
- B2 v3 design — APPROVED WITH NON-BLOCKING NOTES; implemented through B2.1/B2.2 (checkpoint `f4dd079` re-audited: APPROVED WITH NON-BLOCKING NOTES → B2.2 APPROVED / CLOSED)

## Kitchen — implementation reviews

### B2.1
Primary evidence:
- [[KITCHEN_STATIONS_STAGE_B2_1_REVIEW_PACKAGE]]

Fix evidence:
- [[KITCHEN_STATIONS_STAGE_B2_1_FIX_REVIEW]]

Result:
**APPROVED WITH NON-BLOCKING NOTES**

### B2.2
Primary evidence:
- [[KITCHEN_STATIONS_STAGE_B2_2_REVIEW_PACKAGE]]

Fix evidence:
- [[KITCHEN_STATIONS_STAGE_B2_2_FIX_REVIEW]]
- [[KITCHEN_STATIONS_STAGE_B2_2_FIX_REVIEW_2]]

PostgreSQL evidence:
- [[KITCHEN_STATIONS_STAGE_B2_2_POSTGRES_CONCURRENCY_EVIDENCE]]

Final result:
**APPROVED / CLOSED** — committed checkpoint `f4dd079` independently re-audited
(APPROVED WITH NON-BLOCKING NOTES); the approval reused the four already-executed
Kitchen tests and the PostgreSQL concurrency evidence.

The PostgreSQL proof confirmed:
- real parent-Order `FOR UPDATE` contention
- B waited on a lock while A held the Order
- post-lock B observed A's committed READY state
- final sibling tasks READY
- OrderItem READY
- Order not SERVED
- no lost update/stale rollup

## Payment / security review evidence
Branch-level reconciliation:
- [[PAYMENT_BRANCH_RECONCILIATION]]

Approved status of Stage 1/2a/2b is documentary approval, not inferred from Git.
Stage 2c remains WIP / authorized-not-closed.

If payment review artifacts are later consolidated into stable filenames, link them here rather than duplicating their content.

## Review-result vocabulary
Use explicit verdicts:
- `APPROVED`
- `APPROVED WITH NON-BLOCKING NOTES`
- `FIX REQUIRED`
- `DESIGN REVISION REQUIRED`

Keep these distinct from architectural/status vocabulary such as:
- `CURRENT`
- `APPROVED BUT UNMERGED`
- `ACTIVE DESIGN`
- `RISK`
- `DEFERRED`
- `HISTORICAL / SUPERSEDED`

## Evidence discipline
Always distinguish:
- `TEST EXISTS`
- `TEST EXECUTED AND PASSED`
- environment used
- implementation defect
- evidence gap
- pre-existing unrelated failure

Never overclaim:
- SQLite as PostgreSQL concurrency proof
- sequential tests as locking proof
- mocked provider behavior as live-provider evidence
- inspection as runtime execution
- developer-generated review packages as independently verified truth

## Token-efficient audit workflow
The repository is the context source.

Developer prompts should remain concise:
- authorized slice
- approved design
- critical invariants
- out-of-scope boundaries
- concise handoff

The independent auditor owns:
- broader coverage review
- missing edge-case/evidence checks
- validation of developer claims
- final verdict

Do not force the developer to reproduce exhaustive history or broad audit checklists already stored in the repo unless required to close a blocker.

## After an approval
After a slice is independently approved:
- update [[docs/03_CURRENT_WORK]]
- update [[docs/04_HISTORY]] when milestone-worthy
- update [[docs/01_ARCHITECTURE]] when structure/current runtime changed
- update [[docs/02_DECISIONS]] only for a genuinely new durable decision
- link final evidence here
- update the relevant domain MOC
