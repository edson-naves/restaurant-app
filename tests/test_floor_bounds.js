#!/usr/bin/env node
/* Floor Plan Builder — table-visible-bounds fix (fix/floor-table-visible-bounds).
 *
 * Unit tests for the pure geometry formula in web/static/floor_bounds.js
 * (visibleBounds). This is the numeric formula the browser-side drag code
 * relies on to keep every .map-chip fully inside .freemap — the one piece
 * of this fix that a text/markup-only structural check can't verify
 * (arithmetic, not presence). No framework, no DOM, no dependency: plain
 * Node assertions, same ok/FAIL-per-line convention this project's Python
 * test files (tests/test_*.py) already use, so this reads the same way in
 * CI/terminal output despite being a different language.
 *
 * Run: node tests/test_floor_bounds.js
 */
"use strict";

const path = require("path");
const { visibleBounds } = require(path.join(__dirname, "..", "web", "static", "floor_bounds.js"));

let failed = 0;
function check(cond, label) {
  console.log("  " + (cond ? "ok  " : "FAIL") + " " + label);
  if (!cond) failed++;
}
function approx(a, b, eps) {
  eps = eps === undefined ? 1e-9 : eps;
  return typeof a === "number" && typeof b === "number" && Math.abs(a - b) <= eps;
}

// --------------------------------------------------------------------------
// The formula itself, for a known map/table size — hand-computed, all four
// sides (top/left via minX/minY, right/bottom via maxX/maxY).
// --------------------------------------------------------------------------
(function test_formula_matches_hand_computed_values_all_four_sides() {
  console.log("- test_formula_matches_hand_computed_values_all_four_sides");
  // A 640x480 map, a 92x60 chip (a plausible real round/square chip size).
  const b = visibleBounds(640, 480, 92, 60);
  const expectMinX = (92 / 2 / 640) * 1000;   // halfTableWidth / mapWidth * 1000
  const expectMinY = (60 / 2 / 480) * 1000;   // halfTableHeight / mapHeight * 1000
  check(approx(b.minX, expectMinX), `minX (left edge) matches the formula (got ${b.minX}, expected ${expectMinX})`);
  check(approx(b.maxX, 1000 - expectMinX), `maxX (right edge) is 1000 - minX (got ${b.maxX}, expected ${1000 - expectMinX})`);
  check(approx(b.minY, expectMinY), `minY (top edge) matches the formula (got ${b.minY}, expected ${expectMinY})`);
  check(approx(b.maxY, 1000 - expectMinY), `maxY (bottom edge) is 1000 - minY (got ${b.maxY}, expected ${1000 - expectMinY})`);
  check(b.minX > 0 && b.minY > 0, "a real-size chip never gets a zero-width safe margin (the exact bug: raw 0/1000 was allowed before this fix)");
})();

// --------------------------------------------------------------------------
// Different table sizes must not share one bound — a bigger chip needs a
// bigger margin from every edge than a smaller one.
// --------------------------------------------------------------------------
(function test_different_table_sizes_get_different_bounds() {
  console.log("- test_different_table_sizes_get_different_bounds");
  const mapW = 800, mapH = 600;
  const small = visibleBounds(mapW, mapH, 72, 54);    // capacity-1 round/square, near the CSS floor
  const big = visibleBounds(mapW, mapH, 140, 90);      // capacity-20 round/square, near the CSS ceiling
  check(big.minX > small.minX, `a bigger table's minX is farther from the left edge (small=${small.minX}, big=${big.minX})`);
  check(big.maxX < small.maxX, `a bigger table's maxX is farther from the right edge (small=${small.maxX}, big=${big.maxX})`);
  check(big.minY > small.minY, `a bigger table's minY is farther from the top edge (small=${small.minY}, big=${big.minY})`);
  check(big.maxY < small.maxY, `a bigger table's maxY is farther from the bottom edge (small=${small.maxY}, big=${big.maxY})`);
})();

// --------------------------------------------------------------------------
// A large rect chip (the widest real shape, up to 190px per app.css) needs
// visibly more horizontal margin than the same capacity's round/square.
// --------------------------------------------------------------------------
(function test_large_rect_chip_gets_a_wider_horizontal_margin() {
  console.log("- test_large_rect_chip_gets_a_wider_horizontal_margin");
  const mapW = 800, mapH = 600;
  const roundOrSquare = visibleBounds(mapW, mapH, 129, 60);  // capacity-20 round/square width, from app.css's own formula (72 + 19*3)
  const rect = visibleBounds(mapW, mapH, 174, 60);           // capacity-20 rect width (129 * 1.35), same capacity, only the shape differs
  check(rect.minX > roundOrSquare.minX,
    `a large rect's minX sits farther from the left edge than round/square at the same capacity (round/square=${roundOrSquare.minX}, rect=${rect.minX})`);
  check(rect.maxX < roundOrSquare.maxX,
    `a large rect's maxX sits farther from the right edge than round/square at the same capacity (round/square=${roundOrSquare.maxX}, rect=${rect.maxX})`);
  check(approx(rect.minY, roundOrSquare.minY),
    "height is unaffected by the shape's own width stretch (minY identical for both)");
})();

// --------------------------------------------------------------------------
// The two persisted coordinates the production bug was actually reported
// against: a table stored at the raw edges (0,0) and (1000,1000).
// --------------------------------------------------------------------------
(function test_persisted_edge_coordinates_0_0_and_1000_1000_are_now_out_of_the_raw_range() {
  console.log("- test_persisted_edge_coordinates_0_0_and_1000_1000_are_now_out_of_the_raw_range");
  const b = visibleBounds(700, 500, 100, 70);
  check(b.minX > 0, `a persisted x=0 table is no longer inside the safe range at the raw value (minX=${b.minX} > 0)`);
  check(b.maxX < 1000, `a persisted x=1000 table is no longer inside the safe range at the raw value (maxX=${b.maxX} < 1000)`);
  check(b.minY > 0, `a persisted y=0 table is no longer inside the safe range at the raw value (minY=${b.minY} > 0)`);
  check(b.maxY < 1000, `a persisted y=1000 table is no longer inside the safe range at the raw value (maxY=${b.maxY} < 1000)`);
  // The clamp a caller applies (Math.max(min, Math.min(max, raw))) must
  // therefore actually move a raw-0/raw-1000 table off the true edge.
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  check(clamp(0, b.minX, b.maxX) === b.minX, "clamping a raw x=0 lands exactly on minX, not on the true edge (0)");
  check(clamp(1000, b.minX, b.maxX) === b.maxX, "clamping a raw x=1000 lands exactly on maxX, not on the true edge (1000)");
})();

// --------------------------------------------------------------------------
// Degenerate inputs: never throw, never divide by zero, never invert the
// range into something a caller's clamp() would silently mishandle.
// --------------------------------------------------------------------------
(function test_zero_or_missing_map_dimensions_fall_back_to_the_full_range() {
  console.log("- test_zero_or_missing_map_dimensions_fall_back_to_the_full_range");
  for (const [mw, mh, label] of [[0, 500, "zero map width"], [700, 0, "zero map height"], [0, 0, "both zero"]]) {
    let threw = false, b = null;
    try { b = visibleBounds(mw, mh, 92, 60); } catch (e) { threw = true; }
    check(!threw, `${label}: visibleBounds does not throw`);
    check(b && b.minX === 0 && b.maxX === 1000 && b.minY === 0 && b.maxY === 1000,
      `${label}: falls back to the uncorrected full [0,1000] range (got ${b ? JSON.stringify(b) : "N/A"})`);
  }
})();

(function test_negative_table_size_never_throws_or_inverts_the_range() {
  console.log("- test_negative_table_size_never_throws_or_inverts_the_range");
  let threw = false, b = null;
  try { b = visibleBounds(800, 600, -50, -30); } catch (e) { threw = true; }
  check(!threw, "a negative table size does not throw");
  check(b && b.minX === 0 && b.maxX === 1000 && b.minY === 0 && b.maxY === 1000,
    `a negative table size behaves like a zero-size one — no correction needed (got ${b ? JSON.stringify(b) : "N/A"})`);
})();

(function test_table_bigger_than_the_map_collapses_to_the_centre_not_an_inverted_range() {
  console.log("- test_table_bigger_than_the_map_collapses_to_the_centre_not_an_inverted_range");
  const b = visibleBounds(200, 150, 500, 400);  // chip much wider/taller than the map itself
  check(b.minX <= b.maxX, `minX never exceeds maxX even for an oversized chip (minX=${b.minX}, maxX=${b.maxX})`);
  check(b.minY <= b.maxY, `minY never exceeds maxY even for an oversized chip (minY=${b.minY}, maxY=${b.maxY})`);
  check(approx(b.minX, 500) && approx(b.maxX, 500), `an oversized chip collapses to the single centre point on X (got minX=${b.minX}, maxX=${b.maxX})`);
  check(approx(b.minY, 500) && approx(b.maxY, 500), `an oversized chip collapses to the single centre point on Y (got minY=${b.minY}, maxY=${b.maxY})`);
})();

// --------------------------------------------------------------------------
// Pure function: identical inputs always produce identical, freshly-computed
// output — no hidden state, no memoisation surprise.
// --------------------------------------------------------------------------
(function test_pure_deterministic_repeat_call() {
  console.log("- test_pure_deterministic_repeat_call");
  const a = visibleBounds(640, 480, 92, 60);
  const b = visibleBounds(640, 480, 92, 60);
  check(a !== b, "each call returns its own fresh object, not a shared/cached one");
  check(a.minX === b.minX && a.maxX === b.maxX && a.minY === b.minY && a.maxY === b.maxY,
    "identical inputs produce identical values on a second, independent call");
})();

if (failed) {
  console.log(`\n${failed} FAILED`);
  process.exit(1);
}
console.log("\nall floor-bounds tests passed");
