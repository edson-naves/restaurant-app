# Kitchen Stations — v3 (audit round 3)

**Version:** v3 · **Scope:** two targeted polish fixes on top of v2.
**Pair:** this handoff + `KITCHEN_STATIONS_v3.diff` (full cumulative diff of the
feature as of v3). No feature redesign; the v1 findings and v2 fixes are not
revisited.

> Versioning: each audit round is a versioned pair — `KITCHEN_STATIONS_vN_HANDOFF.md`
> + `KITCHEN_STATIONS_vN.diff`. v1 = initial brief/design, v2 = the 9 first-audit
> fixes, v3 = this round. Future rounds → v4, v5, v6 (fresh pair each, history
> preserved, diff is always the full cumulative state at that version).

## Files changed in v3

- `app/routers/sales.py` — stricter `next` redirect validation.
- `app/routers/admin.py` — single station-name normalization used everywhere.
- `tests/test_stations.py` — updated/added tests for both.

## 1. `next` redirect — strict allowlist

`kitchen_status()` no longer uses `startswith`. New helper `_safe_next_board`
validates with `urllib.parse.urlsplit`:

- `scheme` empty, `netloc` empty, no backslash anywhere,
- `path` must be **exactly** `/kitchen` or `/expo`.

Query string is preserved (rebuilt via `urlunsplit`, fragment dropped); anything
invalid falls back to `/kitchen`.

Accepted: `/kitchen`, `/kitchen?x=1`, `/kitchen?station=unassigned`, `/expo`,
`/expo?view=mine`.
Rejected → `/kitchen`: `https://evil.com`, `//evil.com`, `/expo-whatever`,
`/kitchenFake`, `\evil`, `/\evil`, empty, `/orders/1`, `/kitchen/../x`.

## 2. Station-name normalization — centralized

New `_normalize_station_name(value) = value.strip().casefold()` is now the single
comparison key for: duplicate-name validation (create + edit), bulk-import
`by_name` build, bulk-import lookup, and the blank/"unassigned" sentinel. Before,
the dup check used `casefold()` while the importer used `lower()`; they now agree
and it's Unicode-correct. Stored names keep their original casing.

## Tests

- `test_safe_next_board_helper` — unit-tests every accept/reject case.
- `test_status_next_redirect_allowlist` — extended with `/expo-whatever`,
  `/kitchenFake`, `\evil` (all → `/kitchen`).
- `test_import_station_name_normalization` — `" grill "`, `"GRILL"`, `"Grill"`
  all resolve to one station; Unicode `"GRILLÉ"` → `"Grillé"` via casefold.

**Results:** `test_stations.py` all pass (23 cases); `test_floormap.py` and
`test_reservations_map.py` pass; `app.main` imports cleanly.

## Explicitly NOT changed

snapshot-at-fire · `OrderItem.station_id` history · `OrderItem.kitchen_status`
as source of truth · `pending → preparing → ready → served` · READY ≠ SERVED ·
payment gate · coursing · Unassigned bucket · KDS LIMIT 80 · Expo LIMIT 60 ·
inactive-station-with-fired-work visibility · routing to production stations only
· Stage B / `PreparationTask` direction.

## Deferred (still nice-to-have, out of v3)

DB-level unique constraint on station name · duplicate item ids within one import
· CSV formula-injection hardening.

Stopping at v3 for re-audit — not proceeding to Stage B.
