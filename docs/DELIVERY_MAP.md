# Restaurant App — Authoritative Delivery Map (Proposed for AgeOps Import)

## Status

**PROPOSAL — DOCUMENTATION / RECONSTRUCTION ONLY.** No product change, no
AgeOps modification, no Framework modification, no governance-state change,
no authorization of dormant or WIP work (Stage 2c stays unauthorized — see
§11). Not committed, not pushed.

Note on scope: I have no visibility into a project called "Nu-Brite" or its
`DELIVERY_MAP.md` — it is not part of this repository, and nothing in this
codebase or its `docs/` references it. I cannot literally match an artifact
I cannot read. What follows matches the rigor this prompt itself specifies
(explicit IDs, historical-vs-derived identity, evidence per entity,
PROVEN/LIKELY/UNKNOWN relationship confidence, an AgeOps-import section) —
flagged in §14 for the owner's own comparison against Nu-Brite's if that
comparison matters.

AgeOps itself — specifically `planning/state.json` — **does not exist
anywhere in this repository** (confirmed by a full-worktree search before
writing this document, same finding as the prior pass). This document is
therefore written as a standalone import proposal, not a diff against an
existing AgeOps state.

Derived from `docs/00_PROJECT_CONTEXT.md`, `01_ARCHITECTURE.md`,
`02_DECISIONS.md` (ADR-001–029), `03_CURRENT_WORK.md`, `04_HISTORY.md`,
`05_AI_HANDOFF.md`, and direct repository inspection (`app/routers/`,
`app/services/`, `app/models/`) at `origin/main` commit
`a84bcaf229435fc19b9ebcf341d99b76f68cdba7`.

**Identity convention used throughout:** `HIST` = the name/label the
project's own docs already use verbatim for that work (a real historical
identity, not invented here). `DERIVED-###` = no formal identifier exists
anywhere in the repo for this grouping; the ID is proposed here purely as a
planning convenience for AgeOps import, not a discovered fact. Every entity
below states which it is. No historical ID is invented where none exists —
per instruction, ambiguous cases get an explicit `DERIVED-###` instead.

---

## 1. Project

**PROJ-001 — Restaurant App** *(DERIVED — the repository has no formal
project code; this is the only project in scope)*

Single-tenant, single-location restaurant POS and operations system: order
entry, kitchen preparation, table/floor service, payment, closeout,
reporting, selected admin workflows (`00_PROJECT_CONTEXT.md`; ADR-016).
FastAPI + SQLAlchemy 2.0 + Jinja2 + vanilla JS; SQLite dev/test, PostgreSQL
production.

Three converged development lines evidenced in the docs and git history:
1. **Kitchen / Stations / PreparationTask** (Stage A → B1 → B2/B2.2) —
   ADR-002–015, ADR-027; developed on `feat/floor-map`, reconstructed onto
   `045dfd5`, integrated to `main`.
2. **Payment / Security hardening** (Stage 1/2a/2b/2c) — ADR-017–024,
   ADR-029; developed on `fix/p0-security-and-payments`, shipped as
   Release B, **deployed but dormant**.
3. **Floor Plan Builder / Reservations UI** — no ADR number, but the single
   largest body of recent, individually-reviewed work in
   `03_CURRENT_WORK.md`, this session's own first-hand record.

Customer/Loyalty: no implementation evidence anywhere in the repository
(confirmed by grep across `app/`, `web/`, `docs/`); not modeled as an Epic.
See §10.

---

## 2. Business Epics

| ID | Name | Identity | Purpose/outcome | Confidence | Sources |
|---|---|---|---|---|---|
| BE-1 | Dine-In Table & Floor Operations | DERIVED | See the room, seat guests, arrange tables/zones visually, run the floor day-to-day | HIGH | `app/routers/sales.py::floor_plan()`, `admin.py` Floor Plan Builder routes, `web/templates/floor.html`/`admin_tables.html`, `app/services/zone_geometry.py`, `03_CURRENT_WORK.md` (majority of file) |
| BE-2 | Reservations & Waitlist | DERIVED | Book reservations, manage walk-in waitlist, pick tables for a booking | HIGH | `app/routers/reservations.py`, `app/services/reservations.py`, `web/templates/reservations.html`, `01_ARCHITECTURE.md` "Main Domains" |
| BE-3 | Order Taking & Table Service | DERIVED (lifecycle itself is HIST, per ADR-002/005) | Create order, add items/modifiers, hold/course, fire, serve, close | MEDIUM (Epic boundary is inferred — logic is embedded in `sales.py`, "no formal repository layer" per `01_ARCHITECTURE.md`) | `01_ARCHITECTURE.md` "Order Lifecycle"; ADR-002, ADR-005 |
| BE-4 | Kitchen Operations | DERIVED (Stage A/B1/B2 themselves are HIST) | Station-scoped KDS boards, Expo aggregation, READY≠SERVED preserved | HIGH | `04_HISTORY.md` Stage A/B1/B2 entries; ADR-002–015, ADR-027 |
| BE-5 | Payments & Checkout (current line) | DERIVED | Take payment today — split/partial/multi-tender, Square Terminal, local refunds | HIGH | `01_ARCHITECTURE.md` "Current Payment Architecture (unchanged by this release)"; `app/routers/pay.py` |
| BE-6 | Takeout / Delivery | DERIVED | Channel-tag orders, run a delivery queue with driver assignment/status | MEDIUM | `Channel`, `DeliveryOrder`/`DeliveryStatus` models; `sales.py::delivery_queue()`; `01_ARCHITECTURE.md` "Delivery is partial" |
| BE-7 | Staff Operations | DERIVED | Staff records, roles, scheduling | MEDIUM | `app/routers/schedule.py`; `01_ARCHITECTURE.md` "Main Domains" |
| BE-8 | Menu & Upsell Management | DERIVED | Menu items, day-menus, happy-hour pricing, upsell prompts | MEDIUM | `app/services/daymenu.py`, `happyhour.py`, `upsell.py`; `00_PROJECT_CONTEXT.md` |

Not proposed: **Customer/Loyalty** — no evidence anywhere (see §10).

---

## 3. Enabler Epics

| ID | Name | Identity | Purpose/outcome | Confidence | Sources |
|---|---|---|---|---|---|
| EE-1 | Kitchen Task/Routing Architecture | DERIVED (the individual ADRs it groups are HIST) | The Station/PreparationTask model, snapshot-at-fire immutability, Unassigned visibility, hard activation gate — the architecture BE-4 is built on | HIGH | ADR-003,004,005,006,007,013,014,015,027; `04_HISTORY.md` Stage A/B1/B2 v1–v3/B2.1/B2.2 |
| EE-2 | Payment & Security Hardening | DERIVED (Stage 1/2a/2b/2c themselves are HIST) | Fail-closed config, durable `PaymentAttempt`/`RefundAttempt`, provider boundary — approved-target direction BE-5 does not yet run on | HIGH | ADR-017–024, ADR-029; `01_ARCHITECTURE.md` "Deployed-but-Dormant..." |
| EE-3 | Authentication & Authorization | DERIVED | Backend-authoritative session/permission mechanism every write route depends on | HIGH (existence) / MEDIUM (Epic framing) | `01_ARCHITECTURE.md` "Authentication / Authorization"; `app/routers/auth.py` |
| EE-4 | Reporting / Data Platform | DERIVED | OLTP → ETL → star schema, feeding closeout/analytics | HIGH (existence) / MEDIUM (decomposition) | ADR-025; `app/models/star.py`, `app/etl.py`, `app/routers/analytics.py` |
| EE-5 | Data Migration & Import | DERIVED | Custom additive (non-Alembic) schema evolution; CSV/XLSX menu-routing import | HIGH | `01_ARCHITECTURE.md` "Startup / Migration Architecture"; ADR-010; Stage A CSV/XLSX import |

Considered and NOT proposed: **Observability/Reliability** — the docs
explicitly disclaim it ("Not established as general current architecture" —
`01_ARCHITECTURE.md`). See §10.

---

## 4. Features

All Features are **DERIVED** identity — the project's own docs describe
Epics/Stages/Slices, never a "Feature" layer by that name; every Feature ID
below is a planning grouping proposed here, grounded in the cited evidence,
not a name found in the repository.

| ID | Parent | Name | Confidence | Sources |
|---|---|---|---|---|
| FT-01 | BE-1 | Free-map table/zone coordinate model | HIGH | `map_x_per_mille`/`map_y_per_mille` columns; `app/migrate.py` |
| FT-02 | BE-1 | Admin Floor Plan Builder (drag/resize, save) | HIGH | `admin_tables.html`; `/admin/tables/map-layout`, `/admin/zones/{id}/move-layout` |
| FT-03 | BE-1 | Operational Floor Map/Arrange | HIGH | `floor.html` Map/Arrange script; "Operational Floor port" section |
| FT-04 | BE-1 | Floor card status/legend presentation | HIGH | `floor.html` `STATUS_ICON`/`STATUS_LABEL`; "Floor card redesign" section |
| FT-05 | BE-1 | Zone-overlap / zone-fallback placement correctness | HIGH | `admin.py::_table_map_positions()`, `place_new_zone_rect()` |
| FT-06 | BE-2 | Reservation booking + walk-in waitlist entry | HIGH | `reservations.py`; `#bookform`/`#wlform` |
| FT-07 | BE-2 | Floor-map table picker for reservations | HIGH | "floor tabs and select-all," "wide-desktop split layout" sections |
| FT-08 | BE-2 | Today-at-a-glance reservation stats | HIGH | `reservations.py` `stats` dict |
| FT-09 | BE-3 | Order/order-item lifecycle | HIGH (lifecycle) / LOW (isolated slice evidence) | `01_ARCHITECTURE.md` "Order Lifecycle" |
| FT-10 | BE-3 | Seat-ledger / guest-count state | MEDIUM | `01_ARCHITECTURE.md` "seat-ledger settlement"; `sales.py` table state |
| FT-11 | BE-4 | Station-filtered KDS boards | HIGH | Stage A; ADR-012/013 |
| FT-12 | BE-4 | Expo aggregation | HIGH | `01_ARCHITECTURE.md` line 138; `StationType` |
| FT-13 | BE-4 | PreparationTask multi-station fire + rollup | HIGH | Stage B1; ADR-005/006/007 |
| FT-14 | BE-4 | Task-mode KDS reads + per-task READY + activation gate | HIGH | Stage B2/B2.1/B2.2; ADR-027; PG concurrency evidence |
| FT-15 | BE-4 | CSV/XLSX menu-routing import | HIGH | Stage A audit fixes |
| FT-16 | BE-5 | Square Terminal card processing | HIGH | `app/services/square.py` |
| FT-17 | BE-5 | Split/partial/multi-tender settlement | HIGH | "Current Payment Architecture" characteristics |
| FT-18 | BE-5 | Local refund recording | HIGH | `app/services/refunds.py` |
| FT-19 | BE-6 | Order channel tagging | HIGH | `Channel` model |
| FT-20 | BE-6 | Delivery queue + status + driver assignment | HIGH | `sales.py::delivery_queue()`, `DeliveryOrder`/`DeliveryStatus` |
| FT-21 | BE-6 | Third-party delivery integration | LOW | `Channel.is_third_party` field only, no integration code found |
| FT-22 | BE-7 | Staff scheduling | HIGH | `schedule.py` (router + service) |
| FT-23 | BE-7 | Staff records / roles / floor-plan colour | HIGH | "restore staff colour picker" slice; `admin.py` staff routes |
| FT-24 | BE-8 | Day-menu scheduling | MEDIUM | `daymenu.py` |
| FT-25 | BE-8 | Happy-hour pricing | MEDIUM | `happyhour.py` |
| FT-26 | BE-8 | Upsell prompts | MEDIUM | `upsell.py` |
| FT-27 | EE-1 | Kitchen task/routing implementation stages | HIGH | `04_HISTORY.md` Stage A/B1/B2* |
| FT-28 | EE-2 | Payment/security hardening implementation stages | HIGH | ADR-017–024, ADR-029 |

Revision from the prior pass: EE-1/EE-2 previously had no Feature layer of
their own ("their content IS the Vertical Slice list"), which meant every
Kitchen/Payment Slice's parent had to be described as a range ("under FT-11
through FT-15") instead of one specific ID — exactly the inconsistency that
produced long/tangled parent-child edges when rendered. FT-27/FT-28 close
that gap so every Slice below has exactly one Feature parent, the same rule
already used for Feature→Epic in this section.

---

## 5. Vertical Slices / Delivered Increments

Same rule as §4: every Slice has **exactly one** Parent Feature ID, no
ranges, no multi-target groupings — this is the fix for the long/tangled
connector lines in the previous pass, which described Kitchen/Payment
parentage as "under FT-11 through FT-15" instead of one ID per row.

Identity: every row below is **HIST** — its Name is the project's own
verbatim label; the `VS-*` code is DERIVED planning scaffolding around it
(same convention as §6, stated once here rather than as a repeated column).

| ID | Parent | Name | State | Confidence | Sources |
|---|---|---|---|---|---|
| VS-01 | FT-11 | Kitchen Stage A — Station model, explicit routing, snapshot-at-fire, Unassigned, filtered KDS, Expo/Floor display | APPROVED / CLOSED | HIGH | `04_HISTORY.md` §"Kitchen Stations — Stage A"; ADR-012–015 |
| VS-02 | FT-13 | Stage B1 — PreparationTask foundation, modifier routing, multi-station fire, rollup, `fire_seq`, fire-batch uniqueness | APPROVED / CLOSED | HIGH | `04_HISTORY.md` §"Stage B1"; ADR-005/006/007 |
| VS-03 | FT-14 | Stage B2 v1 — initial task-mode KDS design | DESIGN REVISION REQUIRED (superseded) | HIGH | `04_HISTORY.md` §"Stage B2 v1" |
| VS-04 | FT-14 | Stage B2 v2 — revised design | DESIGN REVISION REQUIRED (superseded) | HIGH | `04_HISTORY.md` §"Stage B2 v2" |
| VS-05 | FT-14 | Stage B2 v3 — resolved design: mutually exclusive predicates, activation gate, post-lock refresh | APPROVED WITH NON-BLOCKING NOTES | HIGH | `04_HISTORY.md` §"Stage B2 v3"; `KITCHEN_STATIONS_STAGE_B2_v3_DESIGN.md` |
| VS-06 | FT-14 | B2.1 — task-based KDS reads, badge counts, activation guard | APPROVED WITH NON-BLOCKING NOTES | HIGH | `04_HISTORY.md` §"B2.1" |
| VS-07 | FT-14 | B2.2 — per-task READY, parent-Order lock, legacy bridge | APPROVED / CLOSED (re-audited at `f4dd079`) | HIGH | `04_HISTORY.md` §"B2.2," §"2026-08-23 Kitchen Checkpoint" |
| VS-08 | FT-14 | B2.2 PostgreSQL concurrency proof — real two-session PG16 proof of `FOR UPDATE` contract | PASSED (scoped, not general certification) | HIGH | `KITCHEN_STATIONS_STAGE_B2_2_POSTGRES_CONCURRENCY_EVIDENCE.md` |
| VS-09 | FT-14 | Kitchen Release A — reconstruction onto `045dfd5` + integration (`97bed73`) + fire-lock fix (`7225c6e`) | DEPLOYED (superseded by later `main` history) | HIGH | `04_HISTORY.md` §"2026-08-26" |
| VS-10 | FT-28 | Payment Stage 1 — fail-closed production config, liveness/readiness separation | DEPLOYED — DORMANT (Release B, `489f6d2`) | HIGH | ADR-029 |
| VS-11 | FT-28 | Payment Stage 2a — `PaymentAttempt`, durable snapshot, idempotency, independent `RefundAttempt` | DEPLOYED — DORMANT | HIGH | ADR-029; `payment_attempts.py`, `refund_attempts.py` |
| VS-12 | FT-28 | Payment Stage 2b — provider abstraction, contract hardening | DEPLOYED — DORMANT | HIGH | ADR-029; `payment_providers.py` |
| VS-13 | FT-28 | Payment Stage 2c — settlement/charge integration continuation | **WIP / NOT AUTHORIZED / NOT REVIEWED / NOT DEPLOYED** | HIGH | ADR-024, ADR-029 |
| VS-14 | FT-01 | Floor Plan Builder — design slice (free-map coordinate design, admin builder design) | APPROVED / CLOSED | HIGH | `03_CURRENT_WORK.md` |
| VS-15 | FT-01 | Coordinate migration — `map_x_per_mille`/`map_y_per_mille` columns, additive migration, dual-dialect backfill | DEPLOYED (production-validated) | HIGH | `03_CURRENT_WORK.md`; `app/migrate.py` |
| VS-16 | FT-02 | Free-placement admin builder — drag/resize tables and zones in `/admin/tables` | DEPLOYED | HIGH | `03_CURRENT_WORK.md` |
| VS-17 | FT-05 | Zone-overlap fix — resolve coincident zone layouts, overlap warning | DEPLOYED | HIGH | `03_CURRENT_WORK.md` |
| VS-18 | FT-02 | Table visible-bounds fix — keep free-map tables fully inside visible bounds | INTEGRATED | HIGH | `03_CURRENT_WORK.md` |
| VS-19 | FT-03 | Operational Floor port — Map/Arrange ported to Floor page; reservations picker floor-tabs/select-all; staff colour picker | DEPLOYED (irregular path — see §11) | HIGH | `03_CURRENT_WORK.md` |
| VS-20 | FT-04 | Floor card redesign — 7-state status legend, icon-based cards, adaptive height | DEPLOYED | HIGH | `03_CURRENT_WORK.md` |
| VS-21 | FT-07 | Reservations page layout — wide-desktop split, multi-column zone flow, compact form grid | DEPLOYED | HIGH | `03_CURRENT_WORK.md` |
| VS-22 | FT-05 | New-table zone placement fix — new tables render inside their assigned zone | DEPLOYED | HIGH | `03_CURRENT_WORK.md` |
| VS-23 | FT-05 | Zone-fallback grid rewrite — replace ring fallback with a grid to stop many-unplaced-tables overcrowding | DEPLOYED — **independent review still pending** | HIGH | `03_CURRENT_WORK.md`, most recent entry |

**Governance flag, restated explicitly per this task's own invariant list:**
VS-13 (Payment Stage 2c) is not, and must not be represented as, delivered,
accepted, or part of the approved baseline anywhere in this document. The
existence of `release/stage2c-charge` or any self-asserted approval inside
an artifact does not change this (ADR-029's own wording).

**Deferred Kitchen work** (B3/B4, task-owned SERVED, remake/re-fire, richer
task states, WebSocket/SSE) is deliberately **not** listed as a Slice above
— it is an explicit non-scope declaration, not a delivery increment with a
state of its own. It is covered in §11 instead.

**Everything else** (BE-3, BE-5 current line, BE-6, BE-7, BE-8): no
dedicated Vertical Slice history was located for these in `03_CURRENT_WORK.
md`/`04_HISTORY.md` — they exist as running, current functionality
(routers/services/templates), not as a chronicled sequence of approved
increments the way Kitchen/Payment/Floor are. Proposing invented slice
history for them would violate "do not fabricate missing hierarchy." They
are represented at the Feature level (§4) only, with `UNKNOWN` slice
identity, until a real delivery record for them is found or created.

---

## 6. Historical vs Derived Identity Mapping

| Layer | Identity rule applied | Examples |
|---|---|---|
| Project | DERIVED (no project code exists) | PROJ-001 |
| Business Epic | DERIVED (docs name domains/lists, not "Epics") | BE-1..BE-8 |
| Enabler Epic | DERIVED (docs name ADRs/stages, not "Enabler Epics") | EE-1..EE-5 |
| Feature | DERIVED, always | FT-01..FT-26 |
| Vertical Slice — Kitchen | **HIST** — every Kitchen slice ID's *name* is taken verbatim from `04_HISTORY.md`/`01_ARCHITECTURE.md` section headers ("Stage A," "Stage B1," "B2.1," "B2.2," etc.); the `VS-01..VS-09` code itself is a DERIVED planning label wrapping that historical name | VS-01 wraps "Kitchen Stage A" |
| Vertical Slice — Payment | **HIST** — same pattern ("Stage 1," "Stage 2a/2b/2c") | VS-13 wraps "Stage 2c" |
| Vertical Slice — Floor/Reservations | **HIST** — same pattern (`03_CURRENT_WORK.md` section headers are already informal slice names, e.g. "zone-overlap fix," "Operational Floor port") | VS-23 wraps "zone-fallback grid rewrite" |
| Vertical Slice — BE-3/5/6/7/8 | **UNKNOWN** — no slice-level history found; not fabricated | (none proposed) |

No historical ID was invented at any layer. Where the underlying work has a
project-native name, that name is preserved as the entity's `Name` column
in §5 and the `VS-*`/`FT-*`/`BE-*`/`EE-*` code is clearly marked DERIVED
planning scaffolding around it, per instruction.

---

## 7. Evidence / Source Mapping

Consolidated (per-entity sources are already inline in §2–5; this table is
the reverse index — source document → what it grounded):

| Source | Grounds |
|---|---|
| `00_PROJECT_CONTEXT.md` | PROJ-001 scope; BE-8 domain list; Loyalty non-existence statement |
| `01_ARCHITECTURE.md` | BE-1..BE-8 domain existence; EE-1..EE-5 architecture sections; Order Lifecycle (FT-09); all "Deferred"/"Not Accepted" boundaries |
| `02_DECISIONS.md` (ADR-001–029) | Every EE-1/EE-2 governance invariant; VS-10..VS-13 authorization state; VS-01..VS-09 invariants (READY≠SERVED, snapshot-at-fire, no heuristic routing) |
| `03_CURRENT_WORK.md` | VS-14..VS-23 (Floor/Reservations slices) in §5; current branch/deploy reality; risk register feeding §10/§11 |
| `04_HISTORY.md` | VS-01..VS-09 (Kitchen slices) and VS-10..VS-13 (Payment slices) in §5, state/date history |
| `05_AI_HANDOFF.md` | ADR-028 cross-review model (Claude/Codex roles) — governance context, not an Epic/Feature/Slice source itself |
| `app/models/oltp.py`, `app/routers/*.py`, `app/services/*.py` | Direct code confirmation for BE-6 (Channel/DeliveryOrder), EE-3 (require/can gates), EE-4 (star.py/etl.py), EE-5 (migrate.py) |

---

## 8. DEPENDS_ON Candidates

| Depends on | Rationale | Confidence |
|---|---|---|
| VS-02 → VS-01 | B1's PreparationTask is explicitly additive on Stage A's Station/`OrderItem.station_id`; ADR-006 requires retaining the Stage A field during migration | **PROVEN** — ADR-005/006 state this directly, not inferred from order |
| VS-03/VS-04/VS-05 → VS-02 | B2 explicitly reuses/extends the PreparationTask model B1 introduced | **PROVEN** — `04_HISTORY.md` narrates this as one continuing design effort |
| VS-07 → VS-06 | B2.1 "introduced the task-read path while remaining inert until B2.2 capability existed" — an explicit stated sequencing dependency | **PROVEN** — `01_ARCHITECTURE.md` states this in exactly those terms |
| VS-13 → VS-11/VS-12 | Stage 2c continues "later settlement/charge integration" on the same lineage as the attempt/provider model | **LIKELY** — sequence and shared lineage are described; no formal dependency contract is spelled out the way B2.1→B2.2 is |
| VS-21 → VS-19 | The reservations picker reuses the same zone/table data and per-mille model the operational Floor port established; cross-referenced in both slices' own doc sections | **LIKELY** |
| VS-22 → VS-16 | The fix explicitly reuses `_table_map_positions()`, a function the admin builder already had — stated directly in the fix's own commit message and doc section | **PROVEN** |
| VS-23 → VS-22 | The grid rewrite replaces the fallback algorithm the immediately preceding slice wired in; same function, same doc section continuation | **PROVEN** |

**Not claimed, despite temporal proximity:** any dependency between Kitchen
work and Floor/Reservations work. They are parallel, independent lines that
share a repository and eventually a `main` branch — **UNKNOWN/none
asserted**, per instruction not to force relationships to complete the
graph.

---

## 9. ENABLES Candidates

| Enabler | Enables | Rationale | Confidence |
|---|---|---|---|
| EE-1 | BE-4 (all Features FT-11–15) | Every BE-4 Feature is a direct, named consequence of the Station/PreparationTask model and its ADR-encoded invariants | **PROVEN** |
| EE-2 | BE-5 (future, not current) | Explicitly dormant today — "no live router invokes them" (ADR-029). The edge is to a *future*, not-yet-authorized integration, not current BE-5 behavior | **PROVEN** (for dormancy) / the "enables current behavior" reading is explicitly **FALSE**, stated here to prevent the exact misreading the task brief warns against |
| EE-3 | All Business Epics, broadly | Nearly every mutating route observed is gated by `require()`/`can()` | **LIKELY** — real and broad, but not decomposed into a specific edge per Feature (would require enumerating every route, not done here) |
| EE-4 | BE-3, BE-5 | ADR-025: reporting derives from OLTP data; Orders and Payments are the named OLTP domains | **LIKELY** |
| EE-5 | BE-1 (coordinate migration), BE-4 (CSV/XLSX import) | Both are explicit, named uses of the additive-migration mechanism | **PROVEN** |

---

## 10. Unmapped Work / UNKNOWNs

- **AgeOps `planning/state.json`** — does not exist anywhere in this
  repository. Flagged for owner in §14.
- **Customer / Loyalty** — no code, no doc section beyond one negative
  sentence. Not modeled as an Epic; nothing to map.
- **Observability / Reliability** — explicitly disclaimed as not
  established general architecture (`01_ARCHITECTURE.md`). Real but narrow
  (console logging, `/healthz`, one `AuditEntry` table); too thin for an
  Enabler Epic under the "don't create one merely because code exists"
  rule.
- **Third-party delivery integration** (UberEats/DoorDash) — `Channel.
  is_third_party` field exists; no integration code found (FT-21, LOW
  confidence, no Slice).
- **Table-open concurrency risk / reservation-overlap control** — both
  explicitly named as open, unconfirmed risks, not slices with a state;
  carried as risk-register items, not Vertical Slices.
- **BE-3, BE-5(current), BE-6, BE-7, BE-8 Vertical Slice history** — no
  chronicled delivery-increment record was found for these (see the closing
  note under §5, "Everything else"). `UNKNOWN`, not fabricated.
- **Governance agent** — `03_CURRENT_WORK.md`: "NOT YET ASSIGNED." No
  enforcement mechanism configured yet beyond ADR-028's stated intent.
- **Day-menu / happy-hour / upsell exact scope** (FT-24/25/26) — service
  files exist; no dedicated approval history was located.

---

## 11. Dormant / Rejected / Not-Authorized Work

This section exists specifically so this document cannot be misread as
claiming any of the following as delivered:

| Item | Actual status | Do not represent as |
|---|---|---|
| VS-10, VS-11, VS-12 | DEPLOYED, but **dormant** — shipped in production, **no live router invokes them** | Active runtime behavior, or as enabling current BE-5 functionality |
| VS-13 | **WIP / NOT AUTHORIZED / NOT REVIEWED / NOT DEPLOYED** | Approved baseline, delivered work, or an accepted slice under any framing |
| VS-03 | DESIGN REVISION REQUIRED — rejected by independent review, superseded | A closed or accepted design |
| VS-04 | DESIGN REVISION REQUIRED — rejected by independent review, superseded | A closed or accepted design |
| VS-19 | DEPLOYED, but via a push that bypassed this project's own explicit-authorization gate (recorded in `03_CURRENT_WORK.md` as "Integration/push/deploy happened outside this project's own authorization discipline") | A cleanly-authorized deployment — it is deployed, but the process irregularity is part of its historical record and should not be silently smoothed over |
| VS-23 | DEPLOYED, but **not yet independently reviewed** as of this document | A reviewed/closed slice |
| the Deferred Kitchen work item (B3/B4, task-owned SERVED, remake/re-fire, richer task states, WebSocket/SSE) | NOT AUTHORIZED | Planned/queued work with a commitment behind it — it is explicitly deferred, not scheduled |

ADR-024's core holding is restated here rather than re-derived: **Stage 2c
is not an approved baseline, and the payment branch tip must never be
treated as one fully approved block.** Nothing in this document changes
that.

---

## 12. Confidence Assessment

| Area | Confidence | Why |
|---|---|---|
| Kitchen Epic/Feature/Slice structure (EE-1, BE-4, VS-01..VS-09) | **HIGH** | Extremely well-documented: named stages, explicit review verdicts, dated history, a real PostgreSQL concurrency proof artifact |
| Payment/Security structure (EE-2, VS-10..VS-13) | **HIGH** | Equally well-documented via ADR-017–024, ADR-029, with an explicit supersession record (ADR-029 supersedes five earlier ADRs' status fields while preserving their substantive holdings) |
| Floor/Reservations structure (BE-1/2, VS-14..VS-23) | **HIGH** for slice existence and state (this session's own direct working record); **MEDIUM** for the Business Epic split between BE-1 and BE-2 (a reasonable but inferred boundary, not a documented one) |
| BE-3, BE-5(current), BE-6, BE-7, BE-8 | **MEDIUM** at Feature level (real code, no fabricated slice history); **UNKNOWN** at Slice level (honestly absent, not guessed) |
| DEPENDS_ON / ENABLES relationships | Mixed, stated per-edge in §8/§9 — five PROVEN, three LIKELY, zero forced |
| Overall document | **MEDIUM-HIGH** — strong where the project's own governance docs are strong (Kitchen, Payment), softer where this derivation had to infer Epic/Feature groupings the docs never name as such (BE-3/6/7/8, all Features) |

---

## 13. Proposed AgeOps Import Mapping

AgeOps's actual native schema/import contract was **not found in this
repository** (no `planning/state.json`, no AgeOps config, no vendored
AgeOps client code). The mapping below is therefore a **structural
proposal**, not a confirmed field-level import spec — flagged explicitly so
it is not mistaken for one.

Proposed 1:1 structural mapping (this document's own model IS the
requested target model, so the mapping is direct):

```text
PROJ-001                    → AgeOps Project node
BE-*, EE-* (with category)  → AgeOps Epic node, category = BUSINESS | ENABLER
FT-*                        → AgeOps Feature node, parent = its BE-*/EE-*
VS-*                        → AgeOps Vertical Slice node, parent = its FT-*
```

Proposed field carry-over per node:
- `identity_type` (HIST | DERIVED) → a custom/metadata field, not silently
  dropped — AgeOps consumers should be able to tell a reconstructed grouping
  from a project-native name.
- `state` (e.g. APPROVED/CLOSED, DEPLOYED-DORMANT, WIP/NOT AUTHORIZED) → map
  to whatever AgeOps's own status enum offers; **do not** collapse
  DEPLOYED-DORMANT and DEPLOYED into the same value without a distinguishing
  flag, since ADR-029 treats that distinction as load-bearing.
- `confidence` (HIGH/MEDIUM/LOW) → a metadata field, not a status.
- Relationship edges (§8/§9) → import only PROVEN and LIKELY edges; do not
  import UNKNOWN/not-asserted relationships as empty/placeholder edges.
- Relative Size → **do not import a size value.** None is assigned anywhere
  in this document (per instruction); if AgeOps requires a non-null size
  field, that is a schema conflict for the owner to resolve, not something
  this derivation should paper over with an invented number.

---

## 14. Human Owner Decisions Required

1. **AgeOps `planning/state.json` does not exist in this repository.** Is it
   maintained externally, never set up, or was this task templated from a
   different project? Also: is a real AgeOps import schema available
   anywhere so §13 can be replaced with an actual field-level mapping
   instead of a structural proposal?
2. **Nu-Brite's `DELIVERY_MAP.md` was named as the rigor benchmark but is
   not accessible from here.** If it exists in another repository/location,
   point me at it and I will reconcile this document's format against it
   directly rather than against my own reading of this prompt's section
   list.
3. Same five open framing questions as the prior derivation pass (BE-7/EE-3
   split; BE-6 as its own Epic vs. a Feature of a unified Order-Channels
   Epic; BE-8 as its own Epic vs. folded into Ordering; whether BE-3/5/6/7/8
   should get real Vertical Slice history reconstructed from git log even
   though no doc chronicles it — this document deliberately did not
   attempt that reconstruction from git log alone, treating doc silence as
   `UNKNOWN` rather than mining commits for it, since the task brief asks
   for reconstruction from "the strongest available evidence" and the
   *documented* governance trail is materially stronger evidence than raw
   commit messages for those domains).
4. **VS-23 independent review is still pending** — operational, not
   structural, but material to any "what's actually done" reading taken
   today.
5. **VS-19's irregular deployment path** (pushed outside the
   authorization gate, later reconciled) is preserved in §11 rather than
   smoothed over — confirm this is the right level of detail for an AgeOps
   import, versus a pointer back to `03_CURRENT_WORK.md`'s fuller account.

---

## READY FOR AGEOPS NORMALIZATION: **NO**

Reasons, all independently sufficient:
- AgeOps's actual import schema/contract was not found in this repository,
  so §13 is a structural proposal, not a validated mapping — normalizing
  against an unconfirmed contract risks a rework cycle.
- One Vertical Slice (VS-23) is deployed but not yet independently
  reviewed.
- Five Human Owner decisions in §14 are genuinely open and would change
  the Epic/Feature grouping (not just labels) if resolved differently.
- The Nu-Brite rigor comparison requested by the task could not be
  performed — this document's rigor is self-assessed against the task's
  own section list, not benchmarked against the named reference artifact.
