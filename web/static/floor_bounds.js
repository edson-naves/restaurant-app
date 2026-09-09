/* Floor Plan Builder — pure geometry: the visible per-mille position range
 * for a table chip on the free map.
 *
 * `.map-chip` is centred on its (left, top) point
 * (`transform: translate(-50%, -50%)`), so a table stored/dragged to the
 * raw per-mille edges (0 or 1000) has half its rendered box pushed past the
 * map's own border — clipped by `.freemap`'s `overflow: hidden` (confirmed
 * visually in production after 9f063d6: a table at x=0 or y=0 renders half
 * off-map; a wide rect chip or a large capacity makes it worse, since half
 * its own (bigger) box is what goes missing). The fix: never let a chip's
 * *centre* — the stored map_x_per_mille/map_y_per_mille — sit closer to an
 * edge than half its own rendered size, i.e. clamp display position to a
 * per-mille range that keeps the whole box inside the map, not the raw
 * [0, 1000] the coordinate itself is stored in.
 *
 * Pure — no DOM, no globals read, no side effect — so this is the one place
 * the formula lives, exercised identically by the browser
 * (web/templates/admin_tables.html, via window.FloorTableBounds) and by a
 * plain `node tests/test_floor_bounds.js` run (via module.exports). Whoever
 * loads this decides which; the function itself never checks.
 */
(function (global) {
  "use strict";

  /**
   * Per-mille [minX, maxX] x [minY, maxY] range a table chip's *centre* may
   * occupy while keeping its whole rendered box inside a map of size
   * (mapWidth, mapHeight) — both in the same units (CSS pixels, e.g. from
   * two getBoundingClientRect() calls: the map's and the chip's own).
   *
   *   minX = halfTableWidth  / mapWidth  * 1000,  maxX = 1000 - minX
   *   minY = halfTableHeight / mapHeight * 1000,  maxY = 1000 - minY
   *
   * Degenerate inputs never throw or divide by zero:
   *   - mapWidth/mapHeight <= 0 (not yet laid out, hidden, or collapsed):
   *     returns the full, uncorrected [0, 1000] range — nothing real to
   *     compute a fraction of, so this falls back to old (unclamped)
   *     behaviour rather than fabricating a bound from bad data.
   *   - a table wider/taller than the map itself: the raw formula would
   *     push minX/minY past 500, leaving maxX/maxY < minX/minY (an
   *     inverted, empty range) — clamped to the single centre point (500)
   *     instead, so the chip is at least centred rather than unclampable.
   *   - a negative or missing table size: treated as 0 (half of 0 is 0),
   *     same as "no correction needed."
   */
  function visibleBounds(mapWidth, mapHeight, tableWidth, tableHeight) {
    if (!(mapWidth > 0) || !(mapHeight > 0)) {
      return { minX: 0, maxX: 1000, minY: 0, maxY: 1000 };
    }
    var halfW = Math.max(0, tableWidth || 0) / 2;
    var halfH = Math.max(0, tableHeight || 0) / 2;
    var minX = Math.min(500, (halfW / mapWidth) * 1000);
    var minY = Math.min(500, (halfH / mapHeight) * 1000);
    return { minX: minX, maxX: 1000 - minX, minY: minY, maxY: 1000 - minY };
  }

  var api = { visibleBounds: visibleBounds };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.FloorTableBounds = api;
  }
})(typeof window !== "undefined" ? window : this);
