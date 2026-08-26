# System Architecture — Map of Content

> [!important]
> This MOC is a navigation and impact-analysis index, not a source of truth.
> Structural truth belongs in [[docs/01_ARCHITECTURE]].
> Current authorization belongs in [[docs/03_CURRENT_WORK]].
> Durable decisions belong in [[docs/02_DECISIONS]].

## Canonical project context
- [[docs/00_PROJECT_CONTEXT]]
- [[docs/01_ARCHITECTURE]]
- [[docs/02_DECISIONS]]
- [[docs/03_CURRENT_WORK]]
- [[docs/04_HISTORY]]
- [[docs/05_AI_HANDOFF]]

## Repository / AI rules
- [[AGENTS]]
- [[CLAUDE]]
- [[AI_SESSION_SYNC_PROMPT]]

## Evidence foundations
- [[PROJECT_ARCHITECTURE_FACTS_FOR_DOCS]]
- [[PAYMENT_BRANCH_RECONCILIATION]]

The fact audit is evidence for current structural/runtime observations.
The payment reconciliation is evidence for branch containment/divergence.
Neither artifact by itself authorizes implementation.

## Domain maps
- [[docs/moc/MOC_KITCHEN]]
- [[docs/moc/MOC_PAYMENTS]]
- [[docs/moc/MOC_AUDITS]]

Add another domain MOC only when the domain has enough architecture/evidence to justify one.

## Current architecture themes
Use [[docs/01_ARCHITECTURE]] for details. High-value structural areas include:
- FastAPI + SQLAlchemy 2.0 + Jinja2 + vanilla JS
- SQLite dev/test vs PostgreSQL production
- custom additive migration/bootstrap logic
- polling KDS
- Order / OrderItem / PreparationTask lifecycle
- Floor/Tables and Reservations
- Payment / PaymentAllocation / Refund
- Square Terminal integration on the current Floor/Kitchen line
- Staff/Auth/Permissions
- BusinessDay/closeout
- OLTP → star-schema ETL reporting
- scheduling / happy hour / day menu / upsell

## Cross-domain impact checklist
Before changing architecture, inspect whether the change affects:
- authority/source of truth
- Order / OrderItem lifecycle
- Kitchen / PreparationTask
- payment eligibility or settlement
- Floor/Table occupancy
- Reservations
- reporting/ETL
- permissions/security
- startup/configuration
- migration/backfill
- concurrency/idempotency
- time/BusinessDay semantics
- external-provider behavior

## Evidence boundaries
Keep these distinctions explicit:
- current code != approved target architecture
- committed HEAD != dirty working-tree reality
- branch absence != supersession
- SQLite passing != PostgreSQL proof
- sequential success != concurrency proof
- test exists != test executed and passed
- CSS/template inspection != real-device/browser validation
- local payment/refund record != processor truth

## Maintenance use
When a structural change is approved:
1. update [[docs/01_ARCHITECTURE]]
2. update [[docs/03_CURRENT_WORK]]
3. add/update ADR only when a durable decision changed
4. update [[docs/04_HISTORY]] for meaningful milestones
5. add the review/evidence artifact to [[docs/moc/MOC_AUDITS]]
6. update the relevant domain MOC

Avoid duplicating the full architecture in MOCs.

## Integration state

Kitchen Release A is integrated into local `main` at `7225c6e`. Not pushed,
not deployed; `origin/main` remains `045dfd5`. See [[docs/03_CURRENT_WORK]].
