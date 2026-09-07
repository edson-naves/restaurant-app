"""Floor Plan Builder — implementation slice 2: the admin visual builder.

Covers the two new AJAX endpoints (POST /admin/tables/map-layout, POST
/admin/zones/{zone_id}/move-layout) per
docs/Evidence/Floor/FLOOR_PLAN_BUILDER_DESIGN.md §6, plus one page-render
smoke test for the free-map template (§4.4's NULL-fallback path). The
client-side drag math (proportional resize, hit-testing a drop point against
a zone rectangle) is JS-only and has no browser test harness in this
codebase (§9's "not in scope" carve-out) — these tests exercise the backend
contract the JS calls into: validate-then-persist-atomically, or reject and
write nothing.

Throwaway SQLite + dependency overrides, same pattern as
tests/test_floor_spatial.py. Run: python tests/test_floor_admin_ui.py
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

from app.database import Base
from app.deps import current_staff, get_db
from app.main import app
from app.models import oltp  # noqa: F401
from app.models.oltp import Floor, RestaurantTable, Staff, Zone

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _session():
    path = os.path.join(tempfile.gettempdir(), f"flooradminui_{uuid.uuid4().hex}.db")
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
    zone_a = Zone(name="Patio", floor_id=floor.id, pos_x=0, pos_y=0, width=500, height=500)
    zone_b = Zone(name="Bar", floor_id=floor.id, pos_x=500, pos_y=0, width=500, height=500)
    db.add_all([zone_a, zone_b])
    db.flush()
    return owner, waiter, floor, zone_a, zone_b


def _client(db, staff):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


# --------------------------------------------------------------------------
# POST /admin/tables/map-layout
# --------------------------------------------------------------------------

def test_map_layout_moves_a_table():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    t = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4)
    db.add(t)
    db.commit()
    c = _client(db, owner)

    r = c.post("/admin/tables/map-layout", data={"tables": f"{t.id}:123:456"})
    check(r.status_code == 204, f"plain move accepted (got {r.status_code})")
    db.refresh(t)
    check((t.map_x_per_mille, t.map_y_per_mille) == (123, 456), "free-map position persisted")
    check((t.pos_x, t.pos_y) == (0, 0), "grid pos_x/pos_y untouched by the free-map endpoint")
    app.dependency_overrides.clear()
    db.close()


def test_map_layout_clamps_out_of_range():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    t = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4)
    db.add(t)
    db.commit()
    c = _client(db, owner)

    r = c.post("/admin/tables/map-layout", data={"tables": f"{t.id}:-50:5000"})
    check(r.status_code == 204, "out-of-range coordinates still accepted (clamped, not rejected)")
    db.refresh(t)
    check((t.map_x_per_mille, t.map_y_per_mille) == (0, 1000),
          f"clamped to [0,1000] (got {t.map_x_per_mille},{t.map_y_per_mille})")
    app.dependency_overrides.clear()
    db.close()


def test_map_layout_rejects_unknown_table_and_malformed_entry():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    db.commit()
    c = _client(db, owner)

    r = c.post("/admin/tables/map-layout", data={"tables": "999:1:1"})
    check(r.status_code == 400, f"unknown table id rejected (got {r.status_code})")
    r = c.post("/admin/tables/map-layout", data={"tables": "abc:1:1"})
    check(r.status_code == 400, f"non-integer entry rejected (got {r.status_code})")
    app.dependency_overrides.clear()
    db.close()


def test_map_layout_shape_valid_invalid_and_omitted():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    t = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4, shape="round")
    db.add(t)
    db.commit()
    c = _client(db, owner)

    r = c.post("/admin/tables/map-layout", data={"tables": f"{t.id}:10:10:square"})
    check(r.status_code == 204, "valid shape accepted")
    db.refresh(t)
    check(t.shape == "square", "shape persisted from the 4th field")

    r = c.post("/admin/tables/map-layout", data={"tables": f"{t.id}:20:20:triangle"})
    check(r.status_code == 400, f"invalid shape rejected, not silently ignored (got {r.status_code})")
    db.refresh(t)
    check(t.shape == "square", "shape unchanged after the rejected request")
    check((t.map_x_per_mille, t.map_y_per_mille) == (10, 10),
          "position also unchanged — the whole rejected request wrote nothing")

    r = c.post("/admin/tables/map-layout", data={"tables": f"{t.id}:30:30"})
    check(r.status_code == 204, "3-field entry (no shape) still accepted")
    db.refresh(t)
    check(t.shape == "square", "shape untouched when the shape segment is omitted")
    check((t.map_x_per_mille, t.map_y_per_mille) == (30, 30), "position-only move applied")
    app.dependency_overrides.clear()
    db.close()


def test_map_layout_zone_id_reassigns():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    t = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4)
    db.add(t)
    db.commit()
    c = _client(db, owner)

    r = c.post("/admin/tables/map-layout", data={"tables": f"{t.id}:600:100::{zone_b.id}"})
    check(r.status_code == 204, f"drop-into-a-different-zone accepted (got {r.status_code})")
    db.refresh(t)
    check(t.zone_id == zone_b.id, "table reassigned to the new zone")
    check(t.zone == "Bar", "denormalized zone label kept in step (_assign_zone)")
    check((t.map_x_per_mille, t.map_y_per_mille) == (600, 100), "new position persisted with the reassignment")
    app.dependency_overrides.clear()
    db.close()


def test_map_layout_requires_owner():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    t = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4)
    db.add(t)
    db.commit()
    c = _client(db, waiter)

    r = c.post("/admin/tables/map-layout", data={"tables": f"{t.id}:1:1"})
    check(r.status_code == 403, f"non-Owner role rejected (got {r.status_code})")
    app.dependency_overrides.clear()
    db.close()


# --------------------------------------------------------------------------
# POST /admin/zones/{zone_id}/move-layout
# --------------------------------------------------------------------------

def test_move_layout_moves_zone_and_its_tables_atomically():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    t1 = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4)
    t2 = RestaurantTable(number=2, zone_id=zone_a.id, capacity=4)
    db.add_all([t1, t2])
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 50, "pos_y": 60, "width": 300, "height": 200,
        "tables": f"{t1.id}:100:150,{t2.id}:200:250",
    })
    check(r.status_code == 204, f"complete payload accepted (got {r.status_code})")
    db.refresh(zone_a); db.refresh(t1); db.refresh(t2)
    check((zone_a.pos_x, zone_a.pos_y, zone_a.width, zone_a.height) == (50, 60, 300, 200),
          "zone rectangle persisted")
    check((t1.map_x_per_mille, t1.map_y_per_mille) == (100, 150), "table 1 position persisted")
    check((t2.map_x_per_mille, t2.map_y_per_mille) == (200, 250), "table 2 position persisted")
    app.dependency_overrides.clear()
    db.close()


def test_move_layout_rejects_omitted_table_writes_nothing():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    t1 = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4, map_x_per_mille=1, map_y_per_mille=1)
    t2 = RestaurantTable(number=2, zone_id=zone_a.id, capacity=4, map_x_per_mille=2, map_y_per_mille=2)
    db.add_all([t1, t2])
    db.commit()
    before = (zone_a.pos_x, zone_a.pos_y, zone_a.width, zone_a.height)
    c = _client(db, owner)

    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 10, "pos_y": 10, "width": 100, "height": 100,
        "tables": f"{t1.id}:5:5",   # t2 silently omitted
    })
    check(r.status_code == 400, f"incomplete payload rejected (got {r.status_code})")
    check(str(t2.id) in r.text, "the missing table's id is named in the error")
    db.expire_all()
    zone_a2 = db.get(Zone, zone_a.id); t1b = db.get(RestaurantTable, t1.id); t2b = db.get(RestaurantTable, t2.id)
    check((zone_a2.pos_x, zone_a2.pos_y, zone_a2.width, zone_a2.height) == before, "zone rectangle unchanged")
    check((t1b.map_x_per_mille, t1b.map_y_per_mille) == (1, 1), "table 1 unchanged — no partial write")
    check((t2b.map_x_per_mille, t2b.map_y_per_mille) == (2, 2), "table 2 (the omitted one) unchanged")
    app.dependency_overrides.clear()
    db.close()


def test_move_layout_rejects_extra_unknown_id():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    t1 = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4, map_x_per_mille=1, map_y_per_mille=1)
    db.add(t1)
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 0, "pos_y": 0, "width": 200, "height": 200,
        "tables": f"{t1.id}:5:5,999999:1:1",
    })
    check(r.status_code == 400, f"payload with an unknown extra id rejected (got {r.status_code})")
    db.refresh(t1)
    check((t1.map_x_per_mille, t1.map_y_per_mille) == (1, 1), "nothing written on rejection")
    app.dependency_overrides.clear()
    db.close()


def test_move_layout_rejects_duplicate_id():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    t1 = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4, map_x_per_mille=1, map_y_per_mille=1)
    db.add(t1)
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 0, "pos_y": 0, "width": 200, "height": 200,
        "tables": f"{t1.id}:5:5,{t1.id}:6:6",
    })
    check(r.status_code == 400, f"duplicate table id in payload rejected (got {r.status_code})")
    db.refresh(t1)
    check((t1.map_x_per_mille, t1.map_y_per_mille) == (1, 1), "nothing written on rejection")
    app.dependency_overrides.clear()
    db.close()


def test_move_layout_rejects_table_from_another_zone():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    t_a = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4, map_x_per_mille=1, map_y_per_mille=1)
    t_b = RestaurantTable(number=2, zone_id=zone_b.id, capacity=4, map_x_per_mille=9, map_y_per_mille=9)
    db.add_all([t_a, t_b])
    db.commit()
    c = _client(db, owner)

    # Swap t_b in for t_a — same count, but t_b does not belong to zone_a.
    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 0, "pos_y": 0, "width": 200, "height": 200,
        "tables": f"{t_b.id}:5:5",
    })
    check(r.status_code == 400, f"a table from a different zone is rejected (got {r.status_code})")
    db.refresh(t_a); db.refresh(t_b)
    check((t_a.map_x_per_mille, t_a.map_y_per_mille) == (1, 1), "zone_a's own table unchanged")
    check((t_b.map_x_per_mille, t_b.map_y_per_mille) == (9, 9), "the foreign table unchanged")
    check(t_b.zone_id == zone_b.id, "the foreign table's zone assignment is untouched")
    app.dependency_overrides.clear()
    db.close()


def test_move_layout_empty_payload_on_empty_and_occupied_zone():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    db.commit()
    c = _client(db, owner)

    # Empty zone: empty payload matches (zero active tables) — rect-only update.
    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 1, "pos_y": 2, "width": 100, "height": 100, "tables": "",
    })
    check(r.status_code == 204, f"empty payload on an empty zone accepted (got {r.status_code})")
    db.refresh(zone_a)
    check((zone_a.pos_x, zone_a.pos_y) == (1, 2), "rect persisted on the empty-zone case")

    # Now occupy it, and re-try an empty payload — must now be rejected.
    t = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4)
    db.add(t)
    db.commit()
    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 3, "pos_y": 4, "width": 100, "height": 100, "tables": "",
    })
    check(r.status_code == 400, f"empty payload on an occupied zone rejected (got {r.status_code})")
    db.refresh(zone_a)
    check((zone_a.pos_x, zone_a.pos_y) == (1, 2), "rect unchanged after the rejected empty-on-occupied call")
    app.dependency_overrides.clear()
    db.close()


def test_move_layout_inactive_table_excluded_and_rejected_if_named():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    active = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4, map_x_per_mille=1, map_y_per_mille=1)
    retired = RestaurantTable(number=2, zone_id=zone_a.id, capacity=4, is_active=False,
                               map_x_per_mille=7, map_y_per_mille=7)
    db.add_all([active, retired])
    db.commit()
    c = _client(db, owner)

    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 0, "pos_y": 0, "width": 200, "height": 200,
        "tables": f"{active.id}:5:5",
    })
    check(r.status_code == 204, f"payload naming only the active table succeeds (got {r.status_code})")
    db.refresh(retired)
    check((retired.map_x_per_mille, retired.map_y_per_mille) == (7, 7),
          "the retired table's position is untouched")

    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 0, "pos_y": 0, "width": 200, "height": 200,
        "tables": f"{active.id}:6:6,{retired.id}:8:8",
    })
    check(r.status_code == 400, f"payload naming the retired table is rejected (got {r.status_code})")
    db.refresh(active); db.refresh(retired)
    check((active.map_x_per_mille, active.map_y_per_mille) == (5, 5), "active table unchanged on rejection")
    check((retired.map_x_per_mille, retired.map_y_per_mille) == (7, 7), "retired table still untouched")
    app.dependency_overrides.clear()
    db.close()


def test_move_layout_rejects_duplicate_id_and_omission_together():
    # A single malformed payload combining both faults at once: t1 named
    # twice (duplicate) while t2, the zone's other active table, is never
    # named at all (omission). Either fault alone must already 400 (covered
    # above) — this confirms the combination still 400s, and, crucially,
    # that the duplicate-id check firing first doesn't skip past the
    # exact-set check and let a partial write through.
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    t1 = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4, map_x_per_mille=1, map_y_per_mille=1)
    t2 = RestaurantTable(number=2, zone_id=zone_a.id, capacity=4, map_x_per_mille=2, map_y_per_mille=2)
    db.add_all([t1, t2])
    db.commit()
    before = (zone_a.pos_x, zone_a.pos_y, zone_a.width, zone_a.height)
    c = _client(db, owner)

    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 10, "pos_y": 10, "width": 100, "height": 100,
        "tables": f"{t1.id}:5:5,{t1.id}:6:6",   # t1 duplicated, t2 omitted
    })
    check(r.status_code == 400, f"duplicate id + omitted table together rejected (got {r.status_code})")
    db.expire_all()
    zone_a2 = db.get(Zone, zone_a.id); t1b = db.get(RestaurantTable, t1.id); t2b = db.get(RestaurantTable, t2.id)
    check((zone_a2.pos_x, zone_a2.pos_y, zone_a2.width, zone_a2.height) == before,
          "zone rectangle unchanged — no partial write")
    check((t1b.map_x_per_mille, t1b.map_y_per_mille) == (1, 1), "duplicated table unchanged")
    check((t2b.map_x_per_mille, t2b.map_y_per_mille) == (2, 2), "omitted table unchanged")
    app.dependency_overrides.clear()
    db.close()


def test_move_layout_requires_owner():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    db.commit()
    c = _client(db, waiter)

    r = c.post(f"/admin/zones/{zone_a.id}/move-layout", data={
        "pos_x": 0, "pos_y": 0, "width": 100, "height": 100, "tables": "",
    })
    check(r.status_code == 403, f"non-Owner role rejected (got {r.status_code})")
    app.dependency_overrides.clear()
    db.close()


# --------------------------------------------------------------------------
# Page render — NULL-fallback path, mixed placed/unplaced tables
# --------------------------------------------------------------------------

def test_tables_page_renders_with_mixed_null_and_placed_positions():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    placed = RestaurantTable(number=1, zone_id=zone_a.id, capacity=4,
                              map_x_per_mille=200, map_y_per_mille=300)
    unplaced1 = RestaurantTable(number=2, zone_id=zone_a.id, capacity=4)
    unplaced2 = RestaurantTable(number=3, zone_id=zone_a.id, capacity=4)
    db.add_all([placed, unplaced1, unplaced2])
    db.commit()
    c = _client(db, owner)

    r = c.get(f"/admin/tables?floor={floor.id}")
    check(r.status_code == 200, f"page renders (got {r.status_code})")
    body = r.text
    check('class="freemap"' in body, "free-map container is present")
    check('data-id="%d"' % placed.id in body, "the placed table is rendered")
    check('data-id="%d"' % unplaced1.id in body and 'data-id="%d"' % unplaced2.id in body,
          "both never-placed tables are rendered (NULL-fallback path)")
    app.dependency_overrides.clear()
    db.close()


# --------------------------------------------------------------------------
# pointercancel — structural check only (see module docstring: no browser JS
# harness in this codebase). This does not execute the handler or prove its
# runtime behaviour; it confirms the page ships one pointercancel listener
# that never posts and does perform every required cleanup step, so a future
# edit that deletes or guts it fails a text assertion instead of failing
# silently in the browser.
# --------------------------------------------------------------------------

def _slice(body, start_marker, end_marker):
    """Isolate one addEventListener handler's source text between two literal
    markers, so an assertion below can't pass by matching identical code that
    happens to live in a neighbouring handler instead."""
    start = body.find(start_marker)
    end = body.find(end_marker, start)
    return body[start:end]


def test_admin_tables_page_has_pointercancel_cleanup():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    db.commit()
    c = _client(db, owner)

    r = c.get(f"/admin/tables?floor={floor.id}")
    check(r.status_code == 200, f"page renders (got {r.status_code})")
    body = r.text
    check("addEventListener('pointercancel'" in body, "a pointercancel listener is registered on the map")
    handler = _slice(body, "addEventListener('pointercancel'", "addEventListener('click'")
    check("drag = null" in handler, "gesture state is cleared")
    check("classList.remove('dragging')" in handler, "the .dragging class is always removed")
    check("releasePointerCapture" in handler, "pointer capture is explicitly released")
    check("mark(" in handler, "the affected element(s) are flagged .unsaved")
    check("post(" not in handler, "no autosave request is sent on cancellation")
    check("fetch(" not in handler,
          "no raw fetch() bypassing the post() helper is sent on cancellation either")
    check("d.tables.forEach" in handler, "move/resize flag the zone's tables together, not just the zone")
    check("e.pointerId !== drag.pointerId" in handler,
          "pointercancel from a pointer other than the gesture's owner is rejected")
    app.dependency_overrides.clear()
    db.close()


# --------------------------------------------------------------------------
# Single-owning-pointer policy — structural check only, same limitation as
# above (no browser/DOM test harness in this codebase to actually fire two
# simultaneous PointerEvents and observe which one wins). Each assertion
# targets one specific guard line inside its own isolated handler, so this
# is not a loose "the feature exists somewhere" check: removing any one
# guard (the pointerdown re-entrancy check, or any pointerId comparison)
# fails the corresponding assertion here, in the handler it must live in.
# --------------------------------------------------------------------------

def test_admin_tables_page_enforces_single_owning_pointer():
    db = _session()
    owner, waiter, floor, zone_a, zone_b = _seed(db)
    db.commit()
    c = _client(db, owner)

    r = c.get(f"/admin/tables?floor={floor.id}")
    check(r.status_code == 200, f"page renders (got {r.status_code})")
    body = r.text

    down = _slice(body, "addEventListener('pointerdown'", "addEventListener('pointermove'")
    move = _slice(body, "addEventListener('pointermove'", "addEventListener('pointerup'")
    up = _slice(body, "addEventListener('pointerup'", "addEventListener('pointercancel'")
    cancel = _slice(body, "addEventListener('pointercancel'", "addEventListener('click'")

    # pointerdown: a second gesture starting while one is active is ignored
    # outright, before any of the three kinds ever assigns `drag`.
    reentry_guard = down.find("if (drag) return;")
    check(reentry_guard != -1, "pointerdown ignores a new gesture while one is already active")
    first_assignment = down.find("drag = {")
    check(first_assignment != -1 and reentry_guard < first_assignment,
          "the re-entrancy guard runs before any drag object is ever created")

    # pointerId is recorded for all three gesture kinds (table/resize/move),
    # not just one — a second pointerdown that ever slipped past the guard
    # above would otherwise have no owner to compare against for one kind.
    check(down.count("pointerId: e.pointerId") == 3,
          f"pointerId is stored on all 3 drag kinds (found {down.count('pointerId: e.pointerId')})")

    check("e.pointerId !== drag.pointerId" in move,
          "pointermove from a pointer other than the gesture's owner is rejected")
    check("addEventListener('pointerup', function (e)" in body,
          "the pointerup handler receives the event (needed to read its pointerId)")
    check("e.pointerId !== drag.pointerId" in up,
          "pointerup from a pointer other than the gesture's owner is rejected")
    check("e.pointerId !== drag.pointerId" in cancel,
          "pointercancel from a pointer other than the gesture's owner is rejected (duplicated in the test above)")
    app.dependency_overrides.clear()
    db.close()


if __name__ == "__main__":
    for fn in (
        test_map_layout_moves_a_table,
        test_map_layout_clamps_out_of_range,
        test_map_layout_rejects_unknown_table_and_malformed_entry,
        test_map_layout_shape_valid_invalid_and_omitted,
        test_map_layout_zone_id_reassigns,
        test_map_layout_requires_owner,
        test_move_layout_moves_zone_and_its_tables_atomically,
        test_move_layout_rejects_omitted_table_writes_nothing,
        test_move_layout_rejects_extra_unknown_id,
        test_move_layout_rejects_duplicate_id,
        test_move_layout_rejects_duplicate_id_and_omission_together,
        test_move_layout_rejects_table_from_another_zone,
        test_move_layout_empty_payload_on_empty_and_occupied_zone,
        test_move_layout_inactive_table_excluded_and_rejected_if_named,
        test_move_layout_requires_owner,
        test_tables_page_renders_with_mixed_null_and_placed_positions,
        test_admin_tables_page_has_pointercancel_cleanup,
        test_admin_tables_page_enforces_single_owning_pointer,
    ):
        print(f"- {fn.__name__}")
        fn()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall floor-admin-ui tests passed")
