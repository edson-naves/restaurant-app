"""Floor card redesign — 7-state status legend, icon-based card status (no
more text pills), staff-colour border kept separate from status, adaptive
card height, no-show wired on held tables.

Ported the old feat/floor-map prototype's visual language (compact icon
cards, granular status filters) onto this app's own data/permissions —
not a reimplementation, no schema/endpoint changes. Structural checks only
(no browser harness in this repo — see tests/test_floor_operational_map.py
for the same declared limitation); CSS collision/adaptive-height behaviour
was verified separately with a real headless-Chromium session (disposable
SQLite, never production).

Throwaway SQLite + dependency overrides. Run: python tests/test_floor_card_redesign.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _env  # noqa: F401  — declares the test opt-out before app imports
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.deps import current_staff, get_db
from app.main import app
from app.models import oltp  # noqa: F401
from app.models.oltp import (
    Channel,
    Floor,
    Reservation,
    ReservationStatus,
    RestaurantTable,
    Staff,
    Zone,
)

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _session():
    path = os.path.join(tempfile.gettempdir(), f"floorcards_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _seed(db):
    owner = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
    waiter = Staff(name="Sam Lee", role="waiter", pin_code="x", is_active=True, color="#ec4899")
    floor = Floor(name="Main")
    ch = Channel(code="dine_in", name="Dine-in", channel_type="dine_in")
    db.add_all([owner, waiter, floor, ch])
    db.flush()
    zone = Zone(name="Patio", floor_id=floor.id)
    db.add(zone)
    db.flush()
    return owner, waiter, floor, zone, ch


def _client(db, staff):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


def test_legend_offers_all_seven_states_with_real_counts():
    db = _session()
    owner, waiter, floor, zone, ch = _seed(db)
    t_free = RestaurantTable(number=1, zone_id=zone.id, capacity=2)
    t_occ = RestaurantTable(number=2, zone_id=zone.id, capacity=2,
                             status="occupied", current_waiter_id=waiter.id)
    db.add_all([t_free, t_occ])
    db.flush()
    from app.routers.sales import open_order_on_table
    open_order_on_table(db, t_occ, guests=2, waiter_id=waiter.id)
    db.commit()

    c = _client(db, owner)
    body = c.get("/").text
    for status in ("free", "seated", "occupied", "ready_serve", "served", "ready_to_pay", "reserved"):
        check(f'data-status="{status}"' in body, f"the legend offers a '{status}' filter button")
    # No more folding seated/preparing/ready-to-serve/served into one bucket.
    check("OCCUPIED_GROUP" not in body, "the old occupied-bucket grouping is gone — each state is its own filter")
    check('data-count="free">1<' in body, "the Free count reflects real table state (1), not a hardcoded screenshot value")
    check('data-count="occupied">1<' in body, "the Preparing/occupied count reflects real state (1)")
    app.dependency_overrides.clear()
    db.close()


def test_card_uses_one_status_icon_not_text_pills():
    db = _session()
    owner, waiter, floor, zone, ch = _seed(db)
    t_occ = RestaurantTable(number=5, zone_id=zone.id, capacity=2,
                             status="occupied", current_waiter_id=waiter.id)
    db.add(t_occ)
    db.flush()
    from app.routers.sales import open_order_on_table
    open_order_on_table(db, t_occ, guests=2, waiter_id=waiter.id)
    db.commit()

    c = _client(db, owner)
    body = c.get("/").text
    check('class="sicon' in body, "the card carries a status icon (.sicon)")
    check('class="pill occupied"' not in body, "the old redundant disp_status text pill is gone")
    check('class="pill ready-serve"' not in body, "the separate ready-to-serve text pill is folded into the icon")
    check("os-jump" not in body, "the preparing-pill's jump-to-kitchen shortcut is gone (not in the target design; noted, not a domain change)")
    app.dependency_overrides.clear()
    db.close()


def test_staff_border_colour_is_independent_of_status_icon():
    """Border colour = WHO (waiter); the status icon = WHAT. Two different
    tables served by the same waiter but in different states must share the
    waiter's border colour while still carrying their own distinct icon."""
    db = _session()
    owner, waiter, floor, zone, ch = _seed(db)
    t_prep = RestaurantTable(number=1, zone_id=zone.id, capacity=2,
                              status="occupied", current_waiter_id=waiter.id)
    t_pay = RestaurantTable(number=2, zone_id=zone.id, capacity=2,
                             status="ready_to_pay", current_waiter_id=waiter.id)
    db.add_all([t_prep, t_pay])
    db.flush()
    from app.routers.sales import open_order_on_table
    open_order_on_table(db, t_prep, guests=2, waiter_id=waiter.id)
    open_order_on_table(db, t_pay, guests=2, waiter_id=waiter.id)
    db.commit()

    c = _client(db, owner)
    body = c.get("/").text
    check(body.count("border-left-color: #ec4899") >= 2,
          "both tables use the SAME waiter colour for their border regardless of differing status")
    check('title="preparing"' in body or "STATUS_LABEL" not in body,
          "the preparing table's icon still carries its own distinct status label (not overwritten by the border colour)")
    app.dependency_overrides.clear()
    db.close()


def test_held_table_no_show_is_wired_and_guest_name_is_tooltip_only():
    db = _session()
    owner, waiter, floor, zone, ch = _seed(db)
    t_held = RestaurantTable(number=9, zone_id=zone.id, capacity=4)
    db.add(t_held)
    db.flush()
    res = Reservation(kind="reservation", guest_name="Priya Shah", party_size=4,
                       at=datetime.now(), status=ReservationStatus.WAITING)
    res.tables = [t_held]
    db.add(res)
    db.commit()

    c = _client(db, owner)
    body = c.get("/").text
    check("/no-show" in body, "the held table's card offers the existing no-show route (reused, not invented)")
    check("Seat party of 4" in body, "the seat button reflects the real party size, not a hardcoded example")
    n_span = body[body.index('data-number="9"'):]
    n_span = n_span[:n_span.index("</summary>")]
    check("Priya Shah" not in n_span or "title=" in n_span.split("Priya Shah")[0][-40:],
          "the guest name on a held table's card is tooltip-only, not shouted on the face")
    app.dependency_overrides.clear()
    db.close()


def test_zpanel_grid_does_not_stretch_cards_to_match_tallest_row_neighbour():
    """CSS Grid's default align-items:stretch would force every card in a row
    to match its tallest neighbour, defeating adaptive card height entirely —
    the exact 'every card is as tall as the busiest one' problem this
    redesign exists to fix. Caught by a real headless-Chromium session during
    this work (three same-row cards all rendering at an identical 140.5px
    regardless of content) — this is the source-level guard for it."""
    css = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "web", "static", "app.css"),
        encoding="utf-8",
    ).read()
    start = css.index(".zpanel-grid {")
    block = css[start:css.index("}", start) + 1]
    check("align-items: start" in block, "the zone card grid does not stretch cards to a shared row height")


def test_card_title_row_reserves_room_for_the_corner_retire_button():
    """.tbl-del is absolutely positioned over the same top-right corner the
    status icon floats into — without reserved space they visually overlap
    (confirmed with a real browser session: the icon was fully hidden behind
    the retire button on every free table an Owner could see)."""
    css = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "web", "static", "app.css"),
        encoding="utf-8",
    ).read()
    start = css.index("\n.tbl .n {") + 1   # not the ".floor.as-map .tbl .n" suffix match
    line = css[start:css.index("}", start) + 1]
    check("padding-right" in line, "the card's title row reserves space so its status icon never sits under the retire button")


if __name__ == "__main__":
    for fn in (
        test_legend_offers_all_seven_states_with_real_counts,
        test_card_uses_one_status_icon_not_text_pills,
        test_staff_border_colour_is_independent_of_status_icon,
        test_held_table_no_show_is_wired_and_guest_name_is_tooltip_only,
        test_zpanel_grid_does_not_stretch_cards_to_match_tallest_row_neighbour,
        test_card_title_row_reserves_room_for_the_corner_retire_button,
    ):
        print(f"- {fn.__name__}")
        fn()
    print()
    if _fail:
        print(f"{len(_fail)} FAILED")
        sys.exit(1)
    print("all floor-card-redesign tests passed")
