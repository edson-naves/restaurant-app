"""Floor Plan Builder — pure zone-rectangle placement geometry.

Shared by the live `POST /admin/floors/{id}/zones/create` route
(`app/routers/admin.py`) and the one-off zone-overlap backfill
(`app/migrate.py::_backfill_zone_overlap`), so "how do we place a zone
deterministically without colliding" is answered in exactly one place
rather than two copies that could drift.

Deliberately neutral: no import of a router, a migration module, an Engine,
or a Session. Every function here is pure — same inputs, same output, no
database access, no side effect — so a router (a live HTTP handler) and a
migration (a boot-time backfill) can both depend on it without either
pulling in the other's concerns, and it is trivially unit-testable on its
own.
"""
from __future__ import annotations

_ZONE_GRID_COLS = 3
_ZONE_GRID_ROWS = 3

# Phase 1 (grid/scatter) budget: enough to spread out the common case (a
# handful of zones) without pretending to be an exhaustive search — phase 2
# below is what actually proves a slot exists or does not.
_GRID_SCATTER_ATTEMPTS = 100

# Phase 2 (systematic sweep) budget: bounded by the number of positions that
# actually exist for a given rectangle size (see place_new_zone_rect), but
# never more than this — a hard ceiling so a pathological width/height pair
# still returns in well under a second rather than scanning a near-1000x1000
# grid one cell at a time. 200,000 tuple/set checks is a few tens of
# milliseconds in practice, and covers every rectangle size up to roughly
# half the canvas in both dimensions (legal_w * legal_h <= 200_000 already
# holds for width,height <= ~550 each) — comfortably past any real zone
# size, including the near-1000 sizes this fix was reviewed against
# (width or height as high as 961 leaves a legal range in the other
# dimension that this budget fully covers). Beyond the ceiling: treated as
# exhausted (a pragmatic bound, not a mathematical proof of impossibility) —
# availability over perfect coverage, same policy as the True impossible
# case below.
_SWEEP_BUDGET = 200_000


def _legal_range(width: int, height: int) -> tuple[int, int]:
    """Number of distinct legal x values and y values for a rectangle of
    this size inside the [0, 1000] canvas — i.e. how many positions
    pos_x/pos_y may each take so the rectangle still fits (pos + size <=
    1000). Both are always >= 1 for width, height in [1, 1000]."""
    return 1000 - width + 1, 1000 - height + 1


def _zone_slot_candidate(idx: int, width: int, height: int) -> tuple[int, int]:
    """Deterministic (pos_x, pos_y) candidate for the idx-th (0-based) zone
    being placed on one floor, at the given rectangle size.

    The first 3x3 = 9 indexes use a plain grid — recognisably "spread out"
    for the common case of a handful of zones. Beyond index 8, two
    different odd multipliers scatter x and y so later indexes keep landing
    on new points instead of collapsing back onto the same few values once
    clamped to the canvas. This function only has to keep offering new
    candidates; place_new_zone_rect is what actually guarantees no
    collision, by testing each candidate against the real occupied set.
    """
    if idx < _ZONE_GRID_COLS * _ZONE_GRID_ROWS:
        col, row = idx % _ZONE_GRID_COLS, idx // _ZONE_GRID_COLS
        x, y = 40 + col * 320, 40 + row * 330
    else:
        x, y = 40 + (idx * 97) % 560, 40 + (idx * 131) % 640
    x = max(0, min(1000 - width, x))
    y = max(0, min(1000 - height, y))
    return x, y


def place_new_zone_rect(
    taken: set[tuple[int, int, int, int]], start_idx: int, width: int, height: int,
) -> tuple[int, int, int, int] | None:
    """First (pos_x, pos_y, width, height) whose rectangle is not already in
    `taken` — the exact rect of every zone this placement must not exactly
    coincide with — or `None` if none exists.

    Two phases, both deterministic (no randomness) and bounded (no
    uncontrolled scan of the full [0,1000] x [0,1000] space):

      1. Grid/scatter (`_zone_slot_candidate`), starting at `start_idx`, for
         up to `_GRID_SCATTER_ATTEMPTS` tries — cheap, and already spreads
         zones out nicely for the common case (a handful of zones on a
         floor).
      2. A systematic sweep of the rectangle's actual legal position space
         (`_legal_range`) — every (x, y) with 0 <= x <= 1000-width and
         0 <= y <= 1000-height, visited in a fixed row-by-row order — up to
         `_SWEEP_BUDGET` checks. This is exhaustive for any realistic zone
         size (including a size as large as 961x961, reviewed as a real
         near-impossible case) and is what correctly proves a slot exists
         when the grid/scatter phase's small, fixed pattern happens not to
         find one.

    Returns `None` when no free rectangle was found within this function's
    configured budget. Precisely, that means one of two things, and this
    function deliberately does not distinguish which to its caller:

      - **proven impossible** — `legal_w * legal_h <= _SWEEP_BUDGET`, so
        the sweep phase actually visited every legal position for this
        exact size and every one was already taken. The canonical example:
        width = height = 1000 leaves exactly one legal position, (0, 0);
        if that one is occupied, there is mathematically no other
        rectangle of that size distinct from it.
      - **budget exhausted, not proven impossible** — `legal_w * legal_h`
        exceeds `_SWEEP_BUDGET` (an extreme size: larger than roughly half
        the canvas in both dimensions), so the sweep stopped before
        covering every legal position. A free rectangle could still exist
        beyond what was checked; this function simply did not look that
        far, by design (see `_SWEEP_BUDGET`'s own comment) — availability
        of a bounded, fast, always-terminating function takes priority
        over exhaustive coverage of an unrealistic size.

    Either way, `None` is a safe, ordinary return value, never an error —
    there is nothing to distinguish because the caller's obligation is
    identical for both: treat `None` as "no free rectangle was found for
    this zone right now", leave that zone's rectangle completely
    unchanged, and report it as unresolved. Never raise merely because no
    position was found — see `app.migrate._backfill_zone_overlap` for the
    boot-safe policy this feeds into.

    Deterministic across SQLite and PostgreSQL by construction: this
    function never touches a database or a dialect-specific type, only
    Python ints and a set of tuples supplied by the caller.
    """
    idx = start_idx
    for _ in range(_GRID_SCATTER_ATTEMPTS):
        x, y = _zone_slot_candidate(idx, width, height)
        candidate = (x, y, width, height)
        if candidate not in taken:
            return candidate
        idx += 1

    legal_w, legal_h = _legal_range(width, height)
    checked = 0
    for y in range(legal_h):
        for x in range(legal_w):
            if checked >= _SWEEP_BUDGET:
                return None
            candidate = (x, y, width, height)
            if candidate not in taken:
                return candidate
            checked += 1
    return None
