"""Reservations & waitlist page layout — wide-desktop 2-column split (picker
left, forms right), multi-column zone flow, side-by-side lower lists, and
compact horizontal reservation-form grouping.

Ported the old feat/floor-map prototype's own reservations layout (CSS
`columns`, `.res-split`) onto this app's own data/permissions — no backend,
schema, or business-logic change; `stats` (Today at a glance) was already
computed by app/routers/reservations.py, just never rendered here before
this pass; ditto the zflow/res-split CSS pattern, ported from the prototype.

Structural checks only (no browser harness in this repo — same declared
limitation as tests/test_floor_operational_map.py); the actual multi-column
flow, responsive collapse, and field-wrap behaviour were verified separately
with a real headless-Chromium session (disposable SQLite, never production)
at both a wide-desktop and a narrow-mobile viewport.

Run: python tests/test_reservations_layout.py
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
from app.models.oltp import Channel, Floor, RestaurantTable, Staff, Zone

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _session():
    path = os.path.join(tempfile.gettempdir(), f"reslayout_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _seed(db):
    owner = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
    floor = Floor(name="Main")
    ch = Channel(code="dine_in", name="Dine-in", channel_type="dine_in")
    db.add_all([owner, floor, ch])
    db.flush()
    zone = Zone(name="balcony", floor_id=floor.id)  # lowercase on purpose
    db.add(zone)
    db.flush()
    for i in range(1, 4):
        db.add(RestaurantTable(number=i, zone_id=zone.id, capacity=2, is_active=True, status="free"))
    db.commit()
    return owner


def _client(db, staff):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


def test_page_is_full_width_and_uses_the_split_layout():
    db = _session()
    owner = _seed(db)
    c = _client(db, owner)
    body = c.get("/reservations").text
    check("wrap-wide" in body, "the page opts out of the normal ~1240px column, same reason floor.html does")
    check('class="res-split"' in body, "the picker/forms area uses the wide-desktop split, not the old fractional grid")
    check("res-grid" not in body, "the old fractional-width grid class is fully gone, not just unused")
    app.dependency_overrides.clear()
    db.close()


def test_zone_panels_flow_into_columns_not_one_long_stack():
    db = _session()
    owner = _seed(db)
    c = _client(db, owner)
    body = c.get("/reservations").text
    check('class="zflow"' in body, "zone panels sit inside a multi-column flow container")
    check('class="zpanel reszone-panel"' in body,
          "each zone panel keeps its existing .zpanel styling/behaviour and gains the flow-column class alongside it")
    css = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "static", "app.css"),
        encoding="utf-8",
    ).read()
    zflow_start = css.index("\n.zflow {") + 1
    zflow_block = css[zflow_start:css.index("}", zflow_start) + 1]
    check("columns:" in zflow_block.replace(" ", ""), "the flow is native CSS columns (width-driven), not a hardcoded column count")
    panel_start = css.index("\n.reszone-panel {") + 1
    panel_block = css[panel_start:css.index("}", panel_start) + 1]
    check("break-inside: avoid" in panel_block, "a zone panel never splits across two columns mid-panel")


def test_zone_name_display_is_capitalized_without_touching_stored_data():
    db = _session()
    owner = _seed(db)
    c = _client(db, owner)
    body = c.get("/reservations").text
    check(">balcony<" in body, "the raw, lowercase zone name from the database is untouched in the markup")
    css = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "static", "app.css"),
        encoding="utf-8",
    ).read()
    start = css.index("\n.reszone-panel .zp-name {") + 1
    block = css[start:css.index("}", start) + 1]
    check("text-transform: capitalize" in block, "capitalization is a display-only CSS rule")
    check(".zpanel .zp-name {" not in css.replace("reszone-panel ", ""),
          "the capitalize rule is scoped to the reservations picker, not applied globally to every .zp-name (e.g. floor.html's own zone headers)")


def test_upcoming_and_waitlist_sit_side_by_side_under_the_picker():
    db = _session()
    owner = _seed(db)
    c = _client(db, owner)
    body = c.get("/reservations").text
    lists_start = body.index('class="res-lists"')
    lists_block = body[lists_start:lists_start + 4000]
    check("Upcoming reservations" in lists_block, "Upcoming reservations sits inside the side-by-side lists row")
    check("Walk-in waitlist" in lists_block, "Walk-in waitlist sits inside the same side-by-side lists row, not stacked under the forms")
    check(lists_block.index("Upcoming reservations") < lists_block.index("Walk-in waitlist"),
          "Upcoming reservations renders first (left), waitlist second (right)")
    app.dependency_overrides.clear()
    db.close()


def test_book_form_groups_fields_into_three_compact_rows():
    db = _session()
    owner = _seed(db)
    c = _client(db, owner)
    body = c.get("/reservations").text
    form_start = body.index('id="bookform"')
    form_block = body[form_start:body.index("</form>", form_start)]
    lines = form_block.split('class="formline"')
    check(len(lines) - 1 == 3, f"the book form is exactly 3 field rows, not 5 (found {len(lines) - 1})")
    row1, row2 = lines[1], lines[2]
    check("Guest name" in row1 and "Party size" in row1 and "Date" in row1,
          "row 1 groups guest name, party size and date on one line")
    check("Time" in row2 and "Phone" in row2 and "Tables" in row2,
          "row 2 groups time, phone and the table summary on one line")
    app.dependency_overrides.clear()
    db.close()


if __name__ == "__main__":
    for fn in (
        test_page_is_full_width_and_uses_the_split_layout,
        test_zone_panels_flow_into_columns_not_one_long_stack,
        test_zone_name_display_is_capitalized_without_touching_stored_data,
        test_upcoming_and_waitlist_sit_side_by_side_under_the_picker,
        test_book_form_groups_fields_into_three_compact_rows,
    ):
        print(f"- {fn.__name__}")
        fn()
    print()
    if _fail:
        print(f"{len(_fail)} FAILED")
        sys.exit(1)
    print("all reservations-layout tests passed")
