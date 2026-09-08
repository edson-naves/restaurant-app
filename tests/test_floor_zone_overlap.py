"""Floor Plan Builder — zone-overlap fix.

Diagnosis (docs/03_CURRENT_WORK.md — "Floor Plan Builder production
diagnosis"): every zone (old and new) was created without an explicit
position, so `Zone.pos_x/pos_y/width/height` all default to the same fixed
(60, 60, 360, 300). Any two zones neither has ever been dragged/resized land
on the literal same rectangle, and the admin builder's drop hit-test
(`document.elementsFromPoint`, web/templates/admin_tables.html) can then only
ever resolve to ONE of them — the other becomes permanently unreachable by
drag. Confirmed against a real production report (table 6 reassigning to
Patio/Window but never to Main/Bar).

Second pass, addressing independent review of the first fix:

  A. app/services/zone_geometry.py (NEW) — the pure placement geometry
     (`place_new_zone_rect` and its helpers), moved out of app/migrate.py
     into a neutral module with no import of a router, a migration, an
     Engine, or a Session — imported symmetrically by both. Redesigned to
     try a grid/scatter first, then a bounded systematic sweep of the
     rectangle's actual legal position space, and to return `None` (never
     raise) when a size genuinely leaves no distinct rectangle available.
  B. app/migrate.py::_backfill_zone_overlap — availability over perfect
     geometry: a zone `place_new_zone_rect` cannot place is left completely
     unchanged and reported as unresolved; every other group is still
     processed; startup never halts for this. The narrower fail-closed
     RuntimeError now fires only for a genuine internal defect (a zone the
     function should have moved or flagged but silently didn't).
  C. web/static/app.css — rect's own min-width/max-width, so it no longer
     inherits round/square's max-width:140px and loses its visual
     distinction at high capacity.
  D. app/routers/admin.py::create_zones — locks the Floor row
     (SELECT ... FOR UPDATE on PostgreSQL) before computing `taken`, so two
     concurrent creates on the same floor can't both land on the same
     rectangle. SQLite: documented and tested for what it actually is (no
     row lock — `.with_for_update()` compiles to a no-op there), not
     overclaimed.

SQLite always; against a disposable PostgreSQL database only when BOTH
PG_TEST_DSN names one AND ALLOW_DESTRUCTIVE_PG_TESTS=1 is set — reusing the
exact guard from tests/test_floor_map_coordinates.py (never a second copy of
that safety logic). Never touches Render/Neon.

Run: python tests/test_floor_zone_overlap.py
     PG_TEST_DSN=postgresql+psycopg://user:pass@host/some_test_db \
     ALLOW_DESTRUCTIVE_PG_TESTS=1 python tests/test_floor_zone_overlap.py
"""
import os
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _env  # noqa: F401  — declares the test opt-out before app imports
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app import migrate
from app.database import Base, get_db
from app.deps import current_staff
from app.main import app
from app.migrate import ZoneOverlapResult, _backfill_zone_overlap
from app.models import oltp  # noqa: F401  register every table on Base.metadata
from app.models.oltp import Floor, RestaurantTable, Staff, Zone
from app.services import zone_geometry
from app.services.zone_geometry import _legal_range, place_new_zone_rect
from tests._pay_fixture import pg_dsn
from tests.test_floor_map_coordinates import assert_disposable_postgres_target

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)
    return cond


# --------------------------------------------------------------------------
# SQLite session (ORM, for the route-level tests) and SQLite engine (raw
# connection, for the migrate.py backfill tests) — same throwaway-file
# pattern as every other Floor Plan Builder test file.
# --------------------------------------------------------------------------

def _sqlite_engine():
    path = os.path.join(tempfile.gettempdir(), f"zoneoverlap_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    return engine


def _session():
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _seed_owner_and_floor(db, name="Main"):
    owner = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
    floor = Floor(name=name)
    db.add_all([owner, floor])
    db.flush()
    return owner, floor


def _client(db, staff):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


def _rect(z: Zone) -> tuple:
    return (z.pos_x, z.pos_y, z.width, z.height)


# ==========================================================================
# A1 — app/services/zone_geometry.py: neutral module, pure functions
# ==========================================================================

def test_zone_geometry_module_is_neutral_and_place_new_zone_rect_lives_there():
    check(not hasattr(migrate, "_zone_slot_candidate"),
          "app.migrate no longer defines the geometry helper itself (moved out — "
          "place_new_zone_rect remains importable there only because migrate.py "
          "itself imports it from the neutral module for internal use, checked below)")
    check(callable(zone_geometry.place_new_zone_rect), "app.services.zone_geometry defines place_new_zone_rect")

    import re
    src = open(zone_geometry.__file__, encoding="utf-8").read()
    import_lines = [ln for ln in src.splitlines() if ln.strip().startswith(("import ", "from "))]
    check(import_lines == ["from __future__ import annotations"],
          f"zone_geometry.py's only import is the future-annotations one — no real dependency at all "
          f"(got {import_lines})")
    for forbidden in ("app.routers", "app.migrate", "sqlalchemy", "Session"):
        check(not any(re.search(rf"\b{re.escape(forbidden)}\b", ln) for ln in import_lines),
              f"no import line in zone_geometry.py references {forbidden!r}")

    import app.routers.admin as admin_mod
    check(admin_mod.place_new_zone_rect is zone_geometry.place_new_zone_rect,
          "admin.py imports the same function object from the neutral module")
    check(migrate.place_new_zone_rect is zone_geometry.place_new_zone_rect,
          "migrate.py imports the same function object from the neutral module too "
          "(used internally by _backfill_zone_overlap)")
    src_migrate = open(migrate.__file__, encoding="utf-8").read()
    check("from app.services.zone_geometry import place_new_zone_rect" in src_migrate,
          "migrate.py's import is explicit and traceable to the neutral module")


# ==========================================================================
# A2 — place_new_zone_rect: grid/scatter, systematic sweep, impossible = None
# ==========================================================================

def test_place_new_zone_rect_961_finds_alternative_position():
    # width = height = 961 (the review's own "> 960" threshold) — legal
    # range is only 40x40, but that is still 1600 distinct positions; the
    # anchor's own rect is the only one taken, so an alternative must exist.
    anchor = (0, 0, 961, 961)
    result = place_new_zone_rect({anchor}, 0, 961, 961)
    check(result is not None, "961x961 with only the anchor taken: a free rectangle is found")
    check(result != anchor, f"the found rectangle is distinct from the anchor's (got {result})")
    check(result[2:] == (961, 961), "width/height are preserved exactly")


def test_place_new_zone_rect_near_1000_asymmetric_requires_the_sweep():
    # width near-max, height normal: the grid/scatter phase's fixed offsets
    # (multiples of 320/330, scattered by *97/*131) can plausibly all clamp
    # into the narrow 40-wide legal x-range and collide; the systematic
    # sweep phase must still find the (rare) free spot.
    w, h = 961, 300
    legal_w, legal_h = _legal_range(w, h)
    # Occupy every legal position except exactly one.
    all_positions = {(x, y, w, h) for x in range(legal_w) for y in range(legal_h)}
    hole = next(iter(all_positions))
    taken = all_positions - {hole}
    result = place_new_zone_rect(taken, 0, w, h)
    check(result == hole, f"the one remaining legal position is found (got {result}, expected {hole})")


def test_place_new_zone_rect_1000x1000_impossible_returns_none_not_exception():
    raised = False
    try:
        result = place_new_zone_rect({(0, 0, 1000, 1000)}, 0, 1000, 1000)
    except Exception:  # noqa: BLE001
        raised = True
        result = "N/A"
    check(not raised, "no exception is raised for the mathematically impossible case")
    check(result is None, f"None is returned instead (got {result!r})")


def test_place_new_zone_rect_fully_exhausted_near_max_also_returns_none():
    w, h = 961, 300
    legal_w, legal_h = _legal_range(w, h)
    taken = {(x, y, w, h) for x in range(legal_w) for y in range(legal_h)}
    result = place_new_zone_rect(taken, 0, w, h)
    check(result is None, f"every legal position occupied -> None (got {result!r})")


def test_place_new_zone_rect_deterministic_same_inputs_same_output():
    taken = {(60, 60, 360, 300)}
    r1 = place_new_zone_rect(taken, 1, 360, 300)
    r2 = place_new_zone_rect(set(taken), 1, 360, 300)  # a fresh, equal set
    check(r1 == r2, f"identical taken set + start_idx always returns the identical rect (got {r1} vs {r2})")


# ==========================================================================
# B1 — POST /admin/floors/{id}/zones/create: deterministic, non-colliding
# ==========================================================================

def test_create_zones_batch_gets_distinct_rects():
    db = _session()
    owner, floor = _seed_owner_and_floor(db)
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/floors/{floor.id}/zones/create", data={"names": "A, B, C"},
               follow_redirects=False)
    check(r.status_code == 303, f"batch create redirects (got {r.status_code})")

    zones = db.execute(select(Zone).where(Zone.floor_id == floor.id)).scalars().all()
    check(len(zones) == 3, f"3 zones created (got {len(zones)})")
    rects = [_rect(z) for z in zones]
    check(len(set(rects)) == 3, f"all 3 rectangles are pairwise distinct (got {rects})")
    for z in zones:
        check(0 <= z.pos_x <= 1000 - z.width and 0 <= z.pos_y <= 1000 - z.height,
              f"zone {z.name} stays fully inside [0,1000] (got {_rect(z)})")
    app.dependency_overrides.clear()
    db.close()


def test_create_zones_avoids_existing_zone_when_first_candidate_collides():
    """Review finding (LOW, test coverage): the previous version of this
    test passed even with `if candidate not in taken` removed, because
    idx = len(taken) already happened to avoid the one pre-existing zone by
    construction of the starting index, not by the collision check itself.

    This seeds the pre-existing zone at EXACTLY the rectangle that the
    naive "start at idx = len(taken)" candidate would produce — so if the
    collision check were removed, the new zone would land exactly on top of
    it. With the check in place, the search must advance past that
    candidate to the next one.
    """
    db = _session()
    owner, floor = _seed_owner_and_floor(db)
    # With one pre-existing zone, idx will start at 1. Seed the existing
    # zone at exactly what _zone_slot_candidate(1, 360, 300) produces, so
    # the very first candidate the search tries is already taken.
    from app.services.zone_geometry import _zone_slot_candidate
    x0, y0 = _zone_slot_candidate(1, 360, 300)
    existing = Zone(name="Existing", floor_id=floor.id, pos_x=x0, pos_y=y0, width=360, height=300)
    db.add(existing)
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/floors/{floor.id}/zones/create", data={"names": "New"},
               follow_redirects=False)
    check(r.status_code == 303, f"create redirects (got {r.status_code})")
    new_zone = db.execute(select(Zone).where(Zone.name == "New")).scalar_one()
    check(_rect(new_zone) != (x0, y0, 360, 300),
          f"the new zone does NOT land on the first candidate the naive index would try "
          f"(got {_rect(new_zone)}, that candidate was {(x0, y0, 360, 300)}) — proves the "
          "collision check, not just the starting index, is what avoided it")
    db.refresh(existing)
    check(_rect(existing) == (x0, y0, 360, 300), "the pre-existing zone itself is untouched")
    app.dependency_overrides.clear()
    db.close()


def test_create_zones_different_floors_are_independent():
    db = _session()
    owner, floor_a = _seed_owner_and_floor(db, "Floor A")
    _, floor_b = _seed_owner_and_floor(db, "Floor B")
    db.commit()
    c = _client(db, owner)

    c.post(f"/admin/floors/{floor_a.id}/zones/create", data={"names": "A1"}, follow_redirects=False)
    c.post(f"/admin/floors/{floor_b.id}/zones/create", data={"names": "B1"}, follow_redirects=False)

    a1 = db.execute(select(Zone).where(Zone.name == "A1")).scalar_one()
    b1 = db.execute(select(Zone).where(Zone.name == "B1")).scalar_one()
    check(_rect(a1) == _rect(b1),
          f"floor A's and floor B's first zone land on the same first slot, "
          f"independently (got {_rect(a1)} vs {_rect(b1)})")
    app.dependency_overrides.clear()
    db.close()


def test_create_zones_locks_the_floor_row_sqlite_noop_documented():
    """Not a concurrency proof on SQLite (see the PostgreSQL section below
    for the real one) — confirms the lock statement itself does not raise
    or change behaviour on SQLite, where `.with_for_update()` compiles to a
    no-op (no FOR UPDATE syntax exists for this dialect)."""
    db = _session()
    owner, floor = _seed_owner_and_floor(db)
    db.commit()
    c = _client(db, owner)
    r = c.post(f"/admin/floors/{floor.id}/zones/create", data={"names": "A"}, follow_redirects=False)
    check(r.status_code == 303, f"create still works normally on SQLite despite the lock call (got {r.status_code})")
    app.dependency_overrides.clear()
    db.close()


# ==========================================================================
# B2 — app/migrate.py::_backfill_zone_overlap (SQLite call-site)
# ==========================================================================

def _seed_backfill_scenario(engine):
    """Two active zones on one floor sharing the exact model default rect
    (60, 60, 360, 300) — precisely what a never-dragged zone looks like —
    plus one distinct zone on the same floor that must be left alone, and a
    second, independent floor untouched by any of it."""
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    floor = Floor(name="Main")
    other_floor = Floor(name="Other")
    db.add_all([floor, other_floor])
    db.flush()

    dup1 = Zone(name="Main", floor_id=floor.id, sort_order=0)          # default rect
    dup2 = Zone(name="Bar", floor_id=floor.id, sort_order=1)           # same default rect
    distinct = Zone(name="Patio", floor_id=floor.id, sort_order=2,
                     pos_x=500, pos_y=0, width=250, height=250)
    elsewhere = Zone(name="Untouched", floor_id=other_floor.id, sort_order=0)  # also default rect
    db.add_all([dup1, dup2, distinct, elsewhere])
    db.flush()

    placed_table = RestaurantTable(number=1, zone_id=dup2.id, capacity=4,
                                    map_x_per_mille=100, map_y_per_mille=150)
    null_table = RestaurantTable(number=2, zone_id=dup2.id, capacity=4)  # never placed
    inactive_table = RestaurantTable(number=3, zone_id=dup2.id, capacity=4, is_active=False,
                                      map_x_per_mille=999, map_y_per_mille=999)
    db.add_all([placed_table, null_table, inactive_table])
    db.commit()
    ids = {
        "floor": floor.id, "other_floor": other_floor.id,
        "dup1": dup1.id, "dup2": dup2.id, "distinct": distinct.id, "elsewhere": elsewhere.id,
        "placed_table": placed_table.id, "null_table": null_table.id,
        "inactive_table": inactive_table.id,
    }
    db.close()
    return ids


def test_backfill_two_and_three_way_groups_deterministic_and_anchor_preserved():
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)
    ids = _seed_backfill_scenario(engine)

    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    floor3 = Floor(name="Third")
    db.add(floor3)
    db.flush()
    trio = [Zone(name=f"T{i}", floor_id=floor3.id, sort_order=i) for i in range(3)]
    db.add_all(trio)
    db.commit()
    trio_ids = sorted(z.id for z in trio)
    db.close()

    with engine.begin() as conn:
        result = _backfill_zone_overlap(conn)
    check(isinstance(result, ZoneOverlapResult), "returns a ZoneOverlapResult")
    check(result.unresolved == [], f"nothing unresolved in this fully-resolvable scenario (got {result.unresolved})")
    check(len(result.moved) == 3, f"1 (2-way group) + 2 (3-way group) = 3 moved (got {len(result.moved)}: {result.moved})")

    db = Session()
    dup1, dup2 = db.get(Zone, ids["dup1"]), db.get(Zone, ids["dup2"])
    distinct = db.get(Zone, ids["distinct"])
    elsewhere = db.get(Zone, ids["elsewhere"])
    trio_rows = [db.get(Zone, tid) for tid in trio_ids]

    check(_rect(dup1) == (60, 60, 360, 300), "2-way group: the anchor (lowest id) stays at its original rect")
    check(_rect(dup2) != (60, 60, 360, 300), "2-way group: the other zone was moved off the shared rect")
    check(dup2.width == 360 and dup2.height == 300, "2-way group: width/height preserved exactly for the moved zone")
    check(_rect(distinct) == (500, 0, 250, 250), "a genuinely distinct zone on the same floor is untouched")
    check(_rect(elsewhere) == (60, 60, 360, 300),
          "the coincident zone on the OTHER floor is untouched by this floor's fix")

    check(_rect(trio_rows[0]) == (60, 60, 360, 300), "3-way group: the anchor stays at its original rect")
    trio_rects = [_rect(z) for z in trio_rows]
    check(len(set(trio_rects)) == 3, f"3-way group: all 3 zones end up pairwise distinct (got {trio_rects})")
    for z in trio_rows[1:]:
        check(z.width == 360 and z.height == 300, f"3-way group: zone {z.name} keeps its original width/height")

    engine2 = _sqlite_engine()
    Base.metadata.create_all(engine2)
    ids2 = _seed_backfill_scenario(engine2)
    with engine2.begin() as conn2:
        _backfill_zone_overlap(conn2)
    db2 = sessionmaker(bind=engine2, future=True)()
    check(_rect(db2.get(Zone, ids2["dup2"])) == _rect(dup2),
          "re-running from an identical starting state reproduces the identical result (dup2)")
    db2.close()
    db.close()


def test_backfill_moves_tables_proportionally_and_respects_null_inactive_policy():
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)
    ids = _seed_backfill_scenario(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    dup2_before = _rect(db.get(Zone, ids["dup2"]))
    placed_before = (db.get(RestaurantTable, ids["placed_table"]).map_x_per_mille,
                      db.get(RestaurantTable, ids["placed_table"]).map_y_per_mille)
    grid_pos_before = (db.get(RestaurantTable, ids["placed_table"]).pos_x,
                        db.get(RestaurantTable, ids["placed_table"]).pos_y)
    db.close()

    with engine.begin() as conn:
        _backfill_zone_overlap(conn)

    db = Session()
    dup2_after = db.get(Zone, ids["dup2"])
    check(_rect(dup2_after) != dup2_before, "sanity: dup2 actually moved")
    dx = dup2_after.pos_x - dup2_before[0]
    dy = dup2_after.pos_y - dup2_before[1]

    placed = db.get(RestaurantTable, ids["placed_table"])
    expected = (max(0, min(1000, placed_before[0] + dx)), max(0, min(1000, placed_before[1] + dy)))
    check((placed.map_x_per_mille, placed.map_y_per_mille) == expected,
          f"the placed table's map position translated by the same delta as its zone "
          f"(got {(placed.map_x_per_mille, placed.map_y_per_mille)}, expected {expected})")
    check((placed.pos_x, placed.pos_y) == grid_pos_before,
          "the table's OLD grid pos_x/pos_y columns are never touched by this backfill")

    null_table = db.get(RestaurantTable, ids["null_table"])
    check(null_table.map_x_per_mille is None and null_table.map_y_per_mille is None,
          "a never-placed (NULL) table stays NULL — nothing real to translate")

    inactive = db.get(RestaurantTable, ids["inactive_table"])
    check((inactive.map_x_per_mille, inactive.map_y_per_mille) == (999, 999),
          "an inactive table's map position is never read or written by this backfill")
    db.close()


def test_backfill_is_idempotent():
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)
    ids = _seed_backfill_scenario(engine)

    with engine.begin() as conn:
        first = _backfill_zone_overlap(conn)
    check(len(first.moved) > 0, "first run does real work")

    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    snapshot = {z.id: _rect(z) for z in db.execute(select(Zone)).scalars().all()}
    table_snapshot = {
        t.id: (t.map_x_per_mille, t.map_y_per_mille, t.pos_x, t.pos_y)
        for t in db.execute(select(RestaurantTable)).scalars().all()
    }
    db.close()

    with engine.begin() as conn:
        second = _backfill_zone_overlap(conn)
    check(second.moved == [] and second.unresolved == [],
          f"second run changes nothing (got moved={second.moved}, unresolved={second.unresolved})")

    db = Session()
    same_zones = all(_rect(db.get(Zone, zid)) == rect for zid, rect in snapshot.items())
    same_tables = all(
        (t.map_x_per_mille, t.map_y_per_mille, t.pos_x, t.pos_y) == vals
        for t, vals in ((db.get(RestaurantTable, tid), vals) for tid, vals in table_snapshot.items())
    )
    check(same_zones, "no zone row changed between the first and second run")
    check(same_tables, "no table row changed between the first and second run")
    db.close()


def test_backfill_impossible_group_is_unresolved_not_raised():
    """The HIGH finding: two zones sized to the full 1000x1000 canvas have
    exactly one legal position between them. The backfill must report this
    as unresolved and leave both zones alone — never raise, never halt."""
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    floor = Floor(name="Impossible")
    db.add(floor)
    db.flush()
    z0 = Zone(name="Z0", floor_id=floor.id, sort_order=0, pos_x=0, pos_y=0, width=1000, height=1000)
    z1 = Zone(name="Z1", floor_id=floor.id, sort_order=1, pos_x=0, pos_y=0, width=1000, height=1000)
    db.add_all([z0, z1])
    db.commit()
    z0_id, z1_id = z0.id, z1.id
    db.close()

    raised = False
    try:
        with engine.begin() as conn:
            result = _backfill_zone_overlap(conn)
    except Exception:  # noqa: BLE001
        raised = True
        result = None
    check(not raised, "the impossible 1000x1000 group does not raise")
    check(result is not None and result.moved == [], "nothing was (falsely) reported as moved")
    check(result is not None and len(result.unresolved) == 1,
          f"exactly one unresolved entry is reported (got {result.unresolved if result else None})")
    if result:
        check(str(z1_id) in result.unresolved[0] or str(z0_id) in result.unresolved[0],
              "the unresolved log line names the affected zone")

    db = Session()
    z0_after, z1_after = db.get(Zone, z0_id), db.get(Zone, z1_id)
    check(_rect(z0_after) == (0, 0, 1000, 1000), "the anchor zone is completely unchanged")
    check(_rect(z1_after) == (0, 0, 1000, 1000),
          "the impossible zone is left EXACTLY as it was — not resized, not moved")
    db.close()


def test_backfill_resolvable_and_impossible_groups_together():
    """A resolvable group and a mathematically-impossible group on
    different floors in the SAME run: the resolvable one must still be
    corrected; the impossible one must still only be reported, not raised."""
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    floor_ok = Floor(name="OK")
    floor_bad = Floor(name="Impossible")
    db.add_all([floor_ok, floor_bad])
    db.flush()
    ok = [Zone(name=f"OK{i}", floor_id=floor_ok.id, sort_order=i) for i in range(2)]
    bad = [
        Zone(name=f"Bad{i}", floor_id=floor_bad.id, sort_order=i, pos_x=0, pos_y=0, width=1000, height=1000)
        for i in range(2)
    ]
    db.add_all(ok + bad)
    db.commit()
    ok_ids = sorted(z.id for z in ok)
    bad_ids = sorted(z.id for z in bad)
    db.close()

    with engine.begin() as conn:
        result = _backfill_zone_overlap(conn)
    check(len(result.moved) == 1, f"the resolvable group is still corrected (got {result.moved})")
    check(len(result.unresolved) == 1, f"the impossible group is still reported (got {result.unresolved})")

    db = Session()
    check(_rect(db.get(Zone, ok_ids[0])) == (60, 60, 360, 300), "OK group: anchor unchanged")
    check(_rect(db.get(Zone, ok_ids[1])) != (60, 60, 360, 300), "OK group: the other zone moved")
    check(_rect(db.get(Zone, bad_ids[0])) == (0, 0, 1000, 1000), "Bad group: anchor unchanged")
    check(_rect(db.get(Zone, bad_ids[1])) == (0, 0, 1000, 1000), "Bad group: unresolved zone unchanged")
    db.close()


def test_backfill_unresolved_log_has_no_credentials():
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    floor = Floor(name="F")
    db.add(floor)
    db.flush()
    db.add_all([
        Zone(name="A", floor_id=floor.id, sort_order=0, pos_x=0, pos_y=0, width=1000, height=1000),
        Zone(name="B", floor_id=floor.id, sort_order=1, pos_x=0, pos_y=0, width=1000, height=1000),
    ])
    db.commit()
    db.close()

    with engine.begin() as conn:
        result = _backfill_zone_overlap(conn)
    check(len(result.unresolved) == 1, "sanity: one unresolved entry produced")
    line = result.unresolved[0].lower()
    for forbidden in ("password", "://", "dsn", "secret", "token", "@"):
        check(forbidden not in line, f"unresolved log line does not contain {forbidden!r} (line: {result.unresolved[0]!r})")


def test_backfill_genuine_internal_defect_still_raises_and_rolls_back():
    """A real defect (place_new_zone_rect lying about having resolved a
    collision — always returning some OTHER zone's exact rect instead of a
    genuinely free one) must still halt startup and roll back everything in
    this transaction, including an earlier group's already-issued UPDATE —
    distinguishing "we chose not to fix an impossible case" (silent, safe)
    from "this function is broken" (loud, fail-closed).

    The lying rect is deliberately DIFFERENT from the group's own original
    position (not just a same-value no-op UPDATE), so a successful rollback
    is observationally provable: if the transaction did not roll back, the
    moved zone would be found at the lying position; since it rolls back,
    it is found back at its true original position instead.
    """
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    floor_a = Floor(name="A - fixable")
    floor_b = Floor(name="B - will be sabotaged")
    db.add_all([floor_a, floor_b])
    db.flush()
    fixable = [Zone(name=f"A{i}", floor_id=floor_a.id, sort_order=i) for i in range(2)]
    # A third, distinct zone on floor A, same size as the group being
    # de-collided (360x300 — the backfill only ever writes pos_x/pos_y, so
    # a lying rect can only create a genuine duplicate if the size already
    # matches). The lying function's fabricated "free" position always
    # matches this one exactly, so the mover it moves ends up in a NEW,
    # observably different collision instead of a same-value no-op.
    bait = Zone(name="Bait", floor_id=floor_a.id, sort_order=2, pos_x=500, pos_y=0, width=360, height=300)
    sabotaged = [Zone(name=f"B{i}", floor_id=floor_b.id, sort_order=i) for i in range(2)]
    db.add_all(fixable + [bait] + sabotaged)
    db.commit()
    fixable_ids = sorted(z.id for z in fixable)
    bait_rect_before = _rect(bait)
    check(floor_a.id < floor_b.id, "sanity: floor A's group is processed first")
    db.close()

    def _lying_place_new_zone_rect(taken, start_idx, width, height):
        return (500, 0, width, height)  # the bait zone's exact position+size — a fabricated "free" slot

    raised = False
    with patch("app.migrate.place_new_zone_rect", _lying_place_new_zone_rect):
        try:
            with engine.begin() as conn:
                _backfill_zone_overlap(conn)
        except RuntimeError:
            raised = True
    check(raised, "a genuine internal defect (a lying placement function) still raises")

    db = Session()
    a0, a1 = db.get(Zone, fixable_ids[0]), db.get(Zone, fixable_ids[1])
    bait_after = db.get(Zone, bait.id)
    check(_rect(a0) == (60, 60, 360, 300), "floor A's anchor is untouched (it was never a mover)")
    check(_rect(a1) == (60, 60, 360, 300),
          f"floor A's mover is back at its TRUE original position, not the lying function's fabricated "
          f"one — proves the already-issued UPDATE was rolled back, not left applied (got {_rect(a1)})")
    check(_rect(bait_after) == bait_rect_before, "the bait zone itself was never written to")
    db.close()


def test_creating_a_new_zone_after_backfill_does_not_recreate_coincidence():
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)
    ids = _seed_backfill_scenario(engine)
    with engine.begin() as conn:
        _backfill_zone_overlap(conn)

    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    owner = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
    db.add(owner)
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/floors/{ids['floor']}/zones/create", data={"names": "Newest"},
               follow_redirects=False)
    check(r.status_code == 303, f"create after backfill redirects (got {r.status_code})")

    all_rects = [_rect(z) for z in db.execute(
        select(Zone).where(Zone.floor_id == ids["floor"], Zone.is_active.is_(True))
    ).scalars().all()]
    check(len(all_rects) == len(set(all_rects)),
          f"every active zone on the floor still has a distinct rect after the new one joins "
          f"(got {all_rects})")
    app.dependency_overrides.clear()
    db.close()


def test_migrate_run_wires_the_backfill_end_to_end():
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)
    _seed_backfill_scenario(engine)
    applied = migrate.run(engine)
    check(any("de-collided" in line for line in applied),
          f"migrate.run() surfaces the zone-overlap backfill's moved work (got {applied})")


def test_migrate_run_completes_with_an_impossible_case_present():
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    floor = Floor(name="Impossible")
    db.add(floor)
    db.flush()
    db.add_all([
        Zone(name="A", floor_id=floor.id, sort_order=0, pos_x=0, pos_y=0, width=1000, height=1000),
        Zone(name="B", floor_id=floor.id, sort_order=1, pos_x=0, pos_y=0, width=1000, height=1000),
    ])
    db.commit()
    db.close()

    raised = False
    applied = []
    try:
        applied = migrate.run(engine)
    except Exception:  # noqa: BLE001
        raised = True
    check(not raised, "migrate.run() completes — the app can boot — even with an impossible group present")
    check(any("UNRESOLVED" in line for line in applied),
          f"the boot log surfaces the unresolved zone (got {applied})")


# ==========================================================================
# B3 — legacy/corrupted-data defense: NULL/invalid width or height must
# never crash the backfill or the boot.
#
# `Zone.width`/`Zone.height` are NOT NULL at the normal ORM schema level —
# confirmed directly (Zone.__table__.c.width.nullable is False) — so this
# data can never arrive through this codebase's own write paths. It is
# reachable only through something outside them: a hand-run migration, an
# external tool, a pre-hardening artifact. Since the real `zone` table
# enforces NOT NULL on both dialects (verified directly — a raw INSERT with
# NULL width raises IntegrityError on SQLite), these scenarios use a
# deliberately permissive, disposable "legacy" schema — floor + zone only,
# built from scratch with plain DDL, no NOT NULL anywhere — never the real
# production schema/constraint, never Base.metadata's own zone table. This
# mirrors the same disposable-schema-manipulation pattern
# tests/test_floor_map_coordinates.py already uses (_drop_map_columns).
# `restaurant_table` is deliberately never created in this schema — that is
# exactly what makes `_backfill_zone_overlap`'s own `tables_exist` guard
# make this safe: with no restaurant_table, table-movement code never runs,
# so nothing here depends on FK-compatible fixture setup for it.
# ==========================================================================

def _legacy_permissive_engine():
    """A from-scratch, deliberately permissive floor+zone schema — no
    NOT NULL, no CHECK, no FK — standing in for a legacy/corrupted database
    outside this codebase's normal write paths. Never touches Base.metadata
    or any real constraint."""
    engine = _sqlite_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE floor (id INTEGER PRIMARY KEY, name VARCHAR(60), is_active BOOLEAN)"
        ))
        conn.execute(text(
            "CREATE TABLE zone (id INTEGER PRIMARY KEY, floor_id INTEGER, name VARCHAR(60), "
            "sort_order INTEGER, is_active BOOLEAN, pos_x INTEGER, pos_y INTEGER, "
            "width INTEGER, height INTEGER)"
        ))
    return engine


def _insert_legacy_zone(conn, zid, floor_id, x, y, w, h, active=True):
    # `active` is a real Python bool (not 0/1): SQLite's dynamic typing
    # accepts an int for its BOOLEAN-affinity column either way, but
    # PostgreSQL's real boolean type rejects an integer literal outright
    # (DatatypeMismatch) — a bool lets SQLAlchemy's parameter binding adapt
    # correctly per dialect, on both the from-scratch legacy schema
    # (SQLite only) and the real, relaxed-constraint one (PostgreSQL too).
    conn.execute(text(
        "INSERT INTO zone (id, floor_id, name, sort_order, is_active, pos_x, pos_y, width, height) "
        "VALUES (:id, :fid, :name, 0, :active, :x, :y, :w, :h)"
    ), {"id": zid, "fid": floor_id, "name": f"Z{zid}", "active": active, "x": x, "y": y, "w": w, "h": h})


def test_backfill_null_width_group_is_unresolved_not_crashed():
    engine = _legacy_permissive_engine()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO floor (id, name, is_active) VALUES (1, 'F', 1)"))
        _insert_legacy_zone(conn, 1, 1, 0, 0, None, 300)
        _insert_legacy_zone(conn, 2, 1, 0, 0, None, 300)

    raised = False
    result = None
    try:
        with engine.begin() as conn:
            result = _backfill_zone_overlap(conn)
    except Exception:  # noqa: BLE001
        raised = True
    check(not raised, "a NULL width does not raise TypeError/int(None) anywhere")
    check(result is not None and result.moved == [], "nothing is (falsely) reported as moved")
    check(result is not None and len(result.unresolved) == 2,
          f"both zones in the NULL-width group are reported unresolved (got {result.unresolved if result else None})")

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, pos_x, pos_y, width, height FROM zone ORDER BY id")).fetchall()
    check(rows == [(1, 0, 0, None, 300), (2, 0, 0, None, 300)],
          f"both zones are left byte-for-byte unchanged — not resized, not defaulted (got {rows})")


def test_backfill_null_height_group_is_unresolved_not_crashed():
    engine = _legacy_permissive_engine()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO floor (id, name, is_active) VALUES (1, 'F', 1)"))
        _insert_legacy_zone(conn, 1, 1, 0, 0, 360, None)
        _insert_legacy_zone(conn, 2, 1, 0, 0, 360, None)

    raised = False
    result = None
    try:
        with engine.begin() as conn:
            result = _backfill_zone_overlap(conn)
    except Exception:  # noqa: BLE001
        raised = True
    check(not raised, "a NULL height does not raise TypeError/int(None) anywhere")
    check(result is not None and len(result.unresolved) == 2,
          f"both zones in the NULL-height group are reported unresolved (got {result.unresolved if result else None})")


def test_backfill_invalid_dimension_values_zero_negative_over_1000():
    cases = [
        ("width 0", dict(w=0, h=300)),
        ("height negative", dict(w=360, h=-50)),
        ("width over 1000", dict(w=1500, h=300)),
        ("height over 1000", dict(w=360, h=1500)),
    ]
    for label, dims in cases:
        engine = _legacy_permissive_engine()
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO floor (id, name, is_active) VALUES (1, 'F', 1)"))
            _insert_legacy_zone(conn, 1, 1, 0, 0, dims["w"], dims["h"])
            _insert_legacy_zone(conn, 2, 1, 0, 0, dims["w"], dims["h"])

        raised = False
        result = None
        try:
            with engine.begin() as conn:
                result = _backfill_zone_overlap(conn)
        except Exception:  # noqa: BLE001
            raised = True
        check(not raised, f"{label}: does not raise")
        check(result is not None and len(result.unresolved) == 2,
              f"{label}: both zones reported unresolved (got {result.unresolved if result else None})")
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT width, height FROM zone ORDER BY id")).fetchall()
        check(rows == [(dims["w"], dims["h"])] * 2, f"{label}: dimensions left completely untouched (got {rows})")


def test_backfill_invalid_group_alongside_valid_group_both_handled_correctly():
    """The exact scenario the review asked for: an invalid-dimension group
    and a normal, resolvable group in the SAME run. The invalid one stays
    intact/unresolved; the valid one is still corrected."""
    engine = _legacy_permissive_engine()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO floor (id, name, is_active) VALUES (1, 'F', 1)"))
        # Valid, resolvable 2-way group at the real default rect.
        _insert_legacy_zone(conn, 1, 1, 60, 60, 360, 300)
        _insert_legacy_zone(conn, 2, 1, 60, 60, 360, 300)
        # Invalid group elsewhere on the same floor.
        _insert_legacy_zone(conn, 3, 1, 500, 0, None, 250)
        _insert_legacy_zone(conn, 4, 1, 500, 0, None, 250)

    with engine.begin() as conn:
        result = _backfill_zone_overlap(conn)
    check(len(result.moved) == 1, f"the valid group is still corrected (got {result.moved})")
    check(len(result.unresolved) == 2, f"the invalid group is still reported, both members (got {result.unresolved})")

    with engine.connect() as conn:
        z1, z2, z3, z4 = conn.execute(
            text("SELECT pos_x, pos_y, width, height FROM zone ORDER BY id")
        ).fetchall()
    check(z1 == (60, 60, 360, 300), "valid group: anchor unchanged")
    check(z2 != (60, 60, 360, 300), "valid group: the other zone was moved")
    check(z3 == (500, 0, None, 250) and z4 == (500, 0, None, 250),
          f"invalid group: both zones completely untouched (got {z3}, {z4})")


def test_backfill_invalid_dimensions_idempotent_second_run():
    engine = _legacy_permissive_engine()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO floor (id, name, is_active) VALUES (1, 'F', 1)"))
        _insert_legacy_zone(conn, 1, 1, 0, 0, None, None)
        _insert_legacy_zone(conn, 2, 1, 0, 0, None, None)

    with engine.begin() as conn:
        first = _backfill_zone_overlap(conn)
    check(len(first.unresolved) == 2, "first run reports both as unresolved")

    with engine.connect() as conn:
        snapshot = conn.execute(text("SELECT id, pos_x, pos_y, width, height FROM zone ORDER BY id")).fetchall()

    with engine.begin() as conn:
        second = _backfill_zone_overlap(conn)
    check(second.moved == [] and len(second.unresolved) == 2,
          f"second run still reports the same unresolved zones, moves nothing (got {second})")

    with engine.connect() as conn:
        after = conn.execute(text("SELECT id, pos_x, pos_y, width, height FROM zone ORDER BY id")).fetchall()
    check(snapshot == after, f"no row changed between the first and second run (got {snapshot} vs {after})")


def test_backfill_invalid_dimension_log_has_no_credentials():
    engine = _legacy_permissive_engine()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO floor (id, name, is_active) VALUES (1, 'F', 1)"))
        _insert_legacy_zone(conn, 1, 1, 0, 0, None, 300)
        _insert_legacy_zone(conn, 2, 1, 0, 0, None, 300)

    with engine.begin() as conn:
        result = _backfill_zone_overlap(conn)
    check(len(result.unresolved) == 2, "sanity: 2 unresolved entries produced")
    for line in result.unresolved:
        low = line.lower()
        for forbidden in ("password", "://", "dsn", "secret", "token", "@"):
            check(forbidden not in low, f"unresolved log line does not contain {forbidden!r} (line: {line!r})")
        check("zone." in line and "floor" in line.lower(), f"line identifies the zone/floor (line: {line!r})")


def _full_schema_with_relaxed_zone_dimensions(engine):
    """migrate.run() exercises far more than the zone-overlap backfill (the
    ADDED_COLUMNS loop, prep-task backfill, payment hardening — several of
    which assume the FULL normal schema exists, unguarded, since that is
    always true in real use). The minimal floor+zone-only schema used by
    the direct-backfill tests above is not representative for THIS
    end-to-end test. Instead: build the real, full schema normally
    (Base.metadata.create_all — every table, every other NOT NULL
    constraint intact), then relax ONLY zone.width/zone.height on this one
    disposable engine, via SQLite's standard rename-recreate-copy-drop
    technique (SQLite has no ALTER COLUMN ... DROP NOT NULL). This never
    touches Base.metadata itself, any other column's constraint, or any
    real/production schema — only this one throwaway engine's zone table.
    """
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE zone RENAME TO zone_strict"))
        conn.execute(text(
            "CREATE TABLE zone (id INTEGER PRIMARY KEY, floor_id INTEGER NOT NULL, "
            "name VARCHAR(60) NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0, "
            "is_active BOOLEAN NOT NULL DEFAULT 1, color VARCHAR(7), "
            "pos_x INTEGER NOT NULL DEFAULT 60, pos_y INTEGER NOT NULL DEFAULT 60, "
            "width INTEGER, height INTEGER)"
        ))
        conn.execute(text(
            "INSERT INTO zone (id, floor_id, name, sort_order, is_active, color, "
            "pos_x, pos_y, width, height) "
            "SELECT id, floor_id, name, sort_order, is_active, color, pos_x, pos_y, "
            "width, height FROM zone_strict"
        ))
        conn.execute(text("DROP TABLE zone_strict"))


def test_migrate_run_does_not_crash_with_null_dimensions_present():
    """migrate.run() against the FULL real schema, carrying a legacy
    NULL-width group — the actual startup path, not just the backfill
    function in isolation."""
    engine = _sqlite_engine()
    _full_schema_with_relaxed_zone_dimensions(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    floor = Floor(name="Legacy")
    db.add(floor)
    db.commit()
    floor_id = floor.id
    db.close()
    with engine.begin() as conn:
        _insert_legacy_zone(conn, 101, floor_id, 0, 0, None, 300)
        _insert_legacy_zone(conn, 102, floor_id, 0, 0, None, 300)

    raised = False
    applied = []
    try:
        applied = migrate.run(engine)
    except Exception:  # noqa: BLE001
        raised = True
    check(not raised, "migrate.run() completes even with legacy NULL-dimension zones present")
    check(any("UNRESOLVED" in line for line in applied),
          f"the boot log surfaces the unresolved legacy zones (got {applied})")


# ==========================================================================
# C — POST /admin/zones/{id}/move-layout: non-blocking exact-overlap warning
# ==========================================================================

def test_move_layout_warns_only_on_exact_coincidence_not_partial_overlap():
    db = _session()
    owner, floor = _seed_owner_and_floor(db)
    zone_a = Zone(name="A", floor_id=floor.id, pos_x=0, pos_y=0, width=400, height=400)
    zone_b = Zone(name="B", floor_id=floor.id, pos_x=600, pos_y=0, width=400, height=400)
    db.add_all([zone_a, zone_b])
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 300, "pos_y": 0, "width": 400, "height": 400, "tables": "",
    })
    check(r.status_code == 204, f"partial-overlap move accepted (got {r.status_code})")
    check(r.headers.get("X-Zone-Overlap-Warning") is None,
          "no warning header for a partial (non-exact) overlap")

    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 600, "pos_y": 0, "width": 400, "height": 400, "tables": "",
    })
    check(r.status_code == 204, f"exact-coincidence move still succeeds (got {r.status_code})")
    check(r.headers.get("X-Zone-Overlap-Warning") == "exact",
          f"warning header present for an exact coincidence (got {r.headers.get('X-Zone-Overlap-Warning')!r})")
    app.dependency_overrides.clear()
    db.close()


def test_move_layout_no_warning_header_when_save_itself_fails():
    db = _session()
    owner, floor = _seed_owner_and_floor(db)
    zone_a = Zone(name="A", floor_id=floor.id)
    db.add(zone_a)
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 0, "pos_y": 0, "width": 100, "height": 100, "tables": "999:1:1",
    })
    check(r.status_code == 400, f"malformed/mismatched payload rejected (got {r.status_code})")
    check(r.headers.get("X-Zone-Overlap-Warning") is None, "no overlap header on a rejected request")
    app.dependency_overrides.clear()
    db.close()


# ==========================================================================
# E — runtime warning banner for a still-unresolved exact coincidence
# ==========================================================================

def test_tables_page_warns_when_active_zones_still_exactly_coincide():
    """The boot log alone (app/migrate.py's UNRESOLVED lines) is not
    something a manager using the page will ever see. GET /admin/tables
    must independently detect and surface the same condition, fresh on
    every render, without blocking the map."""
    db = _session()
    owner, floor = _seed_owner_and_floor(db)
    dup1 = Zone(name="Main", floor_id=floor.id, sort_order=0)
    dup2 = Zone(name="Bar", floor_id=floor.id, sort_order=1)
    db.add_all([dup1, dup2])
    db.commit()
    c = _client(db, owner)

    r = c.get(f"/admin/tables?floor={floor.id}")
    check(r.status_code == 200, f"page still renders normally (got {r.status_code})")
    check("Zone position conflict" in r.text, "the warning banner is present")
    check("Main" in r.text and "Bar" in r.text, "both coincident zones are named in the banner")
    check('id="freemap"' in r.text, "the map itself still renders — the warning never blocks it")
    check("resolved" not in r.text.lower() or "not automatically resolved" in r.text.lower(),
          "the banner never claims the problem is resolved")
    app.dependency_overrides.clear()
    db.close()


def test_tables_page_no_warning_when_zones_are_distinct():
    db = _session()
    owner, floor = _seed_owner_and_floor(db)
    z1 = Zone(name="Patio", floor_id=floor.id, sort_order=0, pos_x=0, pos_y=0, width=300, height=300)
    z2 = Zone(name="Window", floor_id=floor.id, sort_order=1, pos_x=500, pos_y=0, width=300, height=300)
    db.add_all([z1, z2])
    db.commit()
    c = _client(db, owner)

    r = c.get(f"/admin/tables?floor={floor.id}")
    check(r.status_code == 200, f"page renders (got {r.status_code})")
    check("Zone position conflict" not in r.text, "no warning banner when every zone rect is distinct")
    app.dependency_overrides.clear()
    db.close()


def test_admin_paths_to_fix_a_coincident_zone_still_exist():
    """Confirms the two administrative paths the warning banner points to
    are real, not aspirational: /admin/floors exposes numeric pos_x/pos_y/
    width/height fields per zone (the "Map box"), and /admin/tables' table
    list lets a table's zone be reassigned by name via a <select>,
    independent of any geometry — a coincident zone is never a dead end."""
    db = _session()
    owner, floor = _seed_owner_and_floor(db)
    zone = Zone(name="Main", floor_id=floor.id)
    db.add(zone)
    db.flush()
    table = RestaurantTable(number=1, zone_id=zone.id, capacity=4)
    db.add(table)
    db.commit()
    c = _client(db, owner)

    r = c.get("/admin/floors")
    check(r.status_code == 200, f"/admin/floors renders (got {r.status_code})")
    check('name="pos_x"' in r.text and 'name="pos_y"' in r.text
          and 'name="width"' in r.text and 'name="height"' in r.text,
          "the Map box's numeric position/size fields are present and editable")

    r = c.get("/admin/tables?floor=" + str(floor.id))
    check(r.status_code == 200, f"/admin/tables renders (got {r.status_code})")
    check(f'value="{zone.id}"' in r.text, "the table list's zone <select> still offers this zone by id/name")
    app.dependency_overrides.clear()
    db.close()


# ==========================================================================
# D — shape and capacity: real visual difference on the map
# ==========================================================================

def test_table_shape_and_capacity_render_in_markup():
    db = _session()
    owner, floor = _seed_owner_and_floor(db)
    zone = Zone(name="Z", floor_id=floor.id)
    db.add(zone)
    db.flush()
    small = RestaurantTable(number=1, zone_id=zone.id, capacity=1, shape="round",
                             map_x_per_mille=100, map_y_per_mille=100)
    big = RestaurantTable(number=2, zone_id=zone.id, capacity=20, shape="rect",
                           map_x_per_mille=200, map_y_per_mille=200)
    # RestaurantTable.capacity is NOT NULL at the schema level (confirmed:
    # `.nullable is False`), so a real NULL row can't be committed here —
    # the None-safety of the template's fallback is instead exercised
    # directly, arithmetically, in test_shape_effective_widths_are_distinct
    # _across_capacities below. capacity=0 IS reachable (no CHECK
    # constraint enforces >=1 at the database level, only the form's
    # min="1" does) and is exercised here for real, end-to-end.
    zero_cap = RestaurantTable(number=3, zone_id=zone.id, capacity=0, shape="square",
                                map_x_per_mille=300, map_y_per_mille=300)
    db.add_all([small, big, zero_cap])
    db.commit()
    c = _client(db, owner)

    r = c.get(f"/admin/tables?floor={floor.id}")
    check(r.status_code == 200, f"page renders, including a zero-capacity table, without error (got {r.status_code})")
    body = r.text
    check('data-shape="round"' in body, "round shape attribute present")
    check('data-shape="rect"' in body, "rect shape attribute present")
    check("--cap-w:" in body, "a capacity-driven width custom property is emitted")

    import re
    def cap_w_for(table_id):
        m = re.search(r'data-id="%d"[^>]*--cap-w:\s*(\d+)px' % table_id, body)
        return int(m.group(1)) if m else None

    small_w, big_w, zero_w = cap_w_for(small.id), cap_w_for(big.id), cap_w_for(zero_cap.id)
    check(None not in (small_w, big_w, zero_w), "every chip, including the zero-capacity one, carries a --cap-w value")
    check(small_w < big_w, f"capacity 1 renders narrower than capacity 20 (got {small_w} vs {big_w})")
    check(zero_w == small_w, f"capacity 0 falls back to the same width as capacity 1 (got {zero_w} vs {small_w})")
    check(72 <= small_w <= 140 and 72 <= big_w <= 140,
          f"both --cap-w values stay within [72,140] (got {small_w}, {big_w})")
    app.dependency_overrides.clear()
    db.close()


def _extract_css_rule(css: str, selector: str) -> str:
    start = css.index(selector)
    brace = css.index("{", start)
    end = css.index("}", brace)
    return css[brace:end + 1]


def _px(rule: str, prop: str) -> float | None:
    import re
    m = re.search(rf"{prop}:\s*([\d.]+)px", rule)
    return float(m.group(1)) if m else None


def test_shape_effective_widths_are_distinct_across_capacities():
    """Review finding (MEDIUM): the previous test only checked that shape
    tokens existed in the CSS text, which passed even though rect's own
    width was silently re-clamped by an inherited max-width:140px at high
    capacity — erasing its visual distinction exactly where it mattered
    most. This computes the ACTUAL effective (cascade-resolved) width for
    round/square vs. rect at low, high, and out-of-range capacities, the
    same way a browser would: width formula, then clamped by min/max-width
    — read from the real CSS file, not re-typed as separate expectations.
    """
    css_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "web", "static", "app.css")
    with open(css_path, encoding="utf-8") as f:
        css = f.read()

    base_rule = _extract_css_rule(css, ".map-chip {")
    rect_rule = _extract_css_rule(css, '.map-chip[data-shape="rect"]')
    base_min, base_max = _px(base_rule, "min-width"), _px(base_rule, "max-width")
    rect_min, rect_max = _px(rect_rule, "min-width"), _px(rect_rule, "max-width")
    check(None not in (base_min, base_max, rect_min, rect_max),
          f"all four bounds are present and parse as pixel values "
          f"(base=[{base_min},{base_max}], rect=[{rect_min},{rect_max}])")
    check(rect_max > base_max,
          f"rect's own max-width is wider than round/square's inherited one "
          f"(rect max={rect_max} vs base max={base_max}) — the exact regression this fixes")

    def clamp(v, lo, hi):
        return max(lo, min(hi, v))

    # Same formula as the template (72 + (cap-1)*3, cap clamped to [1,20]).
    def cap_w(capacity):
        cap = max(1, min(20, capacity or 1))
        return 72 + (cap - 1) * 3

    for capacity in (1, 11, 20, 0, None, -5, 999):
        w = cap_w(capacity)
        round_square_w = clamp(w, base_min, base_max)
        rect_w = clamp(w * 1.35, rect_min, rect_max)
        check(base_min <= round_square_w <= base_max,
              f"capacity={capacity}: round/square effective width {round_square_w} stays within [{base_min},{base_max}]")
        check(rect_min <= rect_w <= rect_max,
              f"capacity={capacity}: rect effective width {rect_w} stays within [{rect_min},{rect_max}]")
        check(rect_w > round_square_w,
              f"capacity={capacity}: rect ({rect_w}px) is visibly wider than round/square ({round_square_w}px)")

    # The specific case the review's regression targeted: high capacity.
    w20 = cap_w(20)
    round_square_20 = clamp(w20, base_min, base_max)
    rect_20 = clamp(w20 * 1.35, rect_min, rect_max)
    check(rect_20 - round_square_20 >= 30,
          f"at capacity 20, rect is meaningfully wider than round/square, not silently re-clamped to the "
          f"same ceiling (rect={rect_20}, round/square={round_square_20})")

    round_rule = _extract_css_rule(css, '.map-chip[data-shape="round"]')
    check("50%" in round_rule, "round uses a true ellipse/circle radius")
    check("border-radius" in _extract_css_rule(css, '.map-chip[data-shape="square"]'),
          "square has its own (lighter) rounding rule")


# ==========================================================================
# PostgreSQL — same disposable-target guard as test_floor_map_coordinates.py,
# reused directly (never a second copy of that safety logic). Runs only
# when PG_TEST_DSN names a disposable database AND ALLOW_DESTRUCTIVE_PG_TESTS
# =1 is set; otherwise skipped with a printed message. Never Render/Neon.
# ==========================================================================

def _run_postgres_scenarios():
    dsn = pg_dsn()
    if not dsn:
        print("SKIP: PostgreSQL scenarios (PG_TEST_DSN not set)")
        return
    assert_disposable_postgres_target(dsn)   # raises before any connection if unsafe
    from sqlalchemy import create_engine as _create_engine
    engine = _create_engine(dsn, future=True)
    assert_disposable_postgres_target(str(engine.url))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        # -- basic backfill + idempotency, direct call --------------------
        ids = _seed_backfill_scenario(engine)
        with engine.begin() as conn:
            result = _backfill_zone_overlap(conn)
        check(len(result.moved) == 1 and result.unresolved == [],
              f"PostgreSQL: backfill de-collides the seeded 2-way group (got {result})")
        Session = sessionmaker(bind=engine, future=True)
        db = Session()
        dup1 = db.get(Zone, ids["dup1"])
        check(_rect(dup1) == (60, 60, 360, 300), "PostgreSQL: anchor unchanged")
        db.close()

        with engine.begin() as conn:
            second = _backfill_zone_overlap(conn)
        check(second.moved == [] and second.unresolved == [], f"PostgreSQL: second run is a no-op (got {second})")

        # -- impossible case, no exception, no boot crash ------------------
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, future=True)
        db = Session()
        floor = Floor(name="Impossible")
        db.add(floor)
        db.flush()
        z0 = Zone(name="Z0", floor_id=floor.id, sort_order=0, pos_x=0, pos_y=0, width=1000, height=1000)
        z1 = Zone(name="Z1", floor_id=floor.id, sort_order=1, pos_x=0, pos_y=0, width=1000, height=1000)
        db.add_all([z0, z1])
        db.commit()
        z0_id, z1_id = z0.id, z1.id
        db.close()

        raised = False
        try:
            with engine.begin() as conn:
                result = _backfill_zone_overlap(conn)
        except Exception:  # noqa: BLE001
            raised = True
        check(not raised, "PostgreSQL: the impossible 1000x1000 group does not raise")
        db = Session()
        check(_rect(db.get(Zone, z0_id)) == (0, 0, 1000, 1000) and _rect(db.get(Zone, z1_id)) == (0, 0, 1000, 1000),
              "PostgreSQL: both zones in the impossible group are left unchanged")
        db.close()

        # -- end-to-end: _run_postgres() itself, not the backfill directly --
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        ids3 = _seed_backfill_scenario(engine)
        applied = migrate._run_postgres(engine)
        check(any("de-collided" in line for line in applied),
              f"PostgreSQL: _run_postgres() itself wires and surfaces the zone-overlap backfill "
              f"(got {[l for l in applied if 'zone' in l.lower() or 'de-collided' in l]})")
        db = Session()
        check(_rect(db.get(Zone, ids3["dup1"])) == (60, 60, 360, 300),
              "PostgreSQL: _run_postgres() end-to-end still preserves the anchor")
        db.close()

        # -- cross-dialect determinism: same starting state, same result --
        sqlite_engine = _sqlite_engine()
        Base.metadata.create_all(sqlite_engine)
        ids_sqlite = _seed_backfill_scenario(sqlite_engine)
        with sqlite_engine.begin() as conn:
            _backfill_zone_overlap(conn)
        with sessionmaker(bind=sqlite_engine, future=True)() as s:
            sqlite_dup2_rect = _rect(s.get(Zone, ids_sqlite["dup2"]))

        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        ids_pg = _seed_backfill_scenario(engine)
        with engine.begin() as conn:
            _backfill_zone_overlap(conn)
        # Explicitly closed (not left to garbage collection): an idle-in
        # -transaction connection here would make the NEXT drop_all(engine)
        # below block forever waiting for its lock — a real hang hit and
        # fixed while writing this test, not a hypothetical.
        with sessionmaker(bind=engine, future=True)() as s:
            pg_dup2_rect = _rect(s.get(Zone, ids_pg["dup2"]))
        check(sqlite_dup2_rect == pg_dup2_rect,
              f"SQLite and PostgreSQL reach the identical result from the identical starting state "
              f"(SQLite={sqlite_dup2_rect}, PostgreSQL={pg_dup2_rect})")

        # -- legacy/corrupted data: NULL width does not crash PostgreSQL
        #    either. zone.width/height are NOT NULL in the real schema —
        #    relaxed on THIS disposable database only, via a real ALTER
        #    TABLE ... DROP NOT NULL (PostgreSQL supports this directly,
        #    unlike SQLite's rename-recreate dance) — never touching
        #    production's constraint, only this throwaway table. ----------
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE zone ALTER COLUMN width DROP NOT NULL"))
            conn.execute(text("ALTER TABLE zone ALTER COLUMN height DROP NOT NULL"))
        Session = sessionmaker(bind=engine, future=True)
        with Session() as s:
            floor = Floor(name="Legacy")
            s.add(floor)
            s.commit()
            legacy_floor_id = floor.id
        with engine.begin() as conn:
            _insert_legacy_zone(conn, 201, legacy_floor_id, 0, 0, None, 300)
            _insert_legacy_zone(conn, 202, legacy_floor_id, 0, 0, None, 300)

        raised = False
        try:
            with engine.begin() as conn:
                result = _backfill_zone_overlap(conn)
        except Exception:  # noqa: BLE001
            raised = True
        check(not raised, "PostgreSQL: a NULL width does not raise anywhere")
        check(result.moved == [] and len(result.unresolved) == 2,
              f"PostgreSQL: both legacy zones reported unresolved, nothing moved (got {result})")
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT pos_x, pos_y, width, height FROM zone WHERE id IN (201, 202) ORDER BY id"
            )).fetchall()
        check(rows == [(0, 0, None, 300), (0, 0, None, 300)],
              f"PostgreSQL: both legacy zones left completely unchanged (got {rows})")

        raised = False
        try:
            with engine.begin() as conn:
                second = _backfill_zone_overlap(conn)
        except Exception:  # noqa: BLE001
            raised = True
        check(not raised and len(second.unresolved) == 2,
              f"PostgreSQL: second run over the same legacy data is still safe and idempotent (got {second})")

        # -- concurrency: locking floor A blocks a concurrent create on the
        #    SAME floor, but never blocks floor B -----------------------
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, future=True)
        db = Session()
        owner = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
        floor_a = Floor(name="Concurrent A")
        floor_b = Floor(name="Concurrent B")
        db.add_all([owner, floor_a, floor_b])
        db.commit()
        floor_a_id, floor_b_id = floor_a.id, floor_b.id
        db.close()

        # Hold the lock on floor A in a background connection/transaction.
        holder_engine = _create_engine(dsn, future=True)
        holder_conn = holder_engine.connect()
        holder_txn = holder_conn.begin()
        holder_conn.execute(text("SELECT id FROM floor WHERE id = :id FOR UPDATE"), {"id": floor_a_id})

        blocked_done = threading.Event()
        floor_b_done = threading.Event()

        def _try_lock_floor_a():
            probe_engine = _create_engine(dsn, future=True)
            with probe_engine.connect() as conn:
                with conn.begin():
                    conn.execute(text("SELECT id FROM floor WHERE id = :id FOR UPDATE"), {"id": floor_a_id})
            blocked_done.set()
            probe_engine.dispose()

        def _try_lock_floor_b():
            probe_engine = _create_engine(dsn, future=True)
            with probe_engine.connect() as conn:
                with conn.begin():
                    conn.execute(text("SELECT id FROM floor WHERE id = :id FOR UPDATE"), {"id": floor_b_id})
            floor_b_done.set()
            probe_engine.dispose()

        t_a = threading.Thread(target=_try_lock_floor_a, daemon=True)
        t_b = threading.Thread(target=_try_lock_floor_b, daemon=True)
        t_a.start()
        t_b.start()

        floor_b_finished_quickly = floor_b_done.wait(timeout=5)
        check(floor_b_finished_quickly, "PostgreSQL: locking floor A does NOT block a concurrent operation on floor B")
        check(not blocked_done.is_set(), "PostgreSQL: the concurrent request on floor A is still blocked")

        holder_txn.commit()
        holder_conn.close()
        holder_engine.dispose()
        released_in_time = blocked_done.wait(timeout=5)
        check(released_in_time, "PostgreSQL: the blocked request on floor A completes once the lock is released")
        t_a.join(timeout=5)
        t_b.join(timeout=5)
    finally:
        assert_disposable_postgres_target(str(engine.url))
        Base.metadata.drop_all(engine)
        engine.dispose()


if __name__ == "__main__":
    for fn in (
        test_zone_geometry_module_is_neutral_and_place_new_zone_rect_lives_there,
        test_place_new_zone_rect_961_finds_alternative_position,
        test_place_new_zone_rect_near_1000_asymmetric_requires_the_sweep,
        test_place_new_zone_rect_1000x1000_impossible_returns_none_not_exception,
        test_place_new_zone_rect_fully_exhausted_near_max_also_returns_none,
        test_place_new_zone_rect_deterministic_same_inputs_same_output,
        test_create_zones_batch_gets_distinct_rects,
        test_create_zones_avoids_existing_zone_when_first_candidate_collides,
        test_create_zones_different_floors_are_independent,
        test_create_zones_locks_the_floor_row_sqlite_noop_documented,
        test_backfill_two_and_three_way_groups_deterministic_and_anchor_preserved,
        test_backfill_moves_tables_proportionally_and_respects_null_inactive_policy,
        test_backfill_is_idempotent,
        test_backfill_impossible_group_is_unresolved_not_raised,
        test_backfill_resolvable_and_impossible_groups_together,
        test_backfill_unresolved_log_has_no_credentials,
        test_backfill_genuine_internal_defect_still_raises_and_rolls_back,
        test_creating_a_new_zone_after_backfill_does_not_recreate_coincidence,
        test_migrate_run_wires_the_backfill_end_to_end,
        test_migrate_run_completes_with_an_impossible_case_present,
        test_backfill_null_width_group_is_unresolved_not_crashed,
        test_backfill_null_height_group_is_unresolved_not_crashed,
        test_backfill_invalid_dimension_values_zero_negative_over_1000,
        test_backfill_invalid_group_alongside_valid_group_both_handled_correctly,
        test_backfill_invalid_dimensions_idempotent_second_run,
        test_backfill_invalid_dimension_log_has_no_credentials,
        test_migrate_run_does_not_crash_with_null_dimensions_present,
        test_move_layout_warns_only_on_exact_coincidence_not_partial_overlap,
        test_move_layout_no_warning_header_when_save_itself_fails,
        test_tables_page_warns_when_active_zones_still_exactly_coincide,
        test_tables_page_no_warning_when_zones_are_distinct,
        test_admin_paths_to_fix_a_coincident_zone_still_exist,
        test_table_shape_and_capacity_render_in_markup,
        test_shape_effective_widths_are_distinct_across_capacities,
    ):
        print(f"- {fn.__name__}")
        fn()
    print("- _run_postgres_scenarios")
    _run_postgres_scenarios()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall floor-zone-overlap tests passed")
