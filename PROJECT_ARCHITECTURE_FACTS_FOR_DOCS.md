# PROJECT_ARCHITECTURE_FACTS_FOR_DOCS.md

Read-only architecture fact-finding for the Restaurant App, per MASTER PROMPT V2.
No source, docs, tests, schema, data, or git state were modified. Findings are
evidence-classified (CODE-CONFIRMED file:line · TEST-CONFIRMED command+result ·
DOCUMENT-CONFIRMED doc+status · INFERRED · UNRESOLVED) and status-classified
(CURRENT FACT · APPROVED INVARIANT · CURRENT RISK · FUTURE/DEFERRED).

---

## 2. Repository Baseline

```text
Repository inspection date: 2026-08-20
Repository root / pwd:      c:/Users/enave/Projetos/Restaurant/restaurant_app
Current branch:            feat/floor-map

Working tree clean:                 NO
Uncommitted changes present:        YES (14 modified, 40 untracked)
Relevant untracked files present:   YES (docs/, app/services/reservations.py, tests/test_prep_tasks.py,
                                    tests/test_stations.py, web/templates/admin_stations.html, expo.html,
                                    the KITCHEN_STATIONS_* artifacts, this file)

Persistent docs found (all in ./docs/):
- 00_PROJECT_CONTEXT.md: YES  docs/00_PROJECT_CONTEXT.md
- 01_ARCHITECTURE.md:    YES  docs/01_ARCHITECTURE.md
- 02_DECISIONS.md:       YES  docs/02_DECISIONS.md
- 03_CURRENT_WORK.md:    YES  docs/03_CURRENT_WORK.md
- 04_HISTORY.md:         YES  docs/04_HISTORY.md
- 05_AI_HANDOFF.md:      YES  docs/05_AI_HANDOFF.md

Production environment inspected: NO
Production execution performed:   NO
Production data modified:         NO

Live PostgreSQL execution performed:        NO
Live payment-provider execution performed:  NO

Source code modified:    NO
Tests modified:          NO
Dependencies modified:   NO
Git history modified:    NO
```

Note on the six docs: they exist and are maintained, but their content is almost
entirely **Kitchen Stations**. Payments, refunds, Square, security, environments,
reporting, backup, and observability are essentially undocumented there (see §35, §39).

---

## 6. Whole-System Architecture

**Stack (CODE-CONFIRMED, `requirements.txt`, `requirements-prod.txt`, `Dockerfile`)**
- Python: dev pinned to 3.14 (cp314 wheels per `requirements.txt` header); prod image `python:3.13-slim` (`Dockerfile`). Two runtimes — a parity note.
- FastAPI 0.139.2, Starlette 1.3.1, SQLAlchemy 2.0.51, Jinja2 3.1.6, uvicorn[standard] 0.51.0, python-multipart 0.0.32, python-dotenv 1.2.2. Tests: httpx 0.28.1.
- Prod-only (`requirements-prod.txt`): `psycopg[binary]` 3.2.3 (PostgreSQL), gunicorn 23 (UvicornWorker), Pillow 11 (optional; `app/services/images.py` degrades if absent).
- **Pydantic is not pinned** — it arrives transitively via FastAPI (UNRESOLVED exact version).
- **No Square SDK dependency.** Square is a hand-rolled `httpx` client (`app/services/square.py`); no vendor SDK. (CODE-CONFIRMED)
- Frontend: server-rendered Jinja2 + vanilla JS (`web/static/schedule.js`, `tests/js`); no SPA framework. Kitchen refresh = polling (DOCUMENT-CONFIRMED ADR-011; CODE-CONFIRMED KDS templates).

**Repository ownership (CODE-CONFIRMED)**
- `app/` — models (`models/oltp.py` OLTP, `models/star.py` reporting star schema), routers (`admin, analytics, auth, pay, reservations, sales, schedule`), services (`payments, refunds, square, money, closeout, settings, reservations, schedule, daymenu, happyhour, upsell, images, receipt_delivery`), `migrate.py` (additive migrations), `database.py`, `deps.py` (auth/permissions/templates), `security.py`, `bootstrap.py`, `seed.py`, `etl.py` (OLTP→star), `reports.py`.
- `db/` — local SQLite dev DB (`db/restaurant.db`); not production schema authority (DOCUMENT-CONFIRMED 01_ARCHITECTURE).
- `mvp_menu_upload/`, `outbox/`, `scripts/`, `docs/`, `tests/`, `web/` (35 templates + static).
- Deployment scaffolding: `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml`, `Caddyfile`, and multiple target docs (`RENDER.md`, `KOYEB.md`, `CLOUD_RUN.md`, `DEPLOY.md`) → deployment target is **not singular** (INFERRED: portable container, historically Render/Neon, also Cloud Run/Koyeb notes).

**Stale-bytecode finding (CODE-CONFIRMED · CURRENT RISK, maintainability):** `app/services/__pycache__/` contains `charge.cpython-314.pyc`, `payment_attempts.*.pyc`, `payment_providers.*.pyc`, `refund_attempts.*.pyc`, `settlement.*.pyc` **with no corresponding `.py` source and no git history** (`git log -- app/services/<f>.py` empty). These are orphaned compiled artifacts of files that never existed in the tracked tree. **Consequence for this audit:** the "provider abstraction / durable PaymentAttempt / RefundAttempt / settlement / reconciliation-engine" architecture that a reader might infer from those names **is NOT implemented in current source** (see §13).

---

## 7. Architectural Layers & Boundaries (STRUCTURAL OBSERVATIONS)

Actual layering (CODE-CONFIRMED):
- HTTP routing + request parsing: FastAPI routers (`app/routers/*`).
- AuthN/AuthZ: `app/deps.py` (`current_staff`, `require`), `app/security.py` (cookie/PIN).
- Business/domain + financial logic: `app/services/*` (notably `payments.py`, `refunds.py`, `closeout.py`). This is a genuine service layer for money.
- **But** substantial domain logic also lives directly in routers — the KDS/kitchen state machine, ticket assembly, and `_recompute_kitchen` live in `app/routers/sales.py` (e.g. `sales.py:1525` `_recompute_kitchen`, `sales.py:1798` `kitchen_display`), and the Square terminal orchestration lives in `app/routers/pay.py:416-603`. Provider-specific Square calls are invoked directly from the router, not behind an abstraction.
- Persistence: SQLAlchemy 2.0 Mapped models; no repository layer.
- Templates: Jinja2; the docs' own rule ("business invariants should not live only in templates", 01_ARCHITECTURE) is a stated intent, not enforced.
- Config: **no central config module** — `os.environ` is read ad hoc in `database.py`, `security.py`, `services/square.py`, `services/settings.py`. (STRUCTURAL OBSERVATION · relevant to §20 fail-closed.)

---

## 8. Domain Inventory (CODE-CONFIRMED unless noted)

| Domain | Status | Evidence |
|---|---|---|
| Menu / MenuItem / Modifier / ModifierOption | IMPLEMENTED | `models/oltp.py`, `menu_data.py` |
| Orders / OrderItem | IMPLEMENTED | `oltp.py`, `routers/sales.py` |
| Kitchen / Stations | IMPLEMENTED (Stage A) | `Station` `oltp.py:~470`, KDS `sales.py:1798` |
| PreparationTask / PreparationTaskModifier | IMPLEMENTED (B1 foundation), **reads NOT migrated** | `oltp.py:~1000-1080`, `sales.py` fire path |
| Payments / PaymentAllocation / Seat ledger | IMPLEMENTED | `services/payments.py` |
| Refunds | IMPLEMENTED (record-only reversal) | `services/refunds.py`, `Refund` `oltp.py:~1340` |
| Floor / Tables / Zones / Floors | IMPLEMENTED | `oltp.py`, `sales.py` |
| Reservations / Waitlist | IMPLEMENTED | `services/reservations.py`, `routers/reservations.py` |
| Staff / Auth / Permissions | IMPLEMENTED | `deps.py`, `security.py`, `routers/auth.py` |
| Channels (dine-in/takeout/delivery) | IMPLEMENTED (channel field + delivery-only instruments) | `oltp.py` Channel, `payments.py:309` |
| Delivery | PARTIAL | delivery queue `sales.py:2200`, `DeliveryOrder`, `platform_ref`; no dispatch/provider integration found |
| Business Day / Closeout / Z-report | IMPLEMENTED | `services/closeout.py`, `DayClose` (`date` unique) `oltp.py:1586`, `etl.py`, `reports.py` |
| Reporting (star schema) | IMPLEMENTED | `models/star.py`, `etl.py`, `analytics.py` |
| Scheduling (shifts/swaps/time-off) | IMPLEMENTED | `services/schedule.py`, `routers/schedule.py` |
| Happy hour / Day menu / Upsell | IMPLEMENTED | `services/happyhour.py, daymenu.py, upsell.py` |
| Imports (CSV/XLSX routing) | IMPLEMENTED | `routers/admin.py` import_routing |
| Customers / Loyalty | NOT FOUND (as first-class) | no customer/loyalty models surfaced; reservation holds guest name/phone |
| Multi-tenant / location | NOT FOUND (by design) | DOCUMENT-CONFIRMED ADR-016 |
| Provider abstraction / PaymentAttempt / RefundAttempt / reconciliation engine | NOT FOUND (source) | only orphan `.pyc` (§6) |

---

## 9. State Ownership & State Machines

**Ownership matrix (CODE-CONFIRMED)**

| Concept | Authoritative Owner | Derived Consumers | Enforcement |
|---|---|---|---|
| Order.status | `Order.status`, set by `recompute_order_status` (`payments.py:563`) and `_recompute_kitchen` (`sales.py:1525`) | floor, pay screen, reports | Application |
| Order.kitchen_status | `Order.kitchen_status`, `_recompute_kitchen` | KDS/Expo/floor board gate | Application |
| OrderItem.kitchen_status | `OrderItem.kitchen_status` (compat/rollup) | KDS, order, payment gate | Application (rollup `rollup_item_kitchen_status` `sales.py:1628`) |
| PreparationTask.status | `PreparationTask.kitchen_status` (created at fire) | (B1) rollup only; KDS reads NOT yet | Application; **no live writer past fire** in B1 |
| Payment result (local) | `Payment` + `PaymentAllocation` rows | balance panel, order status, reports | DB (`ck_payment_total_nonneg`) + Application |
| Payment result (external/Square) | **Square (external), not persisted locally** | — | **NOT ENFORCED locally** (§13) |
| Refund result | `Refund` row (`oltp.py:~1340`) | reports, drawer | DB (`ck_refund_amount_pos`) + Application |
| Outstanding balance | Derived: `sum(line_total) - sum(allocations)` (`payments.py`) | pay, order status | Application (recomputed, not stored) |
| Table occupancy | `RestaurantTable.status` + `current_waiter_id` | floor | Application (**no row lock on open**, §18) |
| Reservation state | `Reservation.status` | reservations board | Application |
| BusinessDay state | `DayClose` (unique `date`) | Z-report | DB unique + Application |

**State machines (CODE-CONFIRMED; enforcement = application/convention unless a CheckConstraint is cited):**
- **OrderItem.kitchen_status:** `pending → preparing → ready → served`. READY≠SERVED (APPROVED INVARIANT, ADR-002). Rollup never sets SERVED (`sales.py:1638-1645`).
- **PreparationTask.kitchen_status:** enum PREPARING/READY/SERVED (`CheckConstraint` `oltp.py:~1061`); in B1 only PREPARING is written (at fire); READY/SERVED task transitions are **B2/deferred** (DOCUMENT-CONFIRMED 03_CURRENT_WORK).
- **Order.status:** OPEN/PREPARING/READY/PARTIALLY_PAID/PAID/SERVED/CLOSED/CANCELLED (derived; `recompute_order_status`).
- **Seat.status:** open/paid_partial/paid (`ck_seat_status` `oltp.py:866`).
- **Payment:** created → (optionally) voided (`voided`, `voided_at`, `voided_by_id`); voided kept, allocations deleted (`void_payment` `payments.py:617`). No external-charge state machine (§13).
- **Reservation:** WAITING/…/NO_SHOW/seated (auto no-show on window elapse, `services/reservations.py`).

---

## 10. Authoritative / Derived / Snapshot Data (CODE-CONFIRMED)

| Value | Class |
|---|---|
| `OrderItem.station_id` | SNAPSHOT (at fire) + LEGACY COMPATIBILITY (ADR-006) |
| `PreparationTask.station_id` | SNAPSHOT (at fire) |
| `OrderItem.kitchen_status` | DERIVED (rollup) / AUTHORITATIVE for compat (ADR-007) |
| Order.status / Order.kitchen_status | DERIVED |
| Outstanding balance | DERIVED (never stored) |
| Payment/refund provider identifiers | **ABSENT** — not persisted (see §13) |
| `Payment.total_cents`, `items_cents`, `tax_cents`, … | AUTHORITATIVE SNAPSHOT (frozen at settlement) |
| Receipt `payload_json` | SNAPSHOT (frozen at issue, `payments.py:663`) |
| Table occupancy | AUTHORITATIVE (`RestaurantTable.status`) |
| Star-schema fact rows | DENORMALIZED/DERIVED (ETL from OLTP, `etl.py`) |
| DayClose totals | SNAPSHOT (Z-report window) |

---

## 11. Cross-Domain Invariants

| Invariant | Owner | Enforcement | Concurrency-safe? | Tests |
|---|---|---|---|---|
| OrderItem READY ↔ all its PreparationTasks READY | rollup `sales.py:1628` | Application | B1: created-at-fire only; **B2 will add the live path + legacy-write bridge** (03_CURRENT_WORK §Required B2 v2) | `test_prep_tasks.py` (TEST-CONFIRMED pass) |
| Paid Order ↔ durable **local** payment evidence | `Payment`/`PaymentAllocation` | DB rows + `recompute_order_status` | Yes for local (order row lock `_lock_order`) | `test_reconciliation.py` (pass) |
| Paid-by-card Order ↔ durable **Square** evidence | — | **NOT ENFORCED** (no processor id stored) | N/A | none |
| Refund ↔ original payment (≤ refundable) | `refunds.py` `refundable_cents` | Application | UNRESOLVED (no lock verified on refund path) | `test_reconciliation.py` covers refund→report |
| Table occupied ↔ active order | `RestaurantTable.status` | Application | **No** (no row lock on open, §18) | `test_admin.py` (currently FAILING, §32) |
| Reservation ↔ table/time (no double-book) | reservations | **No explicit overlap guard found** | UNRESOLVED/INFERRED gap | `test_reservations_map.py` (pass; scope = map, not overlap) |
| Order balance ↔ charges − refunds | derived | Application | Yes for pay path | `test_reconciliation.py` |
| READY ≠ SERVED | order/kitchen | Application/convention | — | `test_prep_tasks.py`, `test_stations.py` |

---

## 12. Order / Kitchen / PreparationTask (verified against current code)

- Order lifecycle create→add→course/hold→fire→prepare→READY→SERVED→pay→close is CODE-CONFIRMED in `routers/sales.py` and `services/payments.py`.
- **READY ≠ SERVED enforcement:** APPLICATION-ENFORCED. Payment of a seat/item requires SERVED items — the terminal path calls `_require_served(order)` and filters to `KitchenStatus.SERVED` items (`pay.py:430, 440, 535`). No DB constraint ties READY↔SERVED; it is application + workflow.
- Kitchen Stations Stage A elements all present: `Station` (`oltp.py:~470`, `name` unique, `ix_station_active_order`), `MenuItem.station_id`, `OrderItem.station_id` snapshot at fire, KDS station filter reading the **OrderItem** snapshot (`sales.py:1901`), Unassigned = NULL, inactive-station-with-live-work tab construction (`sales.py:1956-1963`), Expo (`sales.py:2073`), CSV/XLSX import (`admin.py`).
- PreparationTask (B1): tables + `PreparationTaskModifier`, nullable `Modifier.station_id`/`ModifierOption.station_id`, multi-station creation at fire, snapshots, rollup fn, fire-batch unique index `uq_prep_task_batch (order_item_id, fire_seq, COALESCE(station_id,-1))` (`oltp.py:1074`; `migrate.py` verifies by definition, **fatal** on violation), observable backfill, `fired_items_without_tasks` B2 gate (`sales.py:1677`). **KDS/Expo/Floor still read the Stage A OrderItem snapshot — reads NOT migrated** (CODE-CONFIRMED; matches DOCUMENT-CONFIRMED 03).
- **Stage status (do not overstate):** Stage A + B1 APPROVED/CLOSED; **B2 NOT approved** — status DESIGN REVISION REQUIRED (§34).

---

## 13. Payment / Refund / Tender — Deep Audit (CORRECTS the prompt's assumed model)

**Actual architecture (CODE-CONFIRMED):**
- Local settlement is a **seat-ledger** model (`services/payments.py`): `pay_seat`, `pay_whole_order` compute items/tax/tip/service/surcharge in integer cents, write one `Payment` + N `PaymentAllocation`, re-derive seat/order status. Order row is locked (`_lock_order` → `SELECT … FOR UPDATE`, no-op on SQLite, `payments.py:318`) to serialize concurrent settlement.
- Card-present is **Square Terminal API**, driven from the **router** (`pay.py:416-603`) via `services/square.py` (httpx). One provider, no abstraction/registry.
- **There is no `PaymentAttempt`, no `RefundAttempt`, no provider registry, no reconciliation engine in source** (§6). "Reconciliation" in this codebase = **end-of-day cash Z-report** via ETL to the star schema (`closeout.py`, `etl.py`, `test_reconciliation.py`), not payment-provider reconciliation.
- **Refunds are record-only** (`services/refunds.py`): `refund_payment` writes a `Refund` row (`method`, `is_cash`), capped at `refundable_cents`. **It does not call Square to reverse a card charge** — no Square reference in `refunds.py`. Card refunds are a local ledger/reporting/drawer entry; the actual card reversal is out of the app. (CODE-CONFIRMED · CURRENT RISK if operators assume card money is auto-returned.)

**Financial-safety invariants (honest verdicts):**

| Invariant | Verdict | Evidence |
|---|---|---|
| Durable PaymentAttempt exists before external charge I/O | **NOT CONFIRMED** — no attempt row; local `Payment` is written only *after* Square COMPLETED (`pay.py:556-571`) | CODE |
| External charge success requires durable processor identity/evidence | **NOT CONFIRMED** — Square `payment_id`/`checkout_id` never persisted; only `card_last4`/`card_brand` | CODE (`oltp.py` Payment has no processor field) |
| Refund success requires durable processor identity/evidence | **NOT CONFIRMED** — refund is local record; no processor id | CODE |
| Ambiguous external result enters reconciliation | **PARTIAL** — on a 120s poll timeout `wait_for_checkout` returns a still-PENDING checkout; recovery relies on the same `checkout_id` being polled again (`terminal_status`), which re-records once outstanding. No background reconciliation; if the waiting page is abandoned, a captured charge may leave no local record | CODE (`square.py:182`, `pay.py:501-576`) |
| Charge retry is idempotent | **PARTIAL** — local double-record is prevented by outstanding-balance check (`pay.py:536-547`); **but** `create_checkout` mints a fresh `idempotency_key=uuid4()` each call (`square.py:152`), so a re-submitted *start* is not idempotent at Square | CODE |
| Refund retry is idempotent | **NOT CONFIRMED** — capped at refundable, but no idempotency key/uniqueness | CODE |
| External identifiers unique where required | **NOT ENFORCED** — none stored | CODE |
| Processor amount checked vs local expected | **NOT CONFIRMED** — completed Square amount not reconciled against local total | CODE |
| Currency validated | **PARTIAL** — currency from `SQUARE_CURRENCY` (default CAD) sent to Square; not reconciled on completion | CODE (`square.py:70`) |
| Unknown provider fails explicitly | N/A — single hard-wired provider |
| Partial Square config fails closed | **CONFIRMED (as feature-off)** — `is_configured()` requires token+location+device; otherwise terminal path 400s and manual entry is used (`square.py:80`, `pay.py:427`) | CODE |
| Local flag alone cannot fabricate external success | **CONFIRMED** — local Payment is only written after Square status COMPLETED | CODE |
| Refund has independent durable lifecycle | **PARTIAL** — durable `Refund` row exists, but no provider execution/lifecycle | CODE |
| Provider-success/local-failure recoverable | **PARTIAL** — recoverable only via re-poll of the live checkout page (no durable attempt/reconciler) | CODE |
| Reconciliation concurrency-safe | Cash Z-report path: INFERRED safe (ETL batch); provider reconciliation: N/A |

**NEW OBSERVATION — NOT PREVIOUSLY APPROVED (HIGH):** the card-terminal charge has a **durability window** — Square can capture money while no local `Payment` is guaranteed (crash between COMPLETED and `db.commit()`, or an abandoned waiting page). There is no durable attempt record and no background reconciler to detect/repair it. Recommended next audit action: trace every `terminal_status` exit path and decide whether a persisted checkout/attempt row + a reconcile-on-load sweep is warranted. **Do not fix now.**

---

## 14. Payment Methods / Tender Model (CODE-CONFIRMED)

`PaymentInstrument` rows (cash, card, contactless, delivery-only platforms). For each:
- **Cash** — represented by a cash instrument; truth = local `Payment` row; refundable via `Refund` (`is_cash=True`, hits drawer); no external system.
- **Card (manual entry)** — `card`/`contactless` instrument; `card_last4`/`card_brand` optional; truth = local `Payment`; card surcharge applies (`_card_surcharge` `payments.py:295`).
- **Card (terminal / Square)** — booked under a "Card (terminal)" instrument (`pay.py:402`); truth = local `Payment` written post-COMPLETED; **no processor id stored** (§13).
- **Delivery platforms** — `delivery_only` instruments validated against delivery channel (`_validate_instrument` `payments.py:309`).
- **Split / partial / multi-tender** — CONFIRMED: seat ledger + `PaymentAllocation` support per-seat, per-item, partial-close settlement (`pay_seat` `item_ids`, `is_partial_close`). Tips, discounts (manager-gated), service charge, card surcharge, change/overpayment guarded (`total_cents >= 0`). Gift card: NOT FOUND.
- **Void vs refund** — distinct: `void_payment` (pre-close reversal, deletes allocations, keeps row) vs `Refund` (post-settlement). CODE-CONFIRMED.

Cash and external card are **not** treated as identical truth models (cash has no external leg; terminal card has an external leg that is not persisted).

---

## 15. Financial Correctness (CODE-CONFIRMED)

- **Integer minor units (cents) throughout** — models, ledger, DB (`database.py` header states this explicitly; `Payment.*_cents`). No `float`/`Decimal` columns for money.
- Splitting uses exact largest-remainder integer math — `distribute`/`split_evenly` guarantee `sum == total` (`money.py:16,49`). Good.
- **Binary float in a money path (flag, LOW):** `pct()` uses `int(amount_cents * percent / 100 + 0.5)` (`money.py:62`) — binary float for tax/tip/surcharge/gratuity percentages. For realistic cent magnitudes this is safe, but it is float arithmetic on money and should be acknowledged.
- Historical values are **SNAPSHOTTED** at settlement (Payment columns + Receipt `payload_json`), tax split recomputed for display from stored base (`_tax_breakdown` `payments.py:594`). Reporting reads snapshots via ETL, not live recompute.

---

## 16. Database / Data Integrity / Migrations

- Engines: SQLite dev (default `sqlite:///db/restaurant.db`, FK + WAL pragma), PostgreSQL prod via `DATABASE_URL` (`database.py:27`) with pool_pre_ping, `pool_recycle` default 280s (Neon serverless-aware; **CONFIRMED prod = Neon**, verified by direct connection 2026-08-26: host `*.neon.tech`, database `neondb`, PostgreSQL 18.6, 50 tables, row counts reconciled against a restore. The Render Postgres instance still exists but is EMPTY — `RENDER.md` describes the pre-migration setup). `expire_on_commit=False`.
- Migrations: **custom, not Alembic** (`migrate.py`). `create_all` makes missing tables; `ADDED_COLUMNS`/`WIDENED_COLUMNS` are guarded idempotent `ALTER`s (PRAGMA on SQLite, information_schema on Postgres). The `uq_prep_task_batch` index is a **required, verified-by-definition invariant that raises/halts startup** on duplicate identities or wrong definition; the prep-task **backfill is best-effort and observable** (logged `SKIPPED …`, never blocks). CODE-CONFIRMED `migrate.py`.

**Integrity matrix (CODE-CONFIRMED)**

| Invariant | DB | App | Engine-specific | Test evidence |
|---|---|---|---|---|
| PreparationTask fire-batch uniqueness | YES (`uq_prep_task_batch`, expression index) | YES | YES (SQLite keylist vs PG catalog) | `test_prep_tasks.py` (pass) |
| Station name uniqueness | YES (`name unique`, `oltp.py:485`) — **raw, not normalized** | partial | no | `test_stations.py` (pass) |
| Payment total non-negative | YES (`ck_payment_total_nonneg`) | YES | no | `test_reconciliation.py` |
| Refund amount positive / ≤ refundable | YES (`ck_refund_amount_pos`) + app cap | YES | no | `test_reconciliation.py` |
| Seat number per order | YES (`uq_seat_order_number`) | — | no | — |
| Shared-item share per seat | YES (`uq_share_item_seat`) | — | no | — |
| Table number / Order code / Zone(floor,name) uniqueness | YES | — | no | — |
| DayClose one-per-date | YES (`date unique`) | — | no | `test_reconciliation.py` |
| Payment/refund **processor id** uniqueness | **NONE (not stored)** | — | — | — |
| Payment idempotency | NONE (relies on outstanding-balance app logic) | partial | — | — |

Backlog (DOCUMENT-CONFIRMED 03): normalized station-name uniqueness, duplicate-ID rejection in routing import, CSV formula-injection hardening — all still open (CODE-CONFIRMED unaddressed).

---

## 17. Performance / Query / Capacity (CODE-CONFIRMED structural; NOT MEASURED)

| Path | Structural risk | Existing protection | Runtime measured? |
|---|---|---|---|
| KDS (`sales.py:1798`) | board scan | filter-before-LIMIT 80, aggregates in SQL, `selectinload(items→station)`, oldest-first | NO |
| Expo (`sales.py:2073`) | board scan | LIMIT 60, `selectinload` | NO |
| Order page / pay | per-item allocation sums in Python | bounded to one order | NO |
| Reports / ETL | full-history aggregate | star schema + windowed close | NO |
| Reservations board | day scan | status/time index (`ix_reservation_status_at`) | NO |

ADR-012 (filter-before-limit) is CODE-CONFIRMED in the KDS. No EXPLAIN/query-plan was run (see §29). Scaling: prod = gunicorn multi-worker (`docker-entrypoint.sh -w`), Postgres pool 10+20. KDS polling frequency ~15s (DOCUMENT-CONFIRMED design). No load target defined.

---

## 18. Concurrency / Idempotency / Multi-Terminal (CODE-CONFIRMED)

- **Order settlement:** row-locked (`_lock_order` FOR UPDATE) in `pay_seat`, `pay_whole_order`, `void_payment` → concurrent double-pay serialized (PG); SQLite serializes writers. Good.
- **Fire / PreparationTask creation:** Order row lock + `IntegrityError`-state-verify + durable unique index (B1). Good (TEST-CONFIRMED `test_prep_tasks.py` incl. a two-session overlap test).
- **Table open / seat:** `open_table` sets OCCUPIED + `current_waiter_id` with **no row lock** (`sales.py:441-490`) → two waiters opening the same table race (last-writer-wins). CURRENT RISK (matches backlog).
- **Reservation:** no explicit overlap/`FOR UPDATE` found (§27) — INFERRED double-booking risk, UNRESOLVED.
- **BusinessDay close:** `DayClose.date` unique gives DB-level idempotency for one-close-per-date; concurrency of the close routine itself UNRESOLVED (not traced).
- **Multi-terminal:** the app assumes multiple concurrent terminals (Postgres prod rationale in `database.py`; "two tablets can't double-pay" in `payments.py:318`). Frontend polling; no WebSocket. Duplicate-submit protection is **backend-state-based** for pay/fire (good) but **absent** for table-open.

---

## 19. Reliability / Failure / Recovery (CODE-CONFIRMED / INFERRED)
- Pay/fire: single transaction, commit at end; failure before commit → rolled back, no partial local state; retry safe (state-based idempotency). 
- Terminal charge: **external side effect precedes commit** → durability window (§13). 
- Migration: prep-index failure is fatal (halts); additive/backfill failures are observable and non-blocking. 
- Explicit timeouts: Square httpx 20s/request, 120s poll (`square.py:30,182`); DB pool recycle 280s. No app-level retry/backoff loops (charge relies on terminal + re-poll). 

---

## 20. Environments / Configuration / Deployment

- Environments: local + test = SQLite; **production = PostgreSQL on Neon (CONFIRMED 2026-08-26, see §16)**, PostgreSQL 18.6. Staging: NOT FOUND (no staging config evidenced). Square: sandbox by default, production via `SQUARE_ENV`.
- Config variable NAMES (values never read): `DATABASE_URL`, `DB_POOL_RECYCLE/SIZE`, `DB_MAX_OVERFLOW`, `SECRET_KEY` (SECRET), `COOKIE_SECURE`, `TZ`, `SQUARE_ENV`, `SQUARE_ACCESS_TOKEN` (SECRET), `SQUARE_LOCATION_ID`, `SQUARE_DEVICE_ID`, `SQUARE_CURRENCY`, `PORT`.
- **Fail-closed audit (CURRENT RISK):**
  - `SECRET_KEY` — **FAIL-OPEN.** Falls back to a hardcoded public dev secret with **no production guard** (`security.py:32,38`). If unset in prod, session cookies are forgeable → identity spoofing (become owner).
    **This was NOT theoretical: it was ACTIVE in production until 2026-08-26.** The Render
    environment held a variable named `Security_Key`, while the code reads `SECRET_KEY` —
    environment variables are case-sensitive, so the value was never read and production
    signed session cookies with the public source-code default. Discovered incidentally
    during pre-deploy work, not by any check. Mitigated by creating `SECRET_KEY` with the
    exact name and a strong value; all existing sessions were invalidated, which is the
    documented safe failure mode. **The code remains fail-open** — a future install with the
    variable missing or misspelled would boot insecure and silent. A design to make it
    fail-closed is approved and pending implementation. 
  - `DATABASE_URL` — **FAIL-OPEN.** Defaults to local SQLite if unset (`database.py:27`); a misconfigured prod would silently run on an ephemeral file DB. 
  - `COOKIE_SECURE` — defaults **off** (`security.py:41`); must be set in prod. 
  - Square partial config — **FAIL-CLOSED (feature off)**. 
  - There is **no central startup validation** that refuses to boot on insecure prod config.
- Deployment: `Dockerfile` (py3.13-slim) + `docker-entrypoint.sh` runs `python -m app.bootstrap` (best-effort: `|| echo …continuing`) then `exec gunicorn app.main:app -k uvicorn.workers.UvicornWorker`. **Migrations run at app import (main.py:38-42) inside every worker** → additive/idempotent design is load-bearing under multi-worker boot. Health `/healthz` (main.py:94). Rollback strategy: NOT DOCUMENTED. Feature/activation gates: the Kitchen B2 activation gate is *designed* (`fired_items_without_tasks==0`) but **not yet an active flag** (B2 unimplemented).

---

## 21. Integrations / Vendor Dependency

| Integration | Purpose | Module | Auth | Timeout | Retry | Idempotency | Recovery | Evidence |
|---|---|---|---|---|---|---|---|---|
| Square Terminal | card-present charge | `services/square.py` (httpx, no SDK) | Bearer token (env) | 20s req / 120s poll | none (re-poll) | per-call uuid key (not stable) | re-poll same checkout | CODE |
| Receipt delivery | receipts | `services/receipt_delivery.py` | — | — | — | — | INFERRED local/print | needs deeper read |
| Email / SMS / maps / storage / analytics / external auth | — | NOT FOUND | — | — | — | — | — |

Vendor coupling: Square logic is centralized in one module but **called directly from the router** (no adapter interface) and there is **no persisted processor id**, so switching providers or reconciling against Square would require new code + schema. Portability of the app itself (container, SQLAlchemy) is otherwise good.

---

## 22. Time / Timezone / BusinessDay / Reporting (CODE-CONFIRMED)
- App forces **restaurant-local time**: `TZ` default `America/Vancouver`, `tzset()` on Unix (`main.py:20-24`). All timestamps use naive `datetime.now()` (local) — NOT timezone-aware, NOT UTC. Consequence: correctness depends on `TZ`; naive datetimes stored in DB. DST handled by OS tz.
- Timestamps present: `created_at, updated_at, opened_at, closed_at, fired_at, ready_at, served_at, sent_to_kitchen_at, voided_at, refunded_at`, reservation `at`, `DayClose` dates.
- **BusinessDay = calendar date** (`DayClose.date` unique) in local time; not a custom cutoff. Closeout = Z-report window (`closeout.py`).
- Reporting source of truth: **snapshot via ETL** into `models/star.py` fact tables (`etl.py`, `reports.py`, `analytics.py`); not live OLTP aggregates. `test_reconciliation.py` exercises OLTP→star→report totals (TEST-CONFIRMED pass).

---

## 23. Security / Privacy / Authorization (CODE-CONFIRMED)
- **Session:** signed cookie = `staff_id.HMAC-SHA256`, constant-time verify (`security.py:57-78`). No expiry/nonce; rotating `SECRET_KEY` logs everyone out (safe). Deactivated staff rejected every request (`deps.py:126`).
- **PIN:** PBKDF2-HMAC-SHA256, 200k rounds, 16-byte salt, constant-time compare; legacy plaintext accepted + upgrade-on-login (`security.py:85-123`). PINs are **hashed** (verified in code).
- **AuthZ:** static role→permission matrix (`deps.py:60`), backend `require(perm)` dependency on routes; unknown permission → empty set → **deny (fail-closed)**; refunds/discounts gated `discount.approve` (owner/manager); settings/staff gated owner. Frontend hiding is not relied upon for enforcement.
- **SECRET_KEY fail-open** (§20) — the one serious security finding. **NEW OBSERVATION — NOT PREVIOUSLY APPROVED (HIGH):** no production guard prevents booting with the public dev signing key; combined with `COOKIE_SECURE` off by default, a misconfigured deploy is forgeable. Recommend a startup assertion (do not implement now).
- **PCI boundary:** card capture is delegated to the Square terminal; app stores only `card_last4` + `card_brand` + (should but does not) a reference. No PAN/CVV received or stored (`square.py` docstring + CODE). Do not claim PCI compliance; boundary is delegated.
- **Privacy/PII:** reservation guest name/phone, staff name/PIN-hash/photo/wage, receipt payloads. No retention/deletion policy found. Not assessed for any legal regime.
- Redirect validation exists for kitchen boards (`_safe_next_board` allowlist, `sales.py:2138`). Jinja autoescape assumed on (default) — not exhaustively audited. Raw SQL is parameterized `text()` in `migrate.py`; no string-formatted SQL observed in the paths read.

---

## 24–26. UX / Responsiveness / Device / Accessibility / Terminology (LEVEL 3 — INVENTORY, largely NOT VERIFIED)
- Server-rendered Jinja across POS/KDS/Expo/Floor/Payment/Reservations/Admin (35 templates). Critical journeys dine-in / takeout / (partial) delivery / refund / reservation are IMPLEMENTED at the route level (CODE-CONFIRMED existence), but **operational UX, responsiveness, touch targets, and accessibility were NOT runtime-verified** (no app run, no device test). Classify device support as **NOT VERIFIED** across surfaces pending real inspection.
- Terminology inconsistency to watch (CODE/observed): "Preparing/Occupied" (floor iconography), "Void" vs "Refund" vs "Cancel", "Station/Kitchen Station". No redesign proposed.

---

## 27. Tables / Reservations / Channels (CODE-CONFIRMED / gaps)
- Tables: `RestaurantTable.status` (free/occupied), `current_waiter_id`, floor-map coordinates/shape/zone; move/merge/split audited (`_audit`, §28). **Open/seat not row-locked** (§18).
- Reservations: window/hold/auto-no-show model (`services/reservations.py`); table release on no-show. **No overlap/double-booking guard found** (CURRENT RISK / UNRESOLVED).
- Channels: `Channel.channel_type` distinguishes delivery; delivery-only instruments enforced at payment; delivery queue exists; full delivery dispatch lifecycle PARTIAL/NOT FOUND.

---

## 28. Observability / Auditability / Recovery (CODE-CONFIRMED)
- Logging: `print(...)` for migration status (`main.py:44`); no structured logging / metrics / tracing / alerting found.
- **Audit trail is narrow:** an `AuditEntry` table (`oltp.py:~1385`, `ix_audit_at`) is written **only** for floor table actions — `move_table`, `merge_tables`, `split_table` (via `_audit` `sales.py:557,595,676,762`). **Payments, voids, refunds, discounts, config/admin changes, and auth events are NOT written to it.** Actor attribution for money exists only on domain rows (`Payment.staff_id/voided_by_id`, `Refund`/`Discount.approved_by_id`). Application `print` logs are not a durable audit log. (CURRENT RISK — forensic gap for financial/config events.)
- Health: `/healthz` (liveness). No separate readiness endpoint; DB/migration readiness not exposed.

---

## 29. Query-Plan Evidence
**No runtime query-plan verification performed.** No EXPLAIN / EXPLAIN QUERY PLAN was run. Index *existence* is CODE-CONFIRMED; index *use* is not.

## 30. Maintainability / Evolution / Technical Debt (STRUCTURAL OBSERVATIONS)
- Orphan `.pyc` for a never-committed payment-provider layer (§6) — should be cleaned (not in this task).
- Domain/state logic split between services and large routers (`sales.py` is very large and owns the kitchen state machine + audit + table ops).
- No central config module; env reads scattered.
- Two Python runtimes (dev 3.14 / prod 3.13).
- Kitchen migration state: `OrderItem.station_id` = LEGACY COMPATIBILITY (TEMPORARY MIGRATION, retained until B-stage cleanup). PreparationTask reads = FUTURE (B2+).

## 31. Cost / Portability / Vendor Lock-in (LEVEL 3)
- Structural cost drivers: KDS polling, star-schema/ETL growth, Postgres history, multi-worker footprint. No forecasting.
- Lock-in: Square (no adapter, no stored ids), Neon/Postgres (standard SQL, portable), container portable. Meaningful data exports: routing CSV/XLSX (`admin.py`), reports/analytics (`analytics.py` — needs deeper read for full export list).

---

## 32. Testing & Evidence Boundary

Tests classified (CODE-CONFIRMED existence): unit (`test_money`), security (`test_security`), route/integration (`test_admin`, `test_stations`, `test_prep_tasks`, `test_reservations_map`, `test_floormap`, `test_happyhour*`, `test_schedule`, `test_upsell`), template (`test_templates`), reconciliation/ETL (`test_reconciliation`), live-server e2e (`test_e2e`).

**TEST EXECUTED this audit (command: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/<f>.py`; environment: local Windows, SQLite, cp314 venv):**

| Test | Result |
|---|---|
| test_money, test_security, test_stations, test_prep_tasks, test_floormap, test_reservations_map, test_happyhour, test_happyhour_sales, test_schedule, test_templates, test_upsell, test_reconciliation | **PASS (exit 0)** |
| **test_admin** | **FAIL (exit 1)** |
| test_e2e | **NOT RUN** — live-server harness (expects `uvicorn` on 127.0.0.1:8079) |

**NEW OBSERVATION — NOT PREVIOUSLY APPROVED (MEDIUM): `test_admin.py` is currently failing in the working tree.** Failing assertions: "Manage tab is hidden from the waiter's nav", then cascading "create table → id=None", "new table is free and active", "duplicate table number refused → 200" (traceback at `test_admin.py:150`). Backend authorization still PASSES (waiter is 403'd on `/admin/tables`, `/admin/staff`, `/admin/menu`, and cannot create a table) — so this is a **nav/UI + admin table-creation-flow regression, not a security regression**, most likely from the uncommitted `feat/floor-map` nav/admin changes. **Do not fix now** — flagged for the owner to decide.

Distinguish: many behaviors are TEST-EXECUTED-AND-PASSED above; payment-provider durability, PG catalog behavior, concurrency-under-true-parallelism, and all UI/device behavior are TEST-EXISTS-only or untested.

## 33. Environment Parity / Realism
- SQLite (dev/test) vs PostgreSQL (prod): real behavioral gap (locking, catalog, NULL/index semantics) — DOCUMENT-CONFIRMED ADR-010; the prep-task index has separate SQLite/PG verification paths but only SQLite was executed.
- Single worker (dev) vs multi-worker gunicorn (prod).
- Square sandbox (default) vs production.
- **No live PostgreSQL integration execution was performed.**
- **No production execution was performed. No production data was modified.**
- No real-device, browser-compat, load, or true-concurrency execution was performed.

---

## 34. Governance / Approval History

Repository artifacts: `docs/02_DECISIONS.md` (ADR-001…016, all Accepted, dated 2026-08-20), `docs/04_HISTORY.md`, `docs/05_AI_HANDOFF.md`, plus Kitchen Stations design/handoff/diff/review artifacts at repo root (v3–v9 handoffs+diffs, Stage B design, B2 v1/v2 design + v1 review package).

| Initiative | Stage | Exact Status | Evidence |
|---|---|---|---|
| Kitchen Stations | Stage A | **APPROVED / CLOSED** | 03_CURRENT_WORK; `KITCHEN_STATIONS_v3_HANDOFF.md` |
| Kitchen Stations | Stage B1 | **APPROVED / CLOSED** | 03_CURRENT_WORK; `KITCHEN_STATIONS_v9_HANDOFF.md` |
| Kitchen Stations | Stage B2 | **DESIGN REVISION REQUIRED** (v2 design now produced, **not yet approved**) | 03_CURRENT_WORK; `KITCHEN_STATIONS_STAGE_B2_v2_DESIGN.md` |
| Kitchen Stations | B2.1 / B2.2 impl | **NOT AUTHORIZED** | 03_CURRENT_WORK |
| Kitchen Stations | B3 (Expo/Floor) / B4 (modifier UI) | **NOT APPROVED / FUTURE** | 03_CURRENT_WORK |
| Payment / Security | (any staged remediation) | **UNKNOWN** — no payment/security stage records or ADRs exist in docs | absence in `docs/` |

Separation:
- **APPROVED BASELINE:** Stage A + B1; ADR-001…016; single-tenant/single-location.
- **CURRENT ACTIVE WORK:** B2 Design v2 awaiting audit (design only).
- **DEFERRED:** B2 impl, B3, B4, `OrderItem.station_id` removal, task SERVED, re-fire, WebSocket, multi-tenant.
- **BACKLOG / RISK:** normalized station-name uniqueness, dup import IDs, CSV formula injection, live-PG verification (docs) + the financial/security/concurrency findings in §13/§18/§20/§23/§28 (this audit).

---

## 35. Documentation Drift

The six docs exist and are internally consistent for Kitchen Stations, but drift/gaps:
- **03_CURRENT_WORK.md — STALE (minor):** lists "Produce B2 Design v2" as the next unchecked action, but `KITCHEN_STATIONS_STAGE_B2_v2_DESIGN.md` now exists (awaiting audit). Corrected wording: *"B2 Design v2 produced; awaiting independent review."*
- **Coverage gap (UNSUPPORTED/TOO-WEAK across 01/02):** the docs describe payments only as "depends on the existing lifecycle" and carry **no** architecture/decisions for the seat-ledger, Square terminal, refunds-are-record-only, tender model, financial arithmetic, environments/config, `SECRET_KEY` behavior, PIN hashing, the narrow audit trail, timezone model, or reporting/ETL. These are substantial implemented facts absent from the docs (see §39).
- **01_ARCHITECTURE deployment section — TOO-WEAK:** says "PostgreSQL production" but omits multi-worker gunicorn, migration-at-import, `/healthz`, and the Neon/pool specifics.
- No **INCORRECT** statements were found in the docs against current code; the issue is **omission**, not falsehood.

---

## 36. Known Risks / Backlog (evidence-supported)

| Risk | Severity | Evidence | Mitigation present | Residual | Destination |
|---|---|---|---|---|---|
| `SECRET_KEY` fail-open (forgeable sessions if unset in prod) | HIGH | `security.py:32,38` | dev-only intent; rotation logs out | no boot guard | 05_HANDOFF + 02_DECISIONS(ADR) + backlog |
| Card-terminal charge durability window (money w/o local record) | HIGH | `pay.py:556-571`, `square.py:182` | re-poll recovery | no durable attempt/reconciler | 01/02/03 + backlog |
| Card "refund" does not reverse at Square (record-only) | HIGH (operational) | `refunds.py` | drawer/report correct | card money not auto-returned | 01_ARCH + 05_HANDOFF |
| No processor id persisted (no card reconciliation/forensics) | MEDIUM | `oltp.py` Payment | card_last4/brand only | — | 01_ARCH + backlog |
| Table open not row-locked (occupancy race) | MEDIUM | `sales.py:441-490` | — | double-open | backlog (03) |
| Reservation double-booking (no overlap guard found) | MEDIUM | `services/reservations.py` (absence) | — | UNRESOLVED | backlog + focused audit |
| Audit trail covers only table moves/merges/splits | MEDIUM | `sales.py:_audit`, absence elsewhere | domain-row actor attribution | no financial/config audit | 01_ARCH + backlog |
| `test_admin.py` failing in working tree | MEDIUM | this audit §32 | authz intact | UI/table-creation regression | 03_CURRENT_WORK |
| `DATABASE_URL`/`COOKIE_SECURE` fail-open defaults | MEDIUM | `database.py:27`, `security.py:41` | — | prod misconfig risk | 05_HANDOFF |
| Orphan payment-provider `.pyc` | LOW | `__pycache__` | — | confusion | cleanup backlog |
| `pct()` float on money | LOW | `money.py:62` | integer elsewhere | negligible drift | note only |
| Normalized station-name uniqueness / dup import IDs / CSV formula injection | LOW–MED (DEFERRED) | DOCUMENT-CONFIRMED 03 | — | open | backlog |
| No live PostgreSQL verification | MEDIUM (evidence gap) | ADR-010 | structural review | unproven on PG | backlog |

---

## 37. Newly Discovered High-Risk Findings

All labeled **NEW OBSERVATION — NOT PREVIOUSLY APPROVED**, none fixed:
1. **SECRET_KEY fail-open (HIGH)** — §23/§20. Forgeable session cookies if prod boots without `SECRET_KEY`. Affected: `security.py`. No test. Next: add a production startup assertion (design + approve first).
2. **Card-terminal charge durability window (HIGH)** — §13. Square capture may leave no local record. Affected: `pay.py`, `square.py`. Next: decide on a persisted checkout/attempt + reconcile-on-load.
3. **Card refunds are record-only (HIGH, operational)** — §13. No Square reversal. Affected: `refunds.py`. Next: clarify intended operating procedure / whether provider refund is required.
4. **No processor id persisted (MEDIUM)** — §13/§16. Blocks card reconciliation & forensics.
5. **`test_admin.py` failing (MEDIUM)** — §32. UI/table-creation regression on the working branch; authz unaffected.

(Table-open concurrency, reservation overlap, and the narrow audit trail are also new-ish but lower/again-listed in §36.)

---

## 38. Traceability Matrix

| Durable Fact / Invariant | Code | Test | Approval | Confidence |
|---|---|---|---|---|
| READY ≠ SERVED | `sales.py`, `pay.py:430` | `test_prep_tasks`/`test_stations` (pass) | ADR-002 | HIGH |
| snapshot-at-fire | fire path `sales.py`, `oltp.py` | `test_prep_tasks` | ADR-003 | HIGH |
| Unassigned visibility | KDS `sales.py:1901-1980` | `test_stations` | ADR-004 | HIGH |
| explicit/no-heuristic routing | `admin.py`, fire path | `test_stations` | ADR-013 | HIGH |
| inactive station live-work reachable | `sales.py:1956-1963` | `test_stations` | ADR-015 | HIGH |
| PreparationTask multi-station model | `oltp.py`, fire path | `test_prep_tasks` | ADR-005 | HIGH |
| OrderItem.station_id compat retained | `oltp.py`, KDS | `test_stations` | ADR-006 | HIGH |
| OrderItem.kitchen_status compat/rollup | `sales.py:1628` | `test_prep_tasks` | ADR-007 | HIGH |
| KDS filter-before-limit | `sales.py:1895-1915` | `test_stations` | ADR-012 | HIGH |
| SQLite/PostgreSQL split | `database.py`, `migrate.py` | SQLite only | ADR-010 | MEDIUM (PG unproven) |
| polling KDS | KDS templates | — | ADR-011 | HIGH |
| fire-batch uniqueness | `oltp.py:1074`, `migrate.py` | `test_prep_tasks` | 03 (B1) | HIGH |
| Local payment durability + order-lock | `payments.py:318` | `test_reconciliation` | none (undoc) | HIGH |
| **PaymentAttempt durability (external)** | absent | none | none | **LOW / not implemented** |
| **payment idempotency (processor)** | absent | none | none | **LOW / not implemented** |
| **RefundAttempt lifecycle (provider)** | absent (record-only) | none | none | **LOW / not implemented** |
| **processor evidence stored** | absent | none | none | **LOW / not implemented** |
| **provider abstraction** | absent (single, in-router) | none | none | **LOW / not implemented** |
| production fail-closed config | FAIL-OPEN `security.py`,`database.py` | none | none | HIGH (that it is fail-open) |
| PIN hashing (PBKDF2) | `security.py:90-123` | `test_security` (pass) | none | HIGH |
| backend authorization | `deps.py:60,135` | `test_admin` authz cases (pass) | none | HIGH |
| single-tenant/single-location | models, docs | — | ADR-016 | HIGH |
| environment architecture (SQLite/PG, gunicorn) | `database.py`,`docker-entrypoint.sh` | — | none | MEDIUM |
| payment methods (cash/card/terminal/split/delivery) | `payments.py`,`pay.py` | `test_reconciliation` | none | HIGH |
| Square integration (Terminal API, sandbox default) | `square.py`,`pay.py` | none (no live) | none | MEDIUM |
| timezone model (local, America/Vancouver) | `main.py:20-24` | — | none | HIGH |
| reporting via star-schema ETL | `star.py`,`etl.py` | `test_reconciliation` | none | HIGH |
| multi-terminal assumption | `database.py`,`payments.py:318` | — | none | MEDIUM |
| current Kitchen B2 authorization boundary | 03_CURRENT_WORK | — | DOCUMENT | HIGH |

---

## 39. Durable Facts Missing From Persistent Documentation

Recommended canonical destination in parentheses:
- Seat-ledger payment model, `PaymentAllocation`, void vs refund, split/partial/multi-tender (**01_ARCHITECTURE**).
- **Refunds are record-only local reversals; card money is not auto-reversed at Square** (**01_ARCHITECTURE** + **05_AI_HANDOFF** as an operating caution).
- Money is integer cents; largest-remainder splitting; `pct()` float caveat (**01_ARCHITECTURE**).
- Square Terminal API integration, `is_configured()` gating, sandbox-default, **no processor id persisted**, charge durability window (**01_ARCHITECTURE** + **02_DECISIONS** as an ADR).
- Environment/config model: SQLite dev / Postgres(Neon) prod, gunicorn multi-worker, migrate-at-import, `/healthz`, config var names (**00_PROJECT_CONTEXT** for model, **01_ARCHITECTURE** for detail).
- **`SECRET_KEY` fail-open + `COOKIE_SECURE`/`DATABASE_URL` defaults** (**05_AI_HANDOFF** operational rule + **02_DECISIONS** if a fail-closed decision is adopted).
- Security model: HMAC session, PBKDF2 PIN hashing, role→permission matrix, refunds gated `discount.approve` (**01_ARCHITECTURE**).
- Audit trail is table-actions-only; financial/config events not audited (**01_ARCHITECTURE** + backlog).
- Timezone = restaurant-local, naive datetimes; BusinessDay = calendar date via `DayClose` (**01_ARCHITECTURE**).
- Reporting = star schema + ETL snapshots, not live aggregates (**01_ARCHITECTURE**).
- Governance: staged/audited slice process, six-doc context (**already partly in 02/05; keep**).

## 40. Candidate ADRs
| Proposed ADR | Evidence | Why ADR-worthy | Covered? |
|---|---|---|---|
| Money is integer minor units; largest-remainder splitting | `money.py`,`database.py` | durable, reversible-by-accident | NO |
| Card capture delegated to Square Terminal; app stores no PAN, only last4/brand | `square.py` | PCI boundary + vendor decision | NO |
| Refunds are local record-only reversals (no provider execution) | `refunds.py` | high-surprise operating rule | NO |
| Production must fail closed on `SECRET_KEY`/`DATABASE_URL` (if adopted) | `security.py`,`database.py` | security posture | NO (decision pending) |
| Reporting via star-schema ETL snapshots | `star.py`,`etl.py` | architecture-level | NO |
| Timezone = restaurant-local naive; BusinessDay = calendar date | `main.py`,`DayClose` | correctness-critical | NO |
(Do not turn the failing `test_admin`, orphan `.pyc`, or `pct()` float into ADRs — they are defects/backlog.)

## 41. Documentation Coverage Matrix (● documented · ○ missing/weak · today)
| Concern | 00 | 01 | 02 | 03 | 04 | 05 |
|---|---|---|---|---|---|---|
| Product scope / deployment model | ● | ● | ● | – | – | – |
| Kitchen / PreparationTask | ● | ● | ● | ● | ● | ● |
| Payments / tender / refunds | ○ | ○ | ○ | ○ | ○ | ○ |
| Financial arithmetic | ○ | ○ | ○ | – | – | – |
| Square / integrations | ○ | ○ | ○ | – | – | ○ |
| Environments / config / deploy | ○ | ○ | ○ | – | – | ○ |
| Security / PIN / SECRET_KEY | ○ | ○ | ○ | – | – | ○ |
| Authorization model | ○ | ● | ○ | – | – | ○ |
| Time / BusinessDay / reporting | ○ | ○ | ○ | – | – | – |
| Observability / audit / backup | ○ | ○ | ○ | – | – | ○ |
| Governance / AI rules | ● | ● | ● | ● | ● | ● |

---

## 42. Final Executive Summary

**A. Confirmed Stable Architecture (HIGH confidence)**
Single-tenant/single-location, server-rendered FastAPI + SQLAlchemy 2.0 + Jinja2; SQLite dev / Postgres prod; integer-cent money with a seat-ledger settlement model and order-row-lock concurrency; Kitchen Stations Stage A + B1 (snapshot-at-fire, explicit routing, Unassigned, filter-before-limit KDS, PreparationTask foundation with a verified fire-batch unique index); PBKDF2 PIN hashing, HMAC sessions, backend role→permission authorization (fail-closed on unknown permission); reporting via star-schema ETL; restaurant-local timezone; polling KDS.

**B. Approved Baselines** — Kitchen Stations Stage A (CLOSED), Stage B1 (CLOSED); ADR-001…016; single-tenant/single-location. No approved payment/security *stage* records exist.

**C. Current Active Work** — Kitchen Stations **B2 Design v2 (design only), awaiting independent review**. No B2 production code authorized.

**D. Deferred / Not Approved (do not represent as done)** — B2 implementation, B3/B4, `OrderItem.station_id` removal, task SERVED, re-fire, WebSocket/SSE, multi-tenant/location, payment-gate redesign; provider abstraction / PaymentAttempt / RefundAttempt / reconciliation engine (**never implemented** — orphan bytecode only).

**E. High-Risk Current Areas (evidence-supported)** — SECRET_KEY fail-open; card-terminal charge durability window; card refunds record-only; no persisted processor id; table-open concurrency; reservation overlap (unresolved); narrow audit trail; failing `test_admin`.

**F. Evidence Gaps** — No live PostgreSQL execution. No production execution/data change. No staging evidence. No performance benchmark or load test. No real-device / browser-compat validation. No true-parallel concurrency test (SQLite serializes). e2e suite not run (live-server). Several Level-2/3 areas (reservations overlap, delivery lifecycle, receipt delivery, full export inventory, accessibility) are INFERRED/UNRESOLVED and flagged for focused follow-up.

**G. Recommendation for the Six Persistent Docs**
- **00_PROJECT_CONTEXT.md — REVISE:** add deployment/runtime/env model and the non-Kitchen domain scope (payments, reporting, scheduling).
- **01_ARCHITECTURE.md — MAJOR REWRITE (additive):** it is Kitchen-only; add payments/tender/refund, financial arithmetic, Square/PCI boundary, environments/config, security/authz, audit trail, timezone, reporting/ETL.
- **02_DECISIONS.md — REVISE:** add the candidate ADRs in §40 (money units, Square/PCI, refunds record-only, reporting ETL, timezone/BusinessDay, and — if adopted — fail-closed config).
- **03_CURRENT_WORK.md — REVISE (minor):** mark B2 Design v2 produced/awaiting review; carry this audit's risk register.
- **04_HISTORY.md — KEEP/REVISE:** add the B2-v2 milestone and this architecture audit as a dated entry.
- **05_AI_HANDOFF.md — REVISE:** add operational-safety rules surfaced here (SECRET_KEY/COOKIE_SECURE/DATABASE_URL must be set in prod; card refunds are record-only; charge durability caveat; do not treat SQLite passes as PG proof).

---

*End of PROJECT_ARCHITECTURE_FACTS_FOR_DOCS.md — read-only audit; no code, docs, tests, schema, data, dependencies, or git state were modified.*
