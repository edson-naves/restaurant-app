# Kitchen Stations (Stage A) — Audit Brief

You are auditing a feature added to an existing restaurant POS. Below is the
context, the design, the decisions, and what to scrutinize. The actual code is
in the companion file **`KITCHEN_STATIONS_CHANGES.diff`** (git diff).

---

## 1. System context (existing, unchanged)

- **Stack:** FastAPI + SQLAlchemy 2.0 (`Mapped`/`mapped_column`) + Jinja2,
  server-rendered. Dev DB = SQLite; prod = Postgres (Render).
- **Single-tenant, single-location.** There is **no** `Restaurant`/`Location`
  entity. `Channel` distinguishes dine_in / delivery / takeout.
- **No websockets/SSE.** "Real-time" is a 15s `setTimeout(location.reload)`
  poll on the kitchen board. This was intentionally reused, not replaced.
- **Order/kitchen model (pre-existing):** `OrderItem.kitchen_status`
  (`pending → preparing → ready → served`) is the **single source of truth** for
  kitchen state, read by the floor plan, the order page, and the payment gate
  (an order can't be paid until served). `OrderItem.course` (1/2/3) drives
  coursing; firing is per-course (`POST /orders/{id}/send`) or per-item.
- **Migrations:** additive-only, idempotent, guarded `ADD COLUMN` in
  `app/migrate.py` (`ADDED_COLUMNS`); `create_all` makes new tables. Records are
  **soft-deactivated** (`is_active`), never deleted, to preserve history.

## 2. Stage A scope (what was built)

A configurable **Station** entity; menu items route to stations; the kitchen
display filters by station; an **Expo** aggregated view; a compact per-station
strip on the floor plan; a bulk CSV/Excel routing importer. **No** change to the
kitchen status lifecycle or the payment gate.

## 3. Data model (see `oltp.py` in the diff)

- **`Station`** table: `name, code, description, type ('production'|'coordination'),
  color, icon, is_active, display_order, target_prep_min (nullable),
  printer_ref, kds_ref, visible_to_foh, visible_to_kitchen, created_at,
  updated_at`. `CheckConstraint` on `type`; index on `(is_active, display_order)`.
- **`MenuItem.station_id`** (nullable FK → station): the item's **routing config**.
  NULL = Unassigned.
- **`OrderItem.station_id`** (nullable FK → station): a **snapshot** written when
  the line is **fired** (not read live from MenuItem). NULL = fired while
  Unassigned.
- Migration: `station` via `create_all`; the two `station_id` columns via
  `ADDED_COLUMNS` (both nullable, no backfill — nothing auto-routed).

## 4. Key logic to review

- **Routing (manual):** items are assigned a station in the Menu editor
  (`/admin/menu`, `station_id` on create/edit) or in bulk. **No heuristic
  auto-routing** — ambiguous items stay Unassigned.
- **Snapshot at fire:** `send_to_kitchen` and `fire_item` set
  `item.station_id = item.menu_item.station_id`. Re-routing a menu item later
  must **not** move already-fired lines.
- **KDS station filter** (`GET /kitchen?station=...`): `all` = full board;
  `unassigned` = lines with NULL snapshot; else a station id. Tickets with no
  lines for the selected station are hidden — but the **Unassigned** tab
  guarantees un-routed lines are never lost.
- **Expo** (`GET /expo`, `_expo_courses`): per order → per course → per station;
  a station is ready only when all its lines are; a course is "waiting on X"
  until every station is ready, then "ready to serve". Coursing preserved (a
  held later course never blocks an earlier one). "Mark course ready" reuses the
  existing kitchen-status route with a same-site `next` redirect.
- **Floor plan strip** (`_order_station_status`): compact per-station ✓/⏳ on the
  occupied card. **Display only** — it does not change the card's aggregate
  `disp_status` (unchanged derivation).
- **Board limit:** `/kitchen` caps rendering at 80 orders (oldest-fired first)
  with a `COUNT` for "showing N of M"; `/expo` caps at 60. Guards against a
  pathological open-order set.
- **Bulk import** (`/admin/stations/routing/template` GET,
  `/admin/stations/routing/import` POST): download a CSV template
  (`item_id, category, item, price, station`), fill the `station` column, upload
  CSV **or .xlsx**. Header detection is tolerant (`Item ID`/`item_id`,
  `Station`). Matches station by name (case-insensitive, active); blank/
  "unassigned" clears; unknown stations & missing item ids are **reported, not
  applied**. The `.xlsx` reader is stdlib-only (`zipfile` + `ElementTree`) — no
  new dependency.
- **Station deactivation** re-routes its menu items to Unassigned (never a silent
  orphan); the UI confirms with the count first.

## 5. Decisions & constraints honored

- Snapshot at fire (not live read). Explicit Unassigned bucket, never hidden.
- No heuristic auto-route. READY ≠ SERVED (payment logic untouched).
- Coursing verified to pre-exist; Expo groups by course (no new "current course"
  field). Station fields conditional by type (production vs coordination).
- `backup_station_id`, `restaurant_id`/`location_id` **deferred** (no dead
  schema; single-tenant today).
- **Not built (Stage B, deliberately):** `PreparationTask` (one item → many
  stations via modifiers), modifier multi-routing, push/real-time.

## 6. Files changed

`app/models/oltp.py` (Station + station_id cols), `app/migrate.py` (2 columns),
`app/routers/admin.py` (station CRUD + bulk import + xlsx reader),
`app/routers/sales.py` (fire snapshot, KDS station filter, `/expo`, floor strip,
board limit), templates `admin_stations.html`, `expo.html`, and edits to
`admin_menu.html`, `admin_nav.html`, `base.html`, `kitchen.html`, `floor.html`,
`app.css`; tests `tests/test_stations.py` (16 cases).
*(Note: `oltp.py`, `migrate.py`, `sales.py`, `floor.html`, `app.css` also contain
unrelated reservation-feature edits from the same branch — focus on the
station-related hunks.)*

## 7. Please audit for

1. **Correctness** of the snapshot semantics (fired lines immune to re-routing),
   the KDS filter, and the Expo/floor aggregation (esp. coursing edge cases,
   empty/served items, mixed stations, the Unassigned bucket).
2. **Data integrity / migrations:** nullable columns, no backfill, soft-delete,
   FK safety on SQLite *and* Postgres; deactivation not orphaning items.
3. **Backward compatibility:** does anything change existing ordering, kitchen
   status derivation, the payment gate, or the floor plan aggregate status?
   (Intent: no.)
4. **Security / validation:** the bulk import (untrusted CSV/xlsx: zip bombs,
   malformed XML, huge files, injection, item_id/station validation), the
   `next` redirect (open-redirect?), permission gating (`settings` /
   `kitchen.view` / `kitchen.update`).
5. **Performance:** the board/expo limits; N+1 queries (station is
   `lazy="joined"` on OrderItem — is that right at scale?); the per-request
   `active_holds`/count queries.
6. **Edge cases & UX:** items routed to a since-deactivated station; a line
   fired while Unassigned then the item gets routed; multi-course tickets;
   stations with no fired lines; duplicate station names.
7. **Anything that would force a redesign** when Stage B (`PreparationTask`,
   modifier multi-routing) is added later.

Give findings by severity (blocker / should-fix / nice-to-have), each with the
file+function and a concrete failure scenario or suggested fix.
