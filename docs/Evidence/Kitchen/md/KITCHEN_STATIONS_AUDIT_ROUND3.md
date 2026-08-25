# Kitchen Stations — Audit round 3 (two targeted fixes)

Only the two requested polish items. No feature redesign, no revisiting the 9
round-1 findings. Code is in the regenerated `KITCHEN_STATIONS_CHANGES.diff`.

## Files changed

- `app/routers/sales.py` — stricter `next` redirect validation.
- `app/routers/admin.py` — single station-name normalization used everywhere.
- `tests/test_stations.py` — updated/added tests for both.

## 1. `next` redirect — strict allowlist

`kitchen_status()` no longer uses `startswith`. A new helper `_safe_next_board`
validates with `urllib.parse.urlsplit`:

- `scheme` must be empty,
- `netloc` must be empty,
- no backslash anywhere,
- `path` must be **exactly** `/kitchen` or `/expo`.

The query string is preserved (rebuilt via `urlunsplit`, fragment dropped); any
invalid value falls back to `/kitchen`.

Accepted: `/kitchen`, `/kitchen?x=1`, `/kitchen?station=unassigned`, `/expo`,
`/expo?view=mine`.
Rejected → `/kitchen`: `https://evil.com`, `//evil.com`, `/expo-whatever`,
`/kitchenFake`, `\evil`, `/\evil`, empty, `/orders/1`, `/kitchen/../x`.

## 2. Station-name normalization — centralized

New `_normalize_station_name(value) = value.strip().casefold()` in
`app/routers/admin.py`, now the single comparison key for:

- duplicate-name validation (`_station_name_clash`, create + edit),
- bulk-import `by_name` map build,
- bulk-import lookup,
- the blank/"unassigned" sentinel check.

Previously the dup check used `casefold()` while the importer used `lower()`.
Both now agree (and it's Unicode-correct via `casefold`). Stored names keep their
original casing — normalization is comparison-only.

## Tests added / changed

- `test_safe_next_board_helper` — unit-tests every accept/reject case above.
- `test_status_next_redirect_allowlist` — extended with `/expo-whatever`,
  `/kitchenFake`, `\evil` (all → `/kitchen`).
- `test_import_station_name_normalization` — `" grill "`, `"GRILL"`, `"Grill"`
  all resolve to the same station; a Unicode case (`"GRILLÉ"` → `"Grillé"`)
  matches via casefold.

## Test results

- `tests/test_stations.py` — **all pass** (23 cases, incl. the 3 new/updated).
- `tests/test_floormap.py` — all pass.
- `tests/test_reservations_map.py` — all pass.
- `app.main` imports cleanly.

## Explicitly NOT changed

snapshot-at-fire · `OrderItem.station_id` history · `OrderItem.kitchen_status`
as source of truth · `pending → preparing → ready → served` · READY ≠ SERVED ·
the payment gate · coursing · the Unassigned bucket · KDS LIMIT 80 · Expo LIMIT
60 · inactive-station-with-fired-work visibility · routing to production stations
only · the Stage B / `PreparationTask` direction. None of these were touched.

Not included (still nice-to-have, per instructions): DB-level unique constraint
on station name, duplicate item ids within one import, CSV formula-injection
hardening.

Stopping here for re-audit — not proceeding to Stage B.
