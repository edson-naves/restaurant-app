"""Kitchen Stations — Stage A admin CRUD + routing safety.

Covers create/edit/move/deactivate on /admin/stations, the type-conditional
field handling, validation, and the key safety rule: deactivating a station
re-routes its menu items to Unassigned (never a silent orphan). Throwaway
SQLite + dependency overrides. Run: python tests/test_stations.py
"""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.deps import current_staff, get_db
from app.main import app
from app.models import oltp  # noqa: F401
from app.models.oltp import MenuCategory, MenuItem, Staff, Station

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _session():
    path = os.path.join(tempfile.gettempdir(), f"stations_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _client(db, staff):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


def _owner(db):
    o = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
    db.add(o)
    db.commit()
    return o


def test_create_and_page_renders():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    r = c.post("/admin/stations/create", data={
        "name": "Pizza", "type": "production", "color": "#e11d48", "icon": "🍕",
        "code": "PIZ", "target_prep_min": "12", "printer_ref": "Printer 1",
        "visible_to_kitchen": 1,
    }, follow_redirects=False)
    check(r.status_code == 303, "create redirects")
    s = db.query(Station).filter(Station.name == "Pizza").one()
    check(s.type == "production" and s.color == "#e11d48" and s.target_prep_min == 12,
          "production station stored with prep target + colour")
    check(s.display_order == 1, "first station gets display_order 1")
    page = c.get("/admin/stations")
    check(page.status_code == 200 and "Kitchen stations" in page.text, "stations page renders")
    check("Stations" in page.text, "nav shows the Stations tab")
    app.dependency_overrides.clear()
    db.close()


def test_coordination_clears_production_fields():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    c.post("/admin/stations/create", data={"name": "Expo", "type": "coordination",
           "target_prep_min": "9", "printer_ref": "P2", "color": "#3b82f6"},
           follow_redirects=False)
    s = db.query(Station).filter(Station.name == "Expo").one()
    check(s.target_prep_min is None and s.printer_ref == "",
          "a coordination station drops prep target + printer")
    app.dependency_overrides.clear()
    db.close()


def test_validation():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    r1 = c.post("/admin/stations/create", data={"name": "  ", "type": "production"},
                follow_redirects=False)
    check(r1.status_code == 400, "blank name rejected")
    r2 = c.post("/admin/stations/create", data={"name": "Bad", "type": "production",
                "color": "red"}, follow_redirects=False)
    check(r2.status_code == 400, "non-hex colour rejected")
    r3 = c.post("/admin/stations/create", data={"name": "Bad2", "type": "wat"},
                follow_redirects=False)
    check(r3.status_code == 400, "invalid type rejected")
    app.dependency_overrides.clear()
    db.close()


def test_move_reorders():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    for n in ("A", "B", "C"):
        c.post("/admin/stations/create", data={"name": n, "type": "production"},
               follow_redirects=False)
    b = db.query(Station).filter(Station.name == "B").one()
    c.post(f"/admin/stations/{b.id}/move", data={"dir": "up"}, follow_redirects=False)
    order = [s.name for s in db.query(Station).order_by(Station.display_order).all()]
    check(order[:2] == ["B", "A"], "moving B up puts it before A")
    app.dependency_overrides.clear()
    db.close()


def test_deactivate_reassigns_items_to_unassigned():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    c.post("/admin/stations/create", data={"name": "Grill", "type": "production"},
           follow_redirects=False)
    grill = db.query(Station).filter(Station.name == "Grill").one()
    cat = MenuCategory(name="Mains"); db.add(cat); db.flush()
    item = MenuItem(category_id=cat.id, name="Steak", price_cents=2500, station_id=grill.id)
    db.add(item); db.commit()

    r = c.post(f"/admin/stations/{grill.id}/active", data={"active": 0}, follow_redirects=False)
    check(r.status_code == 303, "deactivate redirects")
    db.refresh(grill); db.refresh(item)
    check(grill.is_active is False, "station is now inactive")
    check(item.station_id is None, "its routed item is re-routed to Unassigned (no silent orphan)")
    app.dependency_overrides.clear()
    db.close()


def test_menu_item_routing():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    c.post("/admin/stations/create", data={"name": "Fryer", "type": "production"},
           follow_redirects=False)
    fryer = db.query(Station).filter(Station.name == "Fryer").one()
    cat = MenuCategory(name="Apps"); db.add(cat); db.commit()

    # Create routed to Fryer.
    c.post("/admin/menu/items/create", data={
        "category_id": cat.id, "name": "Wings", "price": "9.00", "station_id": fryer.id,
    }, follow_redirects=False)
    item = db.query(MenuItem).filter(MenuItem.name == "Wings").one()
    check(item.station_id == fryer.id, "new item routes to the chosen station")

    # Edit back to Unassigned (0 -> None).
    c.post(f"/admin/menu/items/{item.id}/edit", data={
        "category_id": cat.id, "name": "Wings", "price": "9.00", "station_id": 0,
    }, follow_redirects=False)
    db.refresh(item)
    check(item.station_id is None, "station_id 0 clears routing to Unassigned")

    # Invalid station id is rejected.
    r = c.post(f"/admin/menu/items/{item.id}/edit", data={
        "category_id": cat.id, "name": "Wings", "price": "9.00", "station_id": 9999,
    }, follow_redirects=False)
    check(r.status_code == 400, "routing to a non-existent station is rejected")
    app.dependency_overrides.clear()
    db.close()


def _kitchen_fixture(db):
    """A station, a routed item and an un-routed item, plus a dine-in channel."""
    from app.models.oltp import Channel
    grill = Station(name="Grill", type="production", color="#f59e0b", is_active=True)
    ch = Channel(code="dine_in", name="Dine-in", channel_type="dine_in")
    cat = MenuCategory(name="Mains")
    db.add_all([grill, ch, cat]); db.flush()
    steak = MenuItem(category_id=cat.id, name="ZZSteak", price_cents=2500, station_id=grill.id)
    water = MenuItem(category_id=cat.id, name="ZZWater", price_cents=300, station_id=None)
    db.add_all([steak, water]); db.commit()
    return grill, ch, steak, water


def test_fire_snapshots_station():
    from datetime import datetime
    from app.models.oltp import Order, OrderItem, KitchenStatus, OrderStatus
    db = _session()
    owner = _owner(db)
    grill, ch, steak, water = _kitchen_fixture(db)
    order = Order(code="ORD-FS-1", channel_id=ch.id, status=OrderStatus.OPEN, guest_count=2,
                  opened_at=datetime.now())
    db.add(order); db.flush()
    line = OrderItem(order_id=order.id, menu_item_id=steak.id, quantity=1,
                     unit_price_cents=2500, kitchen_status=KitchenStatus.PENDING)
    db.add(line); db.commit()

    c = _client(db, owner)
    c.post(f"/orders/{order.id}/send", data={"course": 0}, follow_redirects=False)
    db.refresh(line)
    check(line.kitchen_status == KitchenStatus.PREPARING, "firing moves the line to preparing")
    check(line.station_id == grill.id, "firing snapshots the item's station onto the line")

    # Re-routing the menu item afterwards must not move the already-fired line.
    steak.station_id = None
    db.commit()
    db.refresh(line)
    check(line.station_id == grill.id, "the fired line keeps its snapshot after re-routing")
    app.dependency_overrides.clear()
    db.close()


def test_kds_station_filter_and_unassigned():
    from datetime import datetime
    from app.models.oltp import Order, OrderItem, KitchenStatus, OrderStatus
    db = _session()
    owner = _owner(db)
    grill, ch, steak, water = _kitchen_fixture(db)
    order = Order(code="ORD-KF-1", channel_id=ch.id, status=OrderStatus.PREPARING, guest_count=2,
                  opened_at=datetime.now(), kitchen_status=KitchenStatus.PREPARING,
                  sent_to_kitchen_at=datetime.now())
    db.add(order); db.flush()
    db.add(OrderItem(order_id=order.id, menu_item_id=steak.id, quantity=1, unit_price_cents=2500,
                     kitchen_status=KitchenStatus.PREPARING, station_id=grill.id))
    db.add(OrderItem(order_id=order.id, menu_item_id=water.id, quantity=1, unit_price_cents=300,
                     kitchen_status=KitchenStatus.PREPARING, station_id=None))
    db.commit()

    c = _client(db, owner)
    all_board = c.get("/kitchen?station=all").text
    check("ZZSteak" in all_board and "ZZWater" in all_board, "All board shows every station's lines")
    check("Unassigned" in all_board, "All board offers the Unassigned tab (un-routed line present)")

    grill_board = c.get(f"/kitchen?station={grill.id}").text
    check("ZZSteak" in grill_board and "ZZWater" not in grill_board,
          "the Grill station shows only its own line")

    un = c.get("/kitchen?station=unassigned").text
    check("ZZWater" in un and "ZZSteak" not in un, "the Unassigned bucket shows only un-routed lines")
    app.dependency_overrides.clear()
    db.close()


def test_expo_aggregation():
    from datetime import datetime
    from app.models.oltp import Order, OrderItem, KitchenStatus, OrderStatus
    db = _session()
    owner = _owner(db)
    grill, ch, steak, water = _kitchen_fixture(db)   # steak -> Grill
    bar = Station(name="Bar", type="production", is_active=True); db.add(bar); db.flush()
    cat = db.query(MenuCategory).first()
    mojito = MenuItem(category_id=cat.id, name="ZZMojito", price_cents=900, station_id=bar.id)
    db.add(mojito); db.commit()

    order = Order(code="ORD-EX-1", channel_id=ch.id, status=OrderStatus.PREPARING, guest_count=2,
                  opened_at=datetime.now(), kitchen_status=KitchenStatus.PREPARING,
                  sent_to_kitchen_at=datetime.now())
    db.add(order); db.flush()
    db.add(OrderItem(order_id=order.id, menu_item_id=steak.id, quantity=1, unit_price_cents=2500,
                     kitchen_status=KitchenStatus.READY, station_id=grill.id, course=2))
    moj = OrderItem(order_id=order.id, menu_item_id=mojito.id, quantity=1, unit_price_cents=900,
                    kitchen_status=KitchenStatus.PREPARING, station_id=bar.id, course=2)
    db.add(moj); db.commit()

    c = _client(db, owner)
    body = c.get("/expo").text
    check("Grill" in body and "Bar" in body, "expo card lists every station in the course")
    check("Waiting on Bar" in body, "overall waits on the station that isn't ready")
    # "expo-overall ok" is the ready-to-serve banner class (distinct from the
    # sidebar's own 'Ready to serve' stat row, so match the class, not the words).
    check("expo-overall ok" not in body, "not ready to serve while the bar is still preparing")

    moj.kitchen_status = KitchenStatus.READY
    order.kitchen_status = KitchenStatus.READY
    db.commit()
    body2 = c.get("/expo").text
    check("expo-overall ok" in body2, "once every station is ready the course reads ready to serve")
    app.dependency_overrides.clear()
    db.close()


def test_floor_station_strip():
    from datetime import datetime
    from app.models.oltp import (Order, OrderItem, KitchenStatus, OrderStatus,
                                 Floor, Zone, RestaurantTable, TableStatus)
    db = _session()
    owner = _owner(db)
    grill, ch, steak, water = _kitchen_fixture(db)     # steak -> Grill
    bar = Station(name="Bar", type="production", is_active=True); db.add(bar); db.flush()
    cat = db.query(MenuCategory).first()
    moj = MenuItem(category_id=cat.id, name="ZZMojito", price_cents=900, station_id=bar.id)
    floor = Floor(name="F1"); db.add_all([moj, floor]); db.flush()
    zone = Zone(name="Main", floor_id=floor.id)
    waiter = Staff(name="W", role="waiter", pin_code="y", is_active=True)
    db.add_all([zone, waiter]); db.flush()
    t = RestaurantTable(number=1, zone_id=zone.id, zone="Main", capacity=4, pos_x=200, pos_y=200,
                        status=TableStatus.OCCUPIED, current_waiter_id=waiter.id)
    db.add(t); db.flush()
    o = Order(code="ORD-FS", table_id=t.id, channel_id=ch.id, waiter_id=waiter.id,
              status=OrderStatus.PREPARING, guest_count=2, opened_at=datetime.now(),
              kitchen_status=KitchenStatus.PREPARING, sent_to_kitchen_at=datetime.now())
    db.add(o); db.flush()
    db.add(OrderItem(order_id=o.id, menu_item_id=steak.id, quantity=1, unit_price_cents=2500,
                     kitchen_status=KitchenStatus.READY, station_id=grill.id))
    db.add(OrderItem(order_id=o.id, menu_item_id=moj.id, quantity=1, unit_price_cents=900,
                     kitchen_status=KitchenStatus.PREPARING, station_id=bar.id))
    db.commit()

    c = _client(db, owner)
    body = c.get("/").text
    check('class="tbl-stations"' in body, "occupied card shows the per-station strip")
    check("Grill · ready" in body, "Grill chip reads ready (its line is up)")
    check("Bar · preparing" in body, "Bar chip reads preparing (still cooking)")
    app.dependency_overrides.clear()
    db.close()


def test_routing_template_export():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    cat = MenuCategory(name="Mains"); db.add(cat); db.flush()
    it = MenuItem(category_id=cat.id, name="ZZRibeye", price_cents=3200)
    db.add(it); db.commit()
    r = c.get("/admin/stations/routing/template")
    check(r.status_code == 200, "template downloads")
    check("text/csv" in r.headers.get("content-type", ""), "served as CSV")
    check("attachment" in r.headers.get("content-disposition", ""), "as a file download")
    check("item_id" in r.text and "station" in r.text, "template has the header columns")
    check("ZZRibeye" in r.text and str(it.id) in r.text, "template lists the active item")
    app.dependency_overrides.clear()
    db.close()


def test_routing_import_applies_and_reports():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    grill = Station(name="Grill", type="production", is_active=True)
    db.add(grill); db.flush()
    cat = MenuCategory(name="Mains"); db.add(cat); db.flush()
    a = MenuItem(category_id=cat.id, name="Steak", price_cents=2500)
    b = MenuItem(category_id=cat.id, name="Water", price_cents=200, station_id=grill.id)
    cc = MenuItem(category_id=cat.id, name="Fries", price_cents=600)
    db.add_all([a, b, cc]); db.commit()

    csv_text = (
        "item_id,station\n"
        f"{a.id},Grill\n"        # assign
        f"{b.id},\n"             # blank -> clear
        "999999,Grill\n"          # missing item
        f"{cc.id},Nope\n"        # unknown station
    )
    r = c.post("/admin/stations/routing/import",
               files={"file": ("routing.csv", csv_text.encode("utf-8"), "text/csv")})
    check(r.status_code == 200, "import returns the page with a result")
    db.refresh(a); db.refresh(b); db.refresh(cc)
    check(a.station_id == grill.id, "row routes the item to the named station")
    check(b.station_id is None, "a blank station clears routing to Unassigned")
    check(cc.station_id is None, "an unknown station name is NOT applied")
    check("Nope" in r.text, "the unknown station name is reported back")
    app.dependency_overrides.clear()
    db.close()


def _make_xlsx(rows):
    """Minimal .xlsx (shared strings + one sheet) — enough for the importer's
    stdlib reader. int cells are numeric; everything else a shared string."""
    import zipfile as _zip
    NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared, sidx = [], {}

    def sid(s):
        if s not in sidx:
            sidx[s] = len(shared); shared.append(s)
        return sidx[s]

    def colref(ci):
        s, ci = "", ci + 1
        while ci:
            ci, r = divmod(ci - 1, 26)
            s = chr(65 + r) + s
        return s

    sheet_rows = []
    for ri, row in enumerate(rows, 1):
        cells = []
        for ci, val in enumerate(row):
            ref = f"{colref(ci)}{ri}"
            if isinstance(val, int):
                cells.append(f'<c r="{ref}"><v>{val}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{sid(str(val))}</v></c>')
        sheet_rows.append(f'<row r="{ri}">{"".join(cells)}</row>')
    sheet_xml = f'<worksheet xmlns="{NS}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    ss = "".join(f"<si><t>{s}</t></si>" for s in shared)
    ss_xml = f'<sst xmlns="{NS}" count="{len(shared)}" uniqueCount="{len(shared)}">{ss}</sst>'
    buf = io.BytesIO()
    with _zip.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", ss_xml)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


def test_routing_import_xlsx():
    import io as _io  # noqa: F401 — io imported at top; alias keeps helper self-contained
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    cat = MenuCategory(name="Mains"); db.add(cat); db.flush()
    a = MenuItem(category_id=cat.id, name="Steak", price_cents=2500); db.add(a); db.commit()
    xlsx = _make_xlsx([["item_id", "station"], [a.id, "Grill"]])
    r = c.post("/admin/stations/routing/import", files={
        "file": ("menu_items_with_stations.xlsx", xlsx,
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    check(r.status_code == 200, "xlsx upload returns the page")
    db.refresh(a)
    check(a.station_id == grill.id, "an .xlsx file routes items just like CSV")
    app.dependency_overrides.clear()
    db.close()


import io  # noqa: E402 — used by _make_xlsx above


def test_routing_import_tolerant_header():
    """A friendly header like 'Item ID' / 'Station' (spaces, caps, extra cols)
    must map to the right columns — not fall back to reading Category."""
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    cat = MenuCategory(name="Starters"); db.add(cat); db.flush()
    a = MenuItem(category_id=cat.id, name="Wings", price_cents=1250); db.add(a); db.commit()
    csv_text = f"Item ID,Category,Menu Item,Price,Station\n{a.id},Starters,Wings,12.50,Grill\n"
    c.post("/admin/stations/routing/import",
           files={"file": ("f.csv", csv_text.encode(), "text/csv")})
    db.refresh(a)
    check(a.station_id == grill.id,
          "a spaced 'Item ID'/'Station' header maps correctly (not to Category)")
    app.dependency_overrides.clear()
    db.close()


def test_routing_import_headerless():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    bar = Station(name="Bar", type="production", is_active=True); db.add(bar); db.flush()
    cat = MenuCategory(name="Drinks"); db.add(cat); db.flush()
    a = MenuItem(category_id=cat.id, name="Cola", price_cents=300); db.add(a); db.commit()
    # ChatGPT-style 2-column output, no header.
    r = c.post("/admin/stations/routing/import",
               files={"file": ("r.csv", f"{a.id},Bar\n".encode(), "text/csv")})
    db.refresh(a)
    check(a.station_id == bar.id, "a headerless item_id,station file also applies")
    app.dependency_overrides.clear()
    db.close()


def test_strict_item_id_parse():
    from app.routers.admin import _parse_item_id
    check(_parse_item_id("12") == 12 and _parse_item_id("12.0") == 12, "accepts 12 and Excel 12.0")
    check(_parse_item_id("12.5") is None, "rejects 12.5")
    check(_parse_item_id("1e2") is None, "rejects scientific notation")
    check(_parse_item_id("-5") is None and _parse_item_id("0") is None, "rejects non-positive")


def test_routing_rejects_coordination_station():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    expo = Station(name="Expo", type="coordination", is_active=True); db.add(expo)
    cat = MenuCategory(name="Mains"); db.add(cat); db.flush()
    it = MenuItem(category_id=cat.id, name="Steak", price_cents=2500); db.add(it); db.commit()
    r = c.post(f"/admin/menu/items/{it.id}/edit", data={
        "category_id": cat.id, "name": "Steak", "price": "25.00", "station_id": expo.id,
    }, follow_redirects=False)
    check(r.status_code == 400, "an item can't be routed to a coordination station")
    app.dependency_overrides.clear()
    db.close()


def test_duplicate_station_name_rejected():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    c.post("/admin/stations/create", data={"name": "Grill", "type": "production"}, follow_redirects=False)
    r = c.post("/admin/stations/create", data={"name": "grill", "type": "production"}, follow_redirects=False)
    check(r.status_code == 400, "a case-insensitive duplicate station name is rejected")
    app.dependency_overrides.clear()
    db.close()


def test_import_rejects_oversized():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    big = b"item_id,station\n" + b"1,Grill\n" * 800000   # ~6.4 MB
    r = c.post("/admin/stations/routing/import",
               files={"file": ("big.csv", big, "text/csv")})
    check("too large" in r.text.lower(), "an oversized upload is rejected")
    app.dependency_overrides.clear()
    db.close()


def test_kds_filter_before_limit():
    from datetime import datetime, timedelta
    from app.models.oltp import Order, OrderItem, KitchenStatus, OrderStatus
    from app.routers import sales
    db = _session()
    owner = _owner(db)
    grill, ch, steak, water = _kitchen_fixture(db)   # steak->Grill, water->None
    old = Order(code="OLD", channel_id=ch.id, status=OrderStatus.PREPARING, guest_count=1,
                opened_at=datetime.now() - timedelta(hours=2), kitchen_status=KitchenStatus.PREPARING,
                sent_to_kitchen_at=datetime.now() - timedelta(hours=2))
    db.add(old); db.flush()
    db.add(OrderItem(order_id=old.id, menu_item_id=steak.id, quantity=1, unit_price_cents=2500,
                     kitchen_status=KitchenStatus.PREPARING, station_id=grill.id))
    new = Order(code="NEW", channel_id=ch.id, status=OrderStatus.PREPARING, guest_count=1,
                opened_at=datetime.now(), kitchen_status=KitchenStatus.PREPARING,
                sent_to_kitchen_at=datetime.now())
    db.add(new); db.flush()
    db.add(OrderItem(order_id=new.id, menu_item_id=water.id, quantity=1, unit_price_cents=300,
                     kitchen_status=KitchenStatus.PREPARING, station_id=None))
    db.commit()
    c = _client(db, owner)
    orig = sales.KITCHEN_LIMIT
    sales.KITCHEN_LIMIT = 1                       # force the cap to bite
    try:
        body = c.get("/kitchen?station=unassigned").text
        check("ZZWater" in body and "ZZSteak" not in body,
              "an Unassigned order shows under its tab even though it isn't the oldest (filter before LIMIT)")
        allb = c.get("/kitchen?station=all").text
        check("on the board" in allb, "the board shows the 'of M' truncation note")
    finally:
        sales.KITCHEN_LIMIT = orig
    app.dependency_overrides.clear()
    db.close()


def test_deactivate_keeps_kds_tab_and_snapshot():
    from datetime import datetime
    from app.models.oltp import Order, OrderItem, KitchenStatus, OrderStatus
    db = _session()
    owner = _owner(db)
    grill, ch, steak, water = _kitchen_fixture(db)
    o = Order(code="O1", channel_id=ch.id, status=OrderStatus.PREPARING, guest_count=1,
              opened_at=datetime.now(), kitchen_status=KitchenStatus.PREPARING,
              sent_to_kitchen_at=datetime.now())
    db.add(o); db.flush()
    line = OrderItem(order_id=o.id, menu_item_id=steak.id, quantity=1, unit_price_cents=2500,
                     kitchen_status=KitchenStatus.PREPARING, station_id=grill.id)
    db.add(line); db.commit()
    c = _client(db, owner)
    c.post(f"/admin/stations/{grill.id}/active", data={"active": 0}, follow_redirects=False)
    db.refresh(line)
    check(line.station_id == grill.id, "a fired line keeps its station snapshot after deactivation")
    body = c.get("/kitchen").text
    check("Grill" in body, "the deactivated station still shows on the KDS while it has live work")
    app.dependency_overrides.clear()
    db.close()


def test_status_next_redirect_allowlist():
    from datetime import datetime
    from app.models.oltp import Order, OrderItem, KitchenStatus, OrderStatus
    db = _session()
    owner = _owner(db)
    grill, ch, steak, water = _kitchen_fixture(db)
    o = Order(code="O", channel_id=ch.id, status=OrderStatus.PREPARING, guest_count=1,
              opened_at=datetime.now(), kitchen_status=KitchenStatus.PREPARING,
              sent_to_kitchen_at=datetime.now())
    db.add(o); db.flush()
    db.add(OrderItem(order_id=o.id, menu_item_id=steak.id, quantity=1, unit_price_cents=2500,
                     kitchen_status=KitchenStatus.PREPARING, station_id=grill.id))
    db.commit()
    c = _client(db, owner)
    r = c.post(f"/kitchen/{o.id}/status", data={"status": "ready", "next": "//evil.com"},
               follow_redirects=False)
    check(r.headers["location"] == "/kitchen", "an external next falls back to /kitchen")
    r2 = c.post(f"/kitchen/{o.id}/status", data={"status": "preparing", "next": "/expo?view=all&mine=0"},
                follow_redirects=False)
    check(r2.headers["location"] == "/expo?view=all&mine=0", "an /expo next is honoured")
    for bad in ("/orders/1", "/expo-whatever", "/kitchenFake", "\\evil"):
        rb = c.post(f"/kitchen/{o.id}/status", data={"status": "ready", "next": bad},
                    follow_redirects=False)
        check(rb.headers["location"] == "/kitchen", f"non-allowlisted next '{bad}' falls back to /kitchen")
    app.dependency_overrides.clear()
    db.close()


def test_safe_next_board_helper():
    from app.routers.sales import _safe_next_board
    for ok in ("/kitchen", "/kitchen?x=1", "/kitchen?station=unassigned", "/expo", "/expo?view=mine"):
        check(_safe_next_board(ok) == ok, f"allows {ok}")
    for bad in ("https://evil.com", "//evil.com", "/expo-whatever", "/kitchenFake",
                "\\evil", "/\\evil", "", "/orders/1", "/kitchen/../x"):
        check(_safe_next_board(bad) == "/kitchen", f"rejects '{bad}' -> /kitchen")


def test_import_station_name_normalization():
    db = _session()
    owner = _owner(db)
    c = _client(db, owner)
    grill = Station(name="Grill", type="production", is_active=True)
    accent = Station(name="Grillé", type="production", is_active=True)   # Unicode
    cat = MenuCategory(name="Mains")
    db.add_all([grill, accent, cat]); db.flush()
    a = MenuItem(category_id=cat.id, name="A", price_cents=100)
    b = MenuItem(category_id=cat.id, name="B", price_cents=100)
    d = MenuItem(category_id=cat.id, name="C", price_cents=100)
    e = MenuItem(category_id=cat.id, name="D", price_cents=100)
    db.add_all([a, b, d, e]); db.commit()
    csv_text = (
        "item_id,station\n"
        f"{a.id}, grill \n"     # surrounding spaces
        f"{b.id},GRILL\n"       # caps
        f"{d.id},Grill\n"       # exact
        f"{e.id},GRILLÉ\n"      # Unicode, casefold
    )
    c.post("/admin/stations/routing/import",
           files={"file": ("r.csv", csv_text.encode("utf-8"), "text/csv")})
    for it in (a, b, d, e):
        db.refresh(it)
    check(a.station_id == grill.id and b.station_id == grill.id and d.station_id == grill.id,
          "spaces/caps/exact all resolve to the same station (same normalisation as the dup check)")
    check(e.station_id == accent.id, "a Unicode name matches via casefold (GRILLÉ → Grillé)")
    app.dependency_overrides.clear()
    db.close()


if __name__ == "__main__":
    for fn in (test_create_and_page_renders, test_coordination_clears_production_fields,
               test_strict_item_id_parse, test_routing_rejects_coordination_station,
               test_duplicate_station_name_rejected, test_import_rejects_oversized,
               test_kds_filter_before_limit, test_deactivate_keeps_kds_tab_and_snapshot,
               test_status_next_redirect_allowlist, test_safe_next_board_helper,
               test_import_station_name_normalization,
               test_validation, test_move_reorders,
               test_deactivate_reassigns_items_to_unassigned,
               test_menu_item_routing,
               test_fire_snapshots_station,
               test_kds_station_filter_and_unassigned,
               test_expo_aggregation,
               test_floor_station_strip,
               test_routing_template_export,
               test_routing_import_applies_and_reports,
               test_routing_import_xlsx,
               test_routing_import_tolerant_header,
               test_routing_import_headerless):
        print(f"- {fn.__name__}")
        fn()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall station tests passed")
