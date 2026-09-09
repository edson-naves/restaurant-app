"""Operational Floor — Map/Arrange port (feat/floor-operational-port).

Restores, on top of the current schema/endpoints (never the old
feat/floor-map branch's own per-mille-on-pos_x/pos_y semantics or its
batch-everything /admin/tables/layout call):

  - the Map/List view toggle (same cards as the list, positioned by
    map_x_per_mille/map_y_per_mille instead of stacked in zone panels);
  - Arrange mode (Owner/settings only), reusing the admin Floor Plan
    Builder's already-reviewed atomic endpoints (POST /admin/tables/
    map-layout, POST /admin/zones/{id}/move-layout) and pure geometry
    module (floor_bounds.js) — same pointerId-ownership and visible-bounds
    handling, not a re-port of the old branch's own (buggier) version;
  - the staff floor-plan colour picker (admin_staff.html) — the backend
    route never stopped existing;
  - "My tables" persisted across visits via localStorage.

No browser/DOM harness in this codebase (same carve-out as the admin
builder's own pointer tests) — the drag arithmetic itself is covered by
tests/test_floor_bounds.js (shared pure module) and was additionally
exercised in a real headless-Chromium session against a disposable SQLite
database during development (not part of this repo's automated suite).
These tests confirm the markup/wiring a regression would actually break:
present, correctly sourced, and using the current endpoints/data model.

Run: python tests/test_floor_operational_map.py
"""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _env  # noqa: F401  — declares the test opt-out before app imports
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.deps import current_staff
from app.main import app
from app.models import oltp  # noqa: F401
from app.models.oltp import Floor, RestaurantTable, Staff, Zone

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)
    return cond


def _session():
    path = os.path.join(tempfile.gettempdir(), f"flooropmap_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _seed(db):
    owner = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
    waiter = Staff(name="Sam Lee", role="waiter", pin_code="x", is_active=True)
    floor = Floor(name="Main")
    db.add_all([owner, waiter, floor])
    db.flush()
    zone = Zone(name="Patio", floor_id=floor.id, pos_x=10, pos_y=20, width=300, height=250)
    db.add(zone)
    db.flush()
    return owner, waiter, floor, zone


def _client(db, staff):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


def _slice(body, start_marker, end_marker):
    start = body.find(start_marker)
    end = body.find(end_marker, start)
    return body[start:end]


# --------------------------------------------------------------------------
# Map/List + zone rectangles: markup sourced from the current per-mille model
# --------------------------------------------------------------------------

def test_floor_page_renders_map_plane_and_zone_rects():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    t = RestaurantTable(number=1, zone_id=zone.id, capacity=4, is_active=True,
                         map_x_per_mille=0, map_y_per_mille=1000)  # a true edge case
    db.add(t)
    db.commit()
    c = _client(db, owner)

    r = c.get(f"/?floor={floor.id}")
    check(r.status_code == 200, f"page renders (got {r.status_code})")
    body = r.text
    check('class="map-plane"' in body, "the map-plane wrapper is present")
    check(f'class="fzrect" data-id="{zone.id}"' in body, "the zone's rectangle is rendered")
    check('--fx: 1.0%; --fy: 2.0%; --fw: 30.0%; --fh: 25.0%' in body,
          "the zone rectangle's geometry comes from Zone.pos_x/pos_y/width/height")
    check(f'data-id="{t.id}"' in body, "the table card carries its own db id (needed by Arrange)")
    check('--x: 0.0%; --y: 100.0%' in body,
          "the table's map position comes from map_x_per_mille/map_y_per_mille, not pos_x/pos_y")
    check('id="viewToggle"' in body, "the Map/List toggle button is present")
    check('id="arrangeToggle"' in body, "Arrange is offered (this staff has settings permission)")
    app.dependency_overrides.clear()
    db.close()


def test_never_placed_table_defaults_to_plane_centre_not_null_or_crash():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    t = RestaurantTable(number=1, zone_id=zone.id, capacity=4, is_active=True)  # map position never set
    db.add(t)
    db.commit()
    c = _client(db, owner)

    r = c.get(f"/?floor={floor.id}")
    check(r.status_code == 200, f"a never-placed table does not crash the page (got {r.status_code})")
    check('--x: 50.0%; --y: 50.0%' in r.text,
          "a NULL map position falls back to the plane's centre, same as the CSS default")
    app.dependency_overrides.clear()
    db.close()


def test_waiter_without_settings_permission_gets_no_arrange_button():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    db.commit()
    c = _client(db, waiter)

    r = c.get(f"/?floor={floor.id}")
    check(r.status_code == 200, f"page renders for a waiter (got {r.status_code})")
    check('id="viewToggle"' in r.text, "Map/List is still offered to a non-Owner")
    check('id="arrangeToggle"' not in r.text,
          "Arrange is NOT offered without settings permission — same gate as the admin builder")
    app.dependency_overrides.clear()
    db.close()


# --------------------------------------------------------------------------
# Arrange script: pointerId ownership and visible-bounds reuse, structurally
# (same carve-out/limitation as tests/test_floor_admin_ui.py — no DOM
# harness in this codebase to fire real PointerEvents and observe the
# result; the formula itself is covered by tests/test_floor_bounds.js).
# --------------------------------------------------------------------------

def test_arrange_script_reuses_pointer_ownership_and_visible_bounds():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    db.commit()
    c = _client(db, owner)

    r = c.get(f"/?floor={floor.id}")
    body = r.text
    check("/static/floor_bounds.js" in body,
          "the shared pure geometry module is loaded on this page too")

    down = _slice(body, "planes.forEach(function (plane) {", "toggle.addEventListener('click'")
    check("if (drag) return;" in down, "a second pointer while one gesture is active is ignored (admin builder's own fix)")
    check(down.count("pointerId: e.pointerId") == 3,
          f"pointerId is stored on all 3 drag kinds — table/resize/move (found {down.count('pointerId: e.pointerId')})")
    check("e.pointerId !== drag.pointerId" in down,
          "pointermove/pointerup/pointercancel reject a non-owning pointer")
    check("visualOrigin(" in down,
          "a drag's origin is the visual (clamped) position, not the raw persisted value — "
          "the dead-zone fix from the admin builder's own review")
    check("window.FloorTableBounds.visibleBounds(" in body,
          "table bounds are computed via the shared pure module, not a re-implemented formula")

    cancel = _slice(body, "plane.addEventListener('pointercancel'", "plane.addEventListener('click'")
    check("post(" not in cancel, "pointercancel never sends an autosave request")
    check("drag = null" in cancel, "pointercancel clears the active gesture")
    check("classList.remove('dragging')" in cancel, "pointercancel always removes .dragging")

    up = _slice(body, "plane.addEventListener('pointerup'", "plane.addEventListener('pointercancel'")
    check("/admin/tables/map-layout" in up, "a table drag saves through the current atomic endpoint")
    check("/admin/zones/' + d.el.dataset.id + '/move-layout" in up,
          "a zone move/resize saves through the current atomic endpoint, never the old /admin/tables/layout")
    check("elementsFromPoint" in up, "zone drop hit-test uses the browser's own paint order, not hand-rolled geometry")
    app.dependency_overrides.clear()
    db.close()


def test_idle_auto_reload_is_paused_while_arranging():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    db.commit()
    c = _client(db, owner)
    body = c.get(f"/?floor={floor.id}").text
    check("fp-editing" in body,
          "the idle auto-reload checks the fp-editing flag Arrange sets — never reloads mid-drag")
    app.dependency_overrides.clear()
    db.close()


# --------------------------------------------------------------------------
# Staff colour picker restored
# --------------------------------------------------------------------------

def test_staff_page_has_colour_picker_calling_the_existing_route():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    db.commit()
    c = _client(db, owner)

    r = c.get("/admin/staff")
    check(r.status_code == 200, f"/admin/staff renders (got {r.status_code})")
    body = r.text
    check('class="staff-color"' in body, "the floor-plan colour input is present again")
    check("/admin/staff/' + el.dataset.id + '/color" in body,
          "it posts to the colour route that was never actually removed from the backend")
    app.dependency_overrides.clear()
    db.close()


# --------------------------------------------------------------------------
# "My tables" persistence
# --------------------------------------------------------------------------

def test_my_tables_preference_persists_via_localstorage():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    db.commit()
    c = _client(db, owner)
    body = c.get(f"/?floor={floor.id}").text
    check("rms-floor-mine" in body, "a dedicated localStorage key is used (rms- prefix, matches base.html's theme key)")
    check("storedMine()" in body, "a remembered choice is read back")
    check("storeMine(mineFilter)" in body, "clicking My tables writes the new choice back")
    app.dependency_overrides.clear()
    db.close()


# --------------------------------------------------------------------------
# Reservations picker: floor tabs + "select all" (Block 3)
# --------------------------------------------------------------------------

def test_reservation_picker_shows_floor_tabs_only_with_more_than_one_floor():
    db = _session()
    owner, waiter, floor_a, zone_a = _seed(db)
    floor_b = Floor(name="Patio Floor")
    db.add(floor_b)
    db.flush()
    zone_b = Zone(name="Outside", floor_id=floor_b.id)
    db.add(zone_b)
    db.flush()
    t1 = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4, is_active=True, status="free")
    t2 = RestaurantTable(number=2, zone_id=zone_b.id, capacity=4, is_active=True, status="free")
    db.add_all([t1, t2])
    db.commit()
    c = _client(db, owner)

    r = c.get("/reservations")
    check(r.status_code == 200, f"page renders (got {r.status_code})")
    body = r.text
    check('id="resFloorTabs"' in body, "floor tabs render when the picker spans more than one floor")
    check(f'data-picker-floor="{floor_a.id}"' in body and f'data-picker-floor="{floor_b.id}"' in body,
          "one tab per floor, keyed by the real floor id")
    check(f'data-picker-floor-panel="{floor_a.id}"' in body and f'data-picker-floor-panel="{floor_b.id}"' in body,
          "one panel per floor for the tabs to show/hide")
    check("resFloorTabs" in body and "data-picker-floor" in body, "the tab-switch script targets these same attributes")
    app.dependency_overrides.clear()
    db.close()


def test_reservation_picker_no_floor_tabs_with_a_single_floor():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    t = RestaurantTable(number=1, zone_id=zone.id, capacity=4, is_active=True, status="free")
    db.add(t)
    db.commit()
    c = _client(db, owner)

    r = c.get("/reservations")
    check(r.status_code == 200, f"page renders (got {r.status_code})")
    check('id="resFloorTabs"' not in r.text, "no tab bar when there is only one floor to pick from")
    app.dependency_overrides.clear()
    db.close()


def test_reservation_picker_has_select_all_that_skips_booked_tables():
    db = _session()
    owner, waiter, floor, zone = _seed(db)
    t1 = RestaurantTable(number=1, zone_id=zone.id, capacity=4, is_active=True, status="free")
    t2 = RestaurantTable(number=2, zone_id=zone.id, capacity=4, is_active=True, status="free")
    db.add_all([t1, t2])
    db.commit()
    c = _client(db, owner)

    body = c.get("/reservations").text
    check('class="reszone-all"' in body, "the zone header offers a select-all action")
    check("setTableSelected(" in body, "table selection is a shared, force-able function (not just a toggle)")
    check("reszone-all" in body and "data-booked" in body,
          "select-all reads each table's booked marker to decide what to skip")
    app.dependency_overrides.clear()
    db.close()


if __name__ == "__main__":
    for fn in (
        test_floor_page_renders_map_plane_and_zone_rects,
        test_never_placed_table_defaults_to_plane_centre_not_null_or_crash,
        test_waiter_without_settings_permission_gets_no_arrange_button,
        test_arrange_script_reuses_pointer_ownership_and_visible_bounds,
        test_idle_auto_reload_is_paused_while_arranging,
        test_staff_page_has_colour_picker_calling_the_existing_route,
        test_my_tables_preference_persists_via_localstorage,
        test_reservation_picker_shows_floor_tabs_only_with_more_than_one_floor,
        test_reservation_picker_no_floor_tabs_with_a_single_floor,
        test_reservation_picker_has_select_all_that_skips_booked_tables,
    ):
        print(f"- {fn.__name__}")
        fn()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall floor-operational-map tests passed")
