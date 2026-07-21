# Restaurant Management System — v1.0

Built to `restaurant_management_requirements_v3.docx` (Requirements Specification & Workflow, June 2026).
FastAPI + SQLAlchemy + SQLite, server-rendered UI. Covers the three modules in
priority order: Sales & Orders, Payment Processing, Reports & Analytics.
Inventory is out of scope for v1.0, per section 7.

## Run

```bash
cd restaurant_app
.venv/Scripts/python -m app.seed            # reference data + 6 weeks of history
.venv/Scripts/python -c "from app.database import SessionLocal; from app.etl import run_etl; run_etl(SessionLocal(), full_refresh=True)"
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000>. Switch the acting user (and therefore role) from
the dropdown in the header.

## Tests

```bash
python tests/test_templates.py        # every template compiles
node tests/js/test_floor_search.js    # floor plan search rules (needs Node)
python tests/test_admin.py            # setup module: guards, permissions, price parsing
python tests/test_money.py            # split arithmetic invariants
python tests/test_reconciliation.py   # star schema ties back to OLTP
python tests/test_e2e.py              # workflows 6.1 / 4.2.5 against a running server
```

`test_e2e.py` needs the server up on the port set in its `BASE` constant.

## The two schemas

The system deliberately carries **two** models of the same business.

### 1. OLTP — `app/models/oltp.py`

Normalized, written during service. Source of truth.

`staff` · `restaurant_table` · `channel` · `menu_category` · `menu_item` ·
`modifier` · `payment_instrument` · `order` · `seat` · `order_item` ·
`order_item_modifier` · `shared_item_share` · `delivery_order` · `discount` ·
`payment` · `payment_allocation` · `receipt`

The load-bearing table is **`payment_allocation`**. Requirement 4.2.2 says every
order item must be linked to the specific instrument that paid for it, and that
one order may be settled with several instruments ("2 items on Visa, 1 in
Cash"). An allocation is one row per *payment × order item*, so that link is
stored, not inferred.

### 2. Star schema — `app/models/star.py`

Read by every report. Never written to during service.

**Dimensions**

| Dimension | Type | Notes |
|---|---|---|
| `dim_date` | static | key `YYYYMMDD` |
| `dim_time` | static | minute grain; carries `service_period` and `shift` (4.3.4) |
| `dim_staff` | **SCD 2** | a promotion must not rewrite last month's attribution |
| `dim_menu_item` | **SCD 2** | a repricing must not rewrite last month's revenue |
| `dim_table` | SCD 1 | layout changes are corrections |
| `dim_channel` | SCD 1 | dine-in / own driver / UberEats / DoorDash (4.3.5) |
| `dim_payment_instrument` | SCD 1 | pre-computed `method_group` for the 4.3.3 rollup |

**Facts** — grain is the contract, so it is declared explicitly:

| Fact | Grain | Serves |
|---|---|---|
| `fact_order_item` | one row per order item | 4.3.2 best sellers |
| `fact_payment` | one row per payment allocation | 4.3.3 payment breakdown |
| `fact_order_header` | one row per closed order | 4.3.1, 4.3.4, 4.3.5 |

`fact_order_item` and `fact_payment` are separate on purpose. A single item can
be paid by more than one instrument (4.2.2), so merging them would either double
count revenue or lose the instrument detail.

Every dimension reserves key `-1` as *Unknown / Not applicable*, so facts never
carry a NULL foreign key — a delivery order has no table, a third-party order
has no waiter.

## Design decisions worth knowing

**Money is integer cents, everywhere.** Floats cannot represent 0.10, and a
till that drifts a cent will not reconcile against the drawer (workflow 6.3
step 2). All rounding happens in one place, `app/services/money.py::distribute()`,
which uses the largest-remainder method so a split always sums to exactly the
input. `$10.00` across 3 guests is `3.34 / 3.33 / 3.33` — never three times
`3.33` with a cent unaccounted for.

**The seat is the unit of billing, not the order.** Section 4.2.4 makes every
seat an independent payer, so `build_ledgers()` computes each seat's claim:
whole line totals for its own items, plus a proportional share of anything
shared. Payment draws down those claims and writes an allocation per line.

**Revenue keys on settlement date, not order date.** `fact_order_header` carries
both (`date_key` = opened, `close_date_key` = settled). An order opened 23:30 and
paid 00:45 belongs to the second day's takings, because that is the day the cash
entered the drawer. Reports key on the close date so they tie to the end-of-day
reconciliation; the peak-hours chart keys on the *open* time, because that is a
demand curve for staffing.

**Reports never touch the OLTP tables.** Each report is one fact scanned against
small dimensions, so a new filter is a `WHERE` clause rather than another join
through the operational model.

**No raw card numbers are stored** (section 5) — brand and last 4 only.

**Setup data is deactivated, never deleted** (`routers/admin.py`). A table,
staff member or menu item that has ever appeared on an order is referenced by
that order, its payments, its receipts and the star-schema dimensions built
from them. Deleting the row would strand all of it and silently rewrite
historical reports, so `is_active` is flipped instead: gone from the floor plan
and the order screen, still resolvable by anything looking backwards. The
module refuses to retire a table with a live order or a waiter still holding
open ones, and will not let the last active owner lock themselves out —
`staff.manage` and `settings` are owner-only, so there would be no way back in.

**Schema changes go in `app/migrate.py`.** `Base.metadata.create_all()` creates
missing tables but never alters existing ones, so a column added to a model
after the database was seeded is silently absent until a query fails on it.
`migrate.run()` applies idempotent ADD COLUMNs at startup — additive only.

## Bugs found by the tests during the build

Recorded because each one is a class of error worth guarding against:

1. **$1,764 of sold food linked to no payment instrument** — a violation of
   4.2.2. Shared-item shares were written with `db.add()` instead of through the
   relationship, so the parent's collection stayed stale and settlement skipped
   those items. `test_reconciliation.py` caught it; the seed now asserts rather
   than silently dropping value.
2. **Every page 500** — Starlette ≥1.3 removed the legacy
   `TemplateResponse(name, context)` signature. Versions are now pinned.
3. **Payment screen 500** — an unclosed `{% if %}`. `test_templates.py` now
   compiles all templates so this cannot hide behind an unexercised route.
4. **$65.25 missing from the default report range** — not a bug in the model but
   a real gap: the reporting window was derived from order dates only, so
   payments taken after midnight on the last day fell outside it. This is what
   prompted the `close_date_key` role-playing date above.

## Requirements coverage

| Req | Where |
|---|---|
| 4.1.1 floor plan, table states, waiter assignment | `routers/sales.py`, `web/templates/floor.html` |
| 4.1.2 order creation, modifiers, send to kitchen | `routers/sales.py`, `order.html` |
| 4.1.3 kitchen display, colour-coded urgency | `routers/sales.py::kitchen_display`, `kitchen.html` |
| 4.1.4 delivery, platform refs, driver assignment | `routers/sales.py::delivery_queue`, `delivery.html` |
| 4.2.1 instruments (incl. delivery-only guard) | `models/oltp.py::PaymentInstrument`, `services/payments.py::_validate_instrument` |
| 4.2.2 item↔instrument linkage, multi-instrument | `models/oltp.py::PaymentAllocation` |
| 4.2.3 split equally, sub-receipt per guest | `services/payments.py::split_equally` |
| 4.2.4 seat-level payers, shared items, live balance | `services/payments.py::build_ledgers`, `balance_panel` |
| 4.2.5 partial order close | `services/payments.py::pay_seat(is_partial_close=True)` |
| 4.2.6 tips, discounts w/ manager approval | `routers/pay.py`, `models/oltp.py::Discount` |
| 4.2.7 receipts | `services/payments.py::_issue_receipt`, `receipt.html` |
| 4.3.1–4.3.5 reports | `app/reports.py`, `routers/analytics.py` |
| 4.3.3 CSV export | `routers/analytics.py::export_csv` |
| Section 3 roles | `app/deps.py::PERMISSIONS` |
| Setup: tables, staff, menu (owner only) | `routers/admin.py`, `admin_tables.html`, `admin_staff.html`, `admin_menu.html` |
| 6.1 / 6.2 / 6.3 workflows | `tests/test_e2e.py` walks 6.1 and 4.2.5 |

## Not built (deliberate)

Out of scope per section 7: inventory, customer-facing ordering, loyalty,
payroll, accounting integration, multi-location.

Simplified, and would need real work before go-live:

- **Authentication.** The role *permission* model is real and enforced
  (`app/deps.py`); the identity check is a cookie, not a PIN pad or session.
- **UberEats / DoorDash are modelled, not integrated.** Orders, platform refs and
  settlement all work; there is no Merchant API client (assumption in section 8).
- **Payment terminals.** No card-reader integration; instruments are recorded,
  not authorized against a PSP.
- **ETL is run on demand** (button on the Reports screen) rather than scheduled.
  Section 6.3 step 6 wants a midnight archive job.
