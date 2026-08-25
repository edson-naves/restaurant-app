"""Kitchen Stations Stage B1 — PreparationTask model + creation/snapshot + rollup
+ backfill + idempotency. Tasks are introduced UNDER the current system; the KDS/
Expo/floor are NOT changed here (that is B2/B3). Throwaway SQLite + overrides.
Run: python tests/test_prep_tasks.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.deps import current_staff, get_db
from app.main import app
from app.models import oltp  # noqa: F401
from app.models.oltp import (
    Channel, KitchenStatus, MenuCategory, MenuItem, Modifier, ModifierGroup,
    ModifierOption, Order, OrderItem, OrderItemModifier, OrderItemOption,
    OrderStatus, PreparationTask, PreparationTaskStatus, Staff, Station,
)

_fail = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _fail.append(label)


def _session():
    path = os.path.join(tempfile.gettempdir(), f"prep_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _two_sessions():
    """Two independent sessions on the SAME SQLite file, for overlapping-txn
    tests. Returns (dbA, dbB, close)."""
    path = os.path.join(tempfile.gettempdir(), f"prep2_{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine, future=True)
    a, b = Sess(), Sess()
    return a, b, (lambda: (a.close(), b.close()))


def _client(db, staff):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


def _owner(db):
    o = Staff(name="Owner", role="owner", pin_code="x", is_active=True)
    db.add(o); db.commit()
    return o


def _base(db, item_station=None):
    """A dine-in channel + a menu item routed to `item_station` (or Unassigned)."""
    ch = Channel(code="dine_in", name="Dine-in", channel_type="dine_in")
    cat = MenuCategory(name="Mains")
    db.add_all([ch, cat]); db.flush()
    item = MenuItem(category_id=cat.id, name="Burger", price_cents=1500,
                    station_id=item_station)
    db.add(item); db.commit()
    return ch, cat, item


def _order_with_item(db, ch, menu_item, course=2):
    o = Order(code=f"O-{uuid.uuid4().hex[:6]}", channel_id=ch.id, status=OrderStatus.OPEN,
              guest_count=2, opened_at=datetime.now())
    db.add(o); db.flush()
    oi = OrderItem(order_id=o.id, menu_item_id=menu_item.id, quantity=1,
                   unit_price_cents=menu_item.price_cents, kitchen_status=KitchenStatus.PENDING,
                   course=course)
    db.add(oi); db.commit()
    return o, oi


def _fire(c, order_id, course=0):
    return c.post(f"/orders/{order_id}/send", data={"course": course}, follow_redirects=False)


# --------------------------------------------------------------------------

def test_single_station_task():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, oi = _order_with_item(db, ch, mi)
    c = _client(db, owner)
    _fire(c, o.id)
    db.refresh(oi)
    tasks = oi.tasks
    check(len(tasks) == 1, "a single-station item fires exactly one task")
    check(tasks[0].station_id == grill.id and tasks[0].is_base, "the base task snapshots the item's station")
    check(tasks[0].kitchen_status == PreparationTaskStatus.PREPARING, "task starts preparing at fire")
    check(oi.kitchen_status == KitchenStatus.PREPARING, "line rolls up to preparing")
    app.dependency_overrides.clear(); db.close()


def test_multi_station_snapshot():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True)
    fryer = Station(name="Fryer", type="production", is_active=True)
    cold = Station(name="Cold", type="production", is_active=True)
    db.add_all([grill, fryer, cold]); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    # A modifier routed to Fryer, a NULL modifier (folds into base), and a grouped
    # option routed to Cold.
    fries = Modifier(name="Fries", station_id=fryer.id)
    cheese = Modifier(name="Extra cheese", station_id=None)
    grp = ModifierGroup(menu_item_id=mi.id, name="Sauce")
    db.add_all([fries, cheese, grp]); db.flush()
    sauce = ModifierOption(group_id=grp.id, name="Special sauce", station_id=cold.id)
    db.add(sauce); db.flush()
    o, oi = _order_with_item(db, ch, mi)
    db.add_all([
        OrderItemModifier(order_item_id=oi.id, modifier_id=fries.id, price_delta_cents=0),
        OrderItemModifier(order_item_id=oi.id, modifier_id=cheese.id, price_delta_cents=0),
        OrderItemOption(order_item_id=oi.id, option_id=sauce.id, group_name="Sauce",
                        label="Special sauce", price_delta_cents=0),
    ])
    db.commit()
    c = _client(db, owner)
    _fire(c, o.id)
    db.refresh(oi)
    by_station = {t.station_id: t for t in oi.tasks}
    check(len(oi.tasks) == 3, "one sale line fans out to 3 tasks (Grill + Fryer + Cold)")
    check(by_station.get(grill.id) is not None and by_station[grill.id].is_base, "base task at Grill")
    check(by_station.get(fryer.id) is not None and not by_station[fryer.id].is_base, "Fryer task (modifier)")
    check(by_station.get(cold.id) is not None, "Cold task (option)")
    # Extra cheese (NULL) folds into the base Grill task's details.
    grill_labels = {d.label for d in by_station[grill.id].details}
    check("Extra cheese" in grill_labels, "a NULL-station modifier folds into the base task")
    check({d.label for d in by_station[fryer.id].details} == {"Fries"}, "Fryer task carries the Fries label")
    check({d.label for d in by_station[cold.id].details} == {"Special sauce"}, "Cold task carries the sauce label")
    check(all(t.item_label == "Burger" for t in oi.tasks), "every task snapshots the item name")
    app.dependency_overrides.clear(); db.close()


def test_unassigned_task():
    db = _session(); owner = _owner(db)
    ch, cat, mi = _base(db, item_station=None)   # item Unassigned
    o, oi = _order_with_item(db, ch, mi)
    c = _client(db, owner)
    _fire(c, o.id)
    db.refresh(oi)
    check(len(oi.tasks) == 1 and oi.tasks[0].station_id is None,
          "an Unassigned item fires one base task with NULL station (explicit, not hidden)")
    app.dependency_overrides.clear(); db.close()


def test_reroute_after_fire_does_not_move_tasks():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True)
    fryer = Station(name="Fryer", type="production", is_active=True)
    db.add_all([grill, fryer]); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, oi = _order_with_item(db, ch, mi)
    c = _client(db, owner)
    _fire(c, o.id)
    db.refresh(oi)
    task_station = oi.tasks[0].station_id
    # Re-route the menu item afterwards.
    mi.station_id = fryer.id
    db.commit()
    db.refresh(oi)
    check(oi.tasks[0].station_id == task_station == grill.id,
          "editing MenuItem routing after fire never moves the already-created task")
    app.dependency_overrides.clear(); db.close()


def test_rollup_all_ready_makes_item_ready():
    from app.routers.sales import rollup_item_kitchen_status
    db = _session()
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, oi = _order_with_item(db, ch, mi)
    oi.kitchen_status = KitchenStatus.PREPARING
    t1 = PreparationTask(order_item_id=oi.id, order_id=o.id, station_id=grill.id, course=2,
                         kitchen_status=PreparationTaskStatus.READY, quantity=1, item_label="Burger")
    t2 = PreparationTask(order_item_id=oi.id, order_id=o.id, station_id=None, course=2,
                         kitchen_status=PreparationTaskStatus.PREPARING, quantity=1, item_label="Burger")
    db.add_all([t1, t2]); db.commit(); db.refresh(oi)
    rollup_item_kitchen_status(oi)
    check(oi.kitchen_status == KitchenStatus.PREPARING, "one task still preparing → line stays preparing")
    t2.kitchen_status = PreparationTaskStatus.READY; db.commit(); db.refresh(oi)
    rollup_item_kitchen_status(oi)
    check(oi.kitchen_status == KitchenStatus.READY, "all tasks ready → line becomes ready")
    db.close()


def test_rollup_never_sets_served():
    from app.routers.sales import rollup_item_kitchen_status
    db = _session()
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, oi = _order_with_item(db, ch, mi)
    oi.kitchen_status = KitchenStatus.READY
    t = PreparationTask(order_item_id=oi.id, order_id=o.id, station_id=grill.id, course=2,
                        kitchen_status=PreparationTaskStatus.SERVED, quantity=1, item_label="Burger")
    db.add(t); db.commit(); db.refresh(oi)
    rollup_item_kitchen_status(oi)
    check(oi.kitchen_status != KitchenStatus.SERVED,
          "the rollup never sets the line served (served is owned by the serving flow)")
    db.close()


def test_legacy_item_without_tasks_safe():
    from app.routers.sales import rollup_item_kitchen_status
    db = _session()
    ch, cat, mi = _base(db, item_station=None)
    o, oi = _order_with_item(db, ch, mi)
    oi.kitchen_status = KitchenStatus.PREPARING; db.commit(); db.refresh(oi)
    before = oi.kitchen_status
    rollup_item_kitchen_status(oi)   # no tasks → must be a safe no-op
    check(oi.kitchen_status == before, "a line with no tasks is left unchanged (legacy safe)")
    db.close()


def test_held_course_creates_no_task():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, starter = _order_with_item(db, ch, mi, course=1)
    main = OrderItem(order_id=o.id, menu_item_id=mi.id, quantity=1, unit_price_cents=1500,
                     kitchen_status=KitchenStatus.PENDING, course=2)
    db.add(main); db.commit()
    c = _client(db, owner)
    _fire(c, o.id, course=1)                 # fire only the starter
    db.refresh(starter); db.refresh(main)
    check(len(starter.tasks) == 1, "the fired course creates its task")
    check(len(main.tasks) == 0, "a held later course creates NO task until fired")
    check(main.kitchen_status == KitchenStatus.PENDING, "the held course stays pending (not blocked/advanced)")
    app.dependency_overrides.clear(); db.close()


def test_duplicate_fire_is_idempotent():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, oi = _order_with_item(db, ch, mi)
    c = _client(db, owner)
    _fire(c, o.id)
    db.refresh(oi)
    n = len(oi.tasks)
    r2 = _fire(c, o.id)                       # repeat: nothing pending
    db.refresh(oi)
    check(r2.status_code == 400, "a repeated fire has nothing to fire (400)")
    check(len(oi.tasks) == n == 1, "a duplicate fire creates no extra tasks")
    # And the internal guard: calling the creator again is a no-op.
    from app.routers.sales import _create_tasks_for_item
    _create_tasks_for_item(db, oi, datetime.now())
    db.refresh(oi)
    check(len(oi.tasks) == 1, "the creation helper is idempotent on an already-fired line")
    app.dependency_overrides.clear(); db.close()


def test_backfill_idempotent():
    from app import migrate
    db = _session()
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o = Order(code="O-BF", channel_id=ch.id, status=OrderStatus.PREPARING, guest_count=1,
              opened_at=datetime.now(), kitchen_status=KitchenStatus.PREPARING,
              sent_to_kitchen_at=datetime.now())
    db.add(o); db.flush()
    legacy = OrderItem(order_id=o.id, menu_item_id=mi.id, quantity=1, unit_price_cents=1500,
                       kitchen_status=KitchenStatus.PREPARING, course=2, station_id=grill.id)
    db.add(legacy); db.commit()
    # Fresh connection each call — committing ends the prior transaction.
    migrate._backfill_prep_tasks(db.connection()); db.commit()
    migrate._backfill_prep_tasks(db.connection()); db.commit()   # run twice
    db.refresh(legacy)
    check(len(legacy.tasks) == 1, "backfill creates one base task per legacy fired item, and only once")
    check(legacy.tasks[0].station_id == grill.id and legacy.tasks[0].is_base,
          "backfilled task uses the legacy station_id snapshot")
    db.close()


def test_payment_gate_unchanged():
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, oi = _order_with_item(db, ch, mi)
    c = _client(db, owner)
    _fire(c, o.id)
    db.refresh(o)
    check(o.kitchen_status == KitchenStatus.PREPARING and o.status == OrderStatus.PREPARING,
          "after fire the order is preparing (not served) — the payment gate is unaffected")
    # Existing item-level mark-ready still drives the order to ready (unchanged path).
    c.post(f"/kitchen/{o.id}/status", data={"status": "ready"}, follow_redirects=False)
    db.refresh(o)
    check(o.kitchen_status == KitchenStatus.READY, "existing item-level ready still rolls the order to ready")
    db.close()


def _task(oi, o, station_id, fire_seq=1, status=None):
    return PreparationTask(
        order_item_id=oi.id, order_id=o.id, station_id=station_id, course=2,
        kitchen_status=status or PreparationTaskStatus.PREPARING, quantity=1,
        item_label="Burger", fire_seq=fire_seq,
    )


def test_db_rejects_duplicate_within_batch():
    from sqlalchemy.exc import IntegrityError
    db = _session()
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, oi = _order_with_item(db, ch, mi)

    db.add(_task(oi, o, grill.id, fire_seq=1)); db.commit()
    db.add(_task(oi, o, grill.id, fire_seq=1))          # same (line, batch, station)
    raised = False
    try:
        db.commit()
    except IntegrityError:
        db.rollback(); raised = True
    check(raised, "DB rejects a duplicate task for the same (line, fire_seq, station)")

    db.add(_task(oi, o, grill.id, fire_seq=2)); db.commit()   # a later batch is allowed
    check(True, "a later fire batch (fire_seq=2) may reuse the same station")

    # NULL/Unassigned base task: first ok, duplicate in the same batch rejected.
    db.add(_task(oi, o, None, fire_seq=1)); db.commit()
    db.add(_task(oi, o, None, fire_seq=1))
    raised2 = False
    try:
        db.commit()
    except IntegrityError:
        db.rollback(); raised2 = True
    check(raised2, "DB rejects a duplicate Unassigned (NULL) task in the same batch (COALESCE)")
    db.close()


def test_concurrent_fire_creates_one_batch():
    from sqlalchemy import func, select
    from sqlalchemy.exc import IntegrityError
    from app.routers.sales import _create_tasks_for_item
    dbA, dbB, close = _two_sessions()
    grill = Station(name="Grill", type="production", is_active=True)
    fryer = Station(name="Fryer", type="production", is_active=True)
    dbA.add_all([grill, fryer]); dbA.flush()
    ch, cat, mi = _base(dbA, item_station=grill.id)
    o, oi = _order_with_item(dbA, ch, mi)
    fries = Modifier(name="Fries", station_id=fryer.id); dbA.add(fries); dbA.flush()
    dbA.add(OrderItemModifier(order_item_id=oi.id, modifier_id=fries.id, price_delta_cents=0))
    dbA.commit()

    # Two overlapping fires: both sessions load the item and build the batch
    # BEFORE either commits, so both see "no tasks" (the app guard passes for both).
    itemA = dbA.get(OrderItem, oi.id)
    itemB = dbB.get(OrderItem, oi.id)
    _create_tasks_for_item(dbA, itemA, datetime.now())
    _create_tasks_for_item(dbB, itemB, datetime.now())
    dbA.commit()                                        # A wins → 2 tasks (Grill+Fryer)
    raised = False
    try:
        dbB.commit()                                    # B loses on the unique index
    except IntegrityError:
        dbB.rollback(); raised = True
    check(raised, "the second overlapping fire is rejected by the DB unique index")
    n = dbA.execute(
        select(func.count()).select_from(PreparationTask).where(PreparationTask.order_item_id == oi.id)
    ).scalar_one()
    check(n == 2, "exactly one batch exists — 2 tasks, not 4 (no duplicates)")
    from app.routers.sales import rollup_item_kitchen_status
    itemA = dbA.get(OrderItem, oi.id)
    rollup_item_kitchen_status(itemA)
    check(itemA.kitchen_status == KitchenStatus.PREPARING,
          "the surviving batch rolls the line up to a correct status")
    close()


def test_backfill_competing_sessions_no_duplicate():
    """Competing-session + repeated-execution safety for the backfill.

    NOTE: SQLite serializes writers, so this is not a truly simultaneous overlap;
    it proves that two competing sessions and repeated runs never duplicate. The
    DURABLE guarantee is the unique index + conflict-safe insert (INSERT OR IGNORE
    / ON CONFLICT DO NOTHING), proven by test_db_rejects_duplicate_within_batch
    and test_concurrent_fire_creates_one_batch (a real overlap on the index). A
    true simultaneous-overlap backfill belongs in a Postgres integration env.
    """
    from app import migrate
    dbA, dbB, close = _two_sessions()
    grill = Station(name="Grill", type="production", is_active=True); dbA.add(grill); dbA.flush()
    ch, cat, mi = _base(dbA, item_station=grill.id)
    o = Order(code="O-CB", channel_id=ch.id, status=OrderStatus.PREPARING, guest_count=1,
              opened_at=datetime.now(), kitchen_status=KitchenStatus.PREPARING,
              sent_to_kitchen_at=datetime.now())
    dbA.add(o); dbA.flush()
    legacy = OrderItem(order_id=o.id, menu_item_id=mi.id, quantity=1, unit_price_cents=1500,
                       kitchen_status=KitchenStatus.PREPARING, course=2, station_id=None)  # Unassigned
    dbA.add(legacy); dbA.commit()
    migrate._ensure_prep_task_index(dbA.connection())
    migrate._backfill_prep_tasks(dbA.connection()); dbA.commit()   # session A
    migrate._backfill_prep_tasks(dbB.connection()); dbB.commit()   # competing session B
    migrate._backfill_prep_tasks(dbA.connection()); dbA.commit()   # repeated run
    dbA.refresh(legacy)
    check(len(legacy.tasks) == 1, "competing/repeated backfill creates exactly one task")
    check(legacy.tasks[0].station_id is None, "legacy Unassigned backfill stays NULL")
    close()


def test_index_invariant_present_normal():
    from app import migrate
    db = _session()
    # create_all already built the index; the invariant is a clean no-op + verify.
    migrate._ensure_prep_task_index(db.connection())
    check(migrate._index_exists(db.connection(), "uq_prep_task_batch"),
          "the fire-batch unique index is present after the invariant runs")
    check(migrate._prep_task_index_ok(db.connection()),
          "the index is verified BY DEFINITION (unique + order_item_id/fire_seq/COALESCE(station_id))")
    db.close()


def test_index_wrong_definition_halts():
    from sqlalchemy import text
    from app import migrate
    db = _session()
    # Replace the correct index with a same-NAME but WRONG one (non-unique, wrong
    # columns) — exactly the case a name-only check would miss.
    db.execute(text("DROP INDEX uq_prep_task_batch"))
    db.execute(text("CREATE INDEX uq_prep_task_batch ON preparation_task(order_item_id)"))
    db.commit()
    check(migrate._index_exists(db.connection(), "uq_prep_task_batch"),
          "the wrong index exists by NAME")
    check(migrate._prep_task_index_ok(db.connection()) is False,
          "but it is NOT ok by definition (non-unique / wrong columns)")
    halted = False
    try:
        migrate._ensure_prep_task_index(db.connection())
    except RuntimeError as e:
        halted = "does not enforce" in str(e).lower()
    check(halted, "the invariant HALTS on a same-name / wrong-definition index")
    still = db.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_prep_task_batch'"
    )).first()
    check(still is not None, "the wrong index is not silently dropped")
    db.close()


def test_index_definition_strict_rejects_bad_shapes():
    from sqlalchemy import text
    from app import migrate
    bad = {
        "extra key column":
            "CREATE UNIQUE INDEX uq_prep_task_batch ON preparation_task"
            "(order_item_id, fire_seq, COALESCE(station_id,-1), course)",
        "partial index (drops Unassigned protection)":
            "CREATE UNIQUE INDEX uq_prep_task_batch ON preparation_task"
            "(order_item_id, fire_seq, COALESCE(station_id,-1)) WHERE station_id IS NOT NULL",
        "wrong COALESCE expression":
            "CREATE UNIQUE INDEX uq_prep_task_batch ON preparation_task"
            "(order_item_id, fire_seq, COALESCE(station_id,0))",
    }
    for label, ddl in bad.items():
        db = _session()
        db.execute(text("DROP INDEX uq_prep_task_batch"))
        db.execute(text(ddl)); db.commit()
        check(migrate._prep_task_index_ok(db.connection()) is False,
              f"rejected by definition: {label}")
        halted = False
        try:
            migrate._ensure_prep_task_index(db.connection())
        except RuntimeError as e:
            halted = "does not enforce" in str(e).lower()
        check(halted, f"invariant HALTS on: {label}")
        still = db.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_prep_task_batch'"
        )).first()
        check(still is not None, f"wrong index not silently dropped: {label}")
        db.close()


def test_index_exists_is_table_scoped():
    """_index_exists must be scoped: a same-name index on ANOTHER table must not
    count as the preparation_task index (mirrors the schema scoping the Postgres
    branch adds — an identically-named index in another schema/table is not a
    collision). SQLite has no schemas, so this exercises the table-scoped path;
    the Postgres query filters schemaname=current_schema() AND tablename likewise.
    """
    from sqlalchemy import text
    from app import migrate
    db = _session()
    # A probe index on ANOTHER table, committed first (commit invalidates a held
    # connection, so re-fetch db.connection() afterwards).
    db.execute(text("CREATE INDEX ix_probe_other ON station(name)")); db.commit()
    conn = db.connection()
    # The real index, on preparation_task.
    check(migrate._index_exists(conn, "uq_prep_task_batch", "preparation_task"),
          "same name + preparation_task → exists (scoped)")
    check(migrate._index_exists(conn, "uq_prep_task_batch") is True,
          "same name, table unspecified → exists")
    # The other-table index is found when scoped to its own table, but must NOT
    # count when scoped to preparation_task.
    check(migrate._index_exists(conn, "ix_probe_other", "station"),
          "an index on another table is found when scoped to that table")
    check(migrate._index_exists(conn, "ix_probe_other", "preparation_task") is False,
          "same name + WRONG table → must NOT count")
    db.close()


def test_pg_index_key_normalization_strict():
    """Postgres exact-key discrimination (no live Postgres in dev).

    The Postgres branch of _prep_task_index_ok reconstructs each key with
    pg_get_indexdef(idx, n, true) and normalises it with _pg_norm_key, then
    requires key1==order_item_id, key2==fire_seq, key3==COALESCE(station_id,-1).
    This feeds the normaliser the EXACT strings pg_get_indexdef emits for each
    definition and asserts the accept/reject decision the branch makes — proving
    an arithmetic wrap, a wrapping function, an extra/wrong fallback, or extra
    quoting/casting are handled correctly. (Catalog gating — indisunique,
    indpred IS NULL, indnkeyatts==3, indnatts==3 — is exercised against a real
    server in a Postgres integration environment; see the v9 handoff.)
    """
    from app import migrate
    accepted_k3 = ("coalesce(station_id,-1)", "coalesce(station_id,(-1))")

    # key1 / key2 are plain columns.
    check(migrate._pg_norm_key("order_item_id") == "order_item_id", "pg key1 == order_item_id")
    check(migrate._pg_norm_key("fire_seq") == "fire_seq", "pg key2 == fire_seq")

    # Correct third key — every rendering Postgres may use for the -1 literal.
    for good in (
        "COALESCE(station_id, '-1'::integer)",   # typical pg_get_indexdef output
        "COALESCE(station_id, -1)",
        "(COALESCE(station_id, -1))",            # a wrapping paren pair is stripped
        "COALESCE(station_id, (-1))",
    ):
        check(migrate._pg_norm_key(good) in accepted_k3, f"pg key3 ACCEPTED: {good}")

    # Tampered third keys that keep the COALESCE tokens but change the value.
    for bad in (
        "(COALESCE(station_id, '-1'::integer) + course)",   # arithmetic outside COALESCE
        "abs(COALESCE(station_id, '-1'::integer))",         # wrapping function
        "COALESCE(station_id, 0)",                          # wrong fallback
        "COALESCE(station_id, '-1'::integer, course)",      # extra fallback arg
        "(COALESCE(station_id, -1) + 0)",                   # + 0 no-op arithmetic
    ):
        check(migrate._pg_norm_key(bad) not in accepted_k3, f"pg key3 REJECTED: {bad}")

    # A paren that does not wrap the whole expression is NOT stripped away.
    check(migrate._strip_wrapping_parens("(a+b)*c") == "(a+b)*c",
          "non-wrapping parens are preserved (no false accept)")
    check(migrate._strip_wrapping_parens("((x))") == "x",
          "fully-wrapping parens are stripped")


def test_backfill_missing_table_noop():
    from sqlalchemy import create_engine
    from app import migrate
    eng = create_engine("sqlite://")             # empty in-memory, no tables
    with eng.begin() as conn:
        check(migrate._backfill_prep_tasks(conn) == [],
              "a legitimately absent table is a safe no-op ([])")


def test_backfill_unexpected_error_propagates():
    from app import migrate

    class _Dialect:
        name = "sqlite"

    class _Conn:
        dialect = _Dialect()

        def execute(self, *a, **k):
            raise RuntimeError("boom-db")

    orig = migrate._table_exists
    migrate._table_exists = lambda conn, name: True   # pretend the table is present
    raised = False
    try:
        migrate._backfill_prep_tasks(_Conn())
    except RuntimeError as e:
        raised = "boom-db" in str(e)
    finally:
        migrate._table_exists = orig
    check(raised, "an unexpected backfill DB error PROPAGATES (not swallowed as [])")


def test_index_invariant_halts_on_duplicates():
    from sqlalchemy import func, select, text
    from app import migrate
    db = _session()
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, oi = _order_with_item(db, ch, mi)
    # Drop the guard so a duplicate identity can be seeded (simulating a DB from a
    # buggy earlier version), then assert the invariant HALTS rather than ignoring it.
    db.execute(text("DROP INDEX uq_prep_task_batch")); db.commit()
    db.add_all([_task(oi, o, grill.id, fire_seq=1), _task(oi, o, grill.id, fire_seq=1)])
    db.commit()
    halted = False
    try:
        migrate._ensure_prep_task_index(db.connection())
    except RuntimeError as e:
        halted = "duplicate" in str(e).lower()
    check(halted, "the required index invariant HALTS (raises) on pre-existing duplicate identities")
    n = db.execute(select(func.count()).select_from(PreparationTask)).scalar_one()
    check(n == 2, "duplicates are reported, never silently deleted or merged")
    db.close()


def test_fire_conflict_discrimination():
    from app.routers.sales import _fire_conflict_is_idempotent
    db = _session()
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, oi = _order_with_item(db, ch, mi)
    check(_fire_conflict_is_idempotent(db, [oi.id]) is False,
          "pending line with no tasks → NOT idempotent (surface the error)")
    db.add(_task(oi, o, grill.id, fire_seq=1)); db.commit()
    check(_fire_conflict_is_idempotent(db, [oi.id]) is False,
          "line still pending (even with a task) → NOT idempotent")
    oi.kitchen_status = KitchenStatus.PREPARING; db.commit()
    check(_fire_conflict_is_idempotent(db, [oi.id]) is True,
          "non-pending line with tasks → the expected concurrent winner (idempotent)")
    db.close()


def test_unrelated_integrity_error_surfaced():
    from sqlalchemy import func, select
    from sqlalchemy.exc import IntegrityError
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, oi = _order_with_item(db, ch, mi)
    c = _client(db, owner)
    real_commit = db.commit

    def boom():                                   # an UNRELATED integrity failure
        raise IntegrityError("stmt", {}, Exception("unrelated constraint"))

    db.commit = boom
    raised = False
    try:
        _fire(c, o.id)                            # the route must NOT hide this
    except Exception:
        raised = True
    finally:
        db.commit = real_commit
    check(raised, "an unrelated IntegrityError during fire is surfaced, not swallowed as success")
    db.rollback()
    n = db.execute(select(func.count()).select_from(PreparationTask)
                   .where(PreparationTask.order_item_id == oi.id)).scalar_one()
    check(n == 0, "no partial tasks remain after the surfaced error")
    app.dependency_overrides.clear(); db.close()


def test_readiness_invariant_for_b2():
    from app.routers.sales import fired_items_without_tasks
    from app import migrate
    db = _session(); owner = _owner(db)
    grill = Station(name="Grill", type="production", is_active=True); db.add(grill); db.flush()
    ch, cat, mi = _base(db, item_station=grill.id)
    o, oi = _order_with_item(db, ch, mi)
    c = _client(db, owner)
    _fire(c, o.id)
    check(fired_items_without_tasks(db) == 0, "after a fire, no fired item lacks tasks")
    # A legacy fired item with no tasks must be counted (the B2 gate).
    o2 = Order(code="O-LEG", channel_id=ch.id, status=OrderStatus.PREPARING, guest_count=1,
               opened_at=datetime.now(), kitchen_status=KitchenStatus.PREPARING,
               sent_to_kitchen_at=datetime.now())
    db.add(o2); db.flush()
    db.add(OrderItem(order_id=o2.id, menu_item_id=mi.id, quantity=1, unit_price_cents=1500,
                     kitchen_status=KitchenStatus.PREPARING, course=2, station_id=grill.id))
    db.commit()
    check(fired_items_without_tasks(db) == 1, "a legacy fired item without tasks is counted (B2 must not switch)")
    migrate._backfill_prep_tasks(db.connection()); db.commit()
    check(fired_items_without_tasks(db) == 0, "backfill clears the invariant to 0 (B2 may proceed)")
    app.dependency_overrides.clear(); db.close()


if __name__ == "__main__":
    for fn in (test_single_station_task, test_multi_station_snapshot, test_unassigned_task,
               test_reroute_after_fire_does_not_move_tasks, test_rollup_all_ready_makes_item_ready,
               test_rollup_never_sets_served, test_legacy_item_without_tasks_safe,
               test_held_course_creates_no_task, test_duplicate_fire_is_idempotent,
               test_backfill_idempotent, test_payment_gate_unchanged,
               test_db_rejects_duplicate_within_batch, test_concurrent_fire_creates_one_batch,
               test_backfill_competing_sessions_no_duplicate, test_readiness_invariant_for_b2,
               test_index_invariant_present_normal, test_index_invariant_halts_on_duplicates,
               test_index_wrong_definition_halts, test_index_definition_strict_rejects_bad_shapes,
               test_index_exists_is_table_scoped, test_pg_index_key_normalization_strict,
               test_backfill_missing_table_noop, test_backfill_unexpected_error_propagates,
               test_fire_conflict_discrimination, test_unrelated_integrity_error_surfaced):
        print(f"- {fn.__name__}")
        fn()
    if _fail:
        print(f"\n{len(_fail)} FAILED")
        sys.exit(1)
    print("\nall prep-task (B1) tests passed")
