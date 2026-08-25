# Kitchen Stations — Audit round 2 (fixes handoff)

All 9 findings from the first audit are addressed. Minimal changes only; the
Stage A design and the constraints below are preserved. Code is in the
regenerated **`KITCHEN_STATIONS_CHANGES.diff`**; new/updated tests are in
`tests/test_stations.py` (all passing, plus floor-map & reservation suites green).

## Constraints preserved (unchanged)

Snapshot-at-fire semantics · `OrderItem.kitchen_status` lifecycle · READY ≠
SERVED · the existing payment gate · existing coursing behaviour · Stage B
direction toward `PreparationTask`. No feature redesign.

## Findings → fixes → tests

1. **KDS: filters before LIMIT** — `kitchen_display` now applies view / mine /
   status / station **in SQL** (a `_view_mine` helper + an `EXISTS` subquery for
   the station), then `ORDER BY sent_to_kitchen_at LIMIT 80`. `board_total`
   counts the *same* filtered set (no LIMIT) → honest "showing N of M".
   `status_counts` and `station_line_counts` are cheap aggregate queries over the
   full board (view/mine applied). *Test:* `test_kds_filter_before_limit`
   (KITCHEN_LIMIT forced to 1; an Unassigned order that isn't the oldest still
   appears under its tab).

2. **Import safety limits** — untrusted upload is now bounded: raw file ≤ 5 MB
   (`file.read(cap+1)`); the stdlib xlsx reader rejects too many zip entries
   (`_XLSX_MAX_ENTRIES`), total/per-entry inflated size (anti zip-bomb), caps
   rows (`_IMPORT_MAX_ROWS`), columns (`_IMPORT_MAX_COLS`, cells beyond are
   ignored), string length (`_IMPORT_MAX_STR`), and stops absurd column refs in
   `_xlsx_col_index`. *Test:* `test_import_rejects_oversized`.

3. **Strict item_id parse** — `int(float(...))` replaced by `_parse_item_id`:
   accepts `"12"` and Excel's `"12.0"`, rejects `"12.5"`, scientific notation,
   signs, and non-positive. *Test:* `test_strict_item_id_parse`.

4. **Route only to active PRODUCTION stations** — `_routed_station` (create/edit
   item) and the bulk-import station map both require `is_active AND type ==
   production`; the menu-editor dropdown lists production stations only.
   *Test:* `test_routing_rejects_coordination_station`.

5. **No case-insensitive duplicate station names** — `_station_name_clash`
   (strip + casefold) blocks create/edit collisions. *Test:*
   `test_duplicate_station_name_rejected`. (DB-level unique constraint not added
   to avoid a migration on existing data; validation is transactional.)

6. **Deactivation preserves fired work** — `OrderItem` snapshots are untouched
   (deactivation only re-routes `MenuItem`s to Unassigned). The KDS station-tab
   set now includes **inactive** stations that still have preparing/ready lines,
   so their live work stays reachable. *Test:*
   `test_deactivate_keeps_kds_tab_and_snapshot`.

7. **Expo LIMIT in SQL** — `expo_display` applies view/mine in SQL and
   `ORDER BY sent_to_kitchen_at LIMIT 60`; a separate `COUNT` over the filtered
   set drives "shown of N". Coursing logic in `_expo_courses` unchanged.

8. **`next` redirect allowlist** — `kitchen_status` honours `next` only when it
   starts with `/kitchen` or `/expo` and contains no backslash; everything else
   (external URL, `//`, encoded) falls back to `/kitchen`. *Test:*
   `test_status_next_redirect_allowlist`.

9. **No global `lazy="joined"` on `OrderItem.station`** — both `OrderItem.station`
   and `MenuItem.station` are now `lazy="select"`; the KDS, Expo and floor
   queries `selectinload(Order.items).selectinload(OrderItem.station)`, and the
   menu editor `selectinload(MenuItem.station)` — so checkout/reports/other flows
   don't eager-load Station, and the station-using views avoid N+1.

## Please re-audit

Confirm the fixes are correct and complete, that no regression was introduced to
ordering / kitchen-status derivation / the payment gate / the floor aggregate,
and flag anything still open. Same severity format as before.
