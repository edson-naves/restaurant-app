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


def test_zones_split_into_two_independent_columns_not_a_shared_row_grid():
    """CSS multi-column's default height-balancing (and a shared-row grid)
    both leave a gap under a short zone while it waits to line up with a
    taller one — reported by the user against the live deploy. Two literal,
    independent column stacks (alternating by index, so the visual order
    stays stable) avoid that entirely: each column's height depends only on
    its own content."""
    db = _session()
    owner = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
    floor = Floor(name="Main")
    ch = Channel(code="dine_in", name="Dine-in", channel_type="dine_in")
    db.add_all([owner, floor, ch])
    db.flush()
    # The page sorts zones by (sort_order, name) — set sort_order explicitly
    # so this test controls the order directly, rather than relying on these
    # particular names happening to already be alphabetical.
    names = ["Main", "Window", "Patio", "Bar"]
    for i, name in enumerate(names):
        z = Zone(name=name, floor_id=floor.id, sort_order=i)
        db.add(z)
        db.flush()
        db.add(RestaurantTable(number=100 + i, zone_id=z.id, capacity=2, is_active=True, status="free"))
    db.commit()
    c = _client(db, owner)
    body = c.get("/reservations").text

    check('class="zsplit"' in body, "the picker uses the two-column split wrapper")
    check(body.count('class="zsplit-col"') == 2, "exactly two independent column stacks, not a variable/auto column count")
    check('class="zpanel reszone-panel"' in body,
          "each zone panel keeps its existing .zpanel styling/behaviour and gains the split-item class alongside it")
    check("zflow" not in body and "columns:" not in body, "the old CSS multi-column mechanism is fully gone, not left dormant")

    col1 = body[body.index('class="zsplit-col"'):]
    col1_end = col1.index('class="zsplit-col"', 1)
    col1_block = col1[:col1_end]
    col2_block = col1[col1_end:]
    # Zones seeded in order Main, Window, Patio, Bar -> alternating split
    # puts Main/Patio in column 1, Window/Bar in column 2.
    check(col1_block.index("Main") < col1_block.index("Patio"),
          "column 1 holds the 1st and 3rd zones, in their original relative order")
    check(col2_block.index("Window") < col2_block.index("Bar"),
          "column 2 holds the 2nd and 4th zones, in their original relative order")
    check("Window" not in col1_block and "Bar" not in col1_block,
          "column 1 never picks up a zone that belongs in column 2")

    css = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "static", "app.css"),
        encoding="utf-8",
    ).read()
    split_start = css.index("\n.zsplit {") + 1
    split_block = css[split_start:css.index("}", split_start) + 1]
    check("flex" in split_block, "plain flexbox side-by-side, not a shared-row grid or CSS multi-column")
    app.dependency_overrides.clear()
    db.close()


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
        test_zones_split_into_two_independent_columns_not_a_shared_row_grid,
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
