"""Normalized transactional (OLTP) schema.

This is the operational side of the system: what waiters, kitchen staff and
the delivery coordinator touch during service. It is the source of truth and
the source system for the dimensional model in star.py.
"""
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# --------------------------------------------------------------------------
# Reference / lookup data
# --------------------------------------------------------------------------

class Role:
    """Section 3 — five roles with distinct permissions."""
    OWNER = "owner"
    MANAGER = "manager"
    WAITER = "waiter"
    KITCHEN = "kitchen"
    DELIVERY_COORDINATOR = "delivery_coordinator"
    ALL = (OWNER, MANAGER, WAITER, KITCHEN, DELIVERY_COORDINATOR)


class TableStatus:
    """Section 4.1.1 — table states."""
    FREE = "free"
    OCCUPIED = "occupied"
    READY_TO_PAY = "ready_to_pay"


class OrderStatus:
    OPEN = "open"
    PREPARING = "preparing"
    READY = "ready"            # food is up in the pass — "ready to serve"
    SERVED = "served"          # waiter has delivered it to the table
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    CLOSED = "closed"
    CANCELLED = "cancelled"


# Guest-facing labels for the order lifecycle. "ready" reads as "Ready to serve"
# (kitchen done, not yet delivered); the rest are the plain words.
ORDER_STATUS_LABELS = {
    OrderStatus.OPEN: "Open",
    OrderStatus.PREPARING: "Preparing",
    OrderStatus.READY: "Ready to serve",
    OrderStatus.SERVED: "Served",
    OrderStatus.PARTIALLY_PAID: "Partially paid",
    OrderStatus.PAID: "Paid",
    OrderStatus.CLOSED: "Closed",
    OrderStatus.CANCELLED: "Cancelled",
}


def order_status_label(status: str) -> str:
    return ORDER_STATUS_LABELS.get(status, status.replace("_", " ").title())


class KitchenStatus:
    """Section 4.1.3 — Pending -> Preparing -> Ready -> Served."""
    PENDING = "pending"
    PREPARING = "preparing"
    READY = "ready"
    SERVED = "served"      # per item: delivered to the table, off the kitchen line


# Section 4.1.3 — coursing. Meal stages, fired in this order.
COURSE_LABELS = {1: "Starters", 2: "Mains", 3: "Dessert"}


def course_label(n: int) -> str:
    return COURSE_LABELS.get(n, f"Course {n}")


# Day-menu (prix fixe) course slots. The manager composes a day menu from ANY
# subset of these — a menu can be Starter+Main, Starter+Drink, or all five — and
# each slot they include offers one or more items the guest chooses between.
DAY_MENU_COURSES = {1: "Starter", 2: "Main", 3: "Dessert", 4: "Drink", 5: "Side"}


def day_menu_course_label(n: int) -> str:
    return DAY_MENU_COURSES.get(n, f"Course {n}")


# 4.1.2 — common allergens a waiter flags on a line. "Other" pairs with a free
# text box for anything not listed.
ALLERGEN_OPTIONS = ("Lactose", "Gluten", "Seafood", "Nuts")


def build_allergens(selected: list[str], other: str = "") -> str:
    """Assemble the stored allergen string from ticked options and free text."""
    parts = [a for a in selected if a in ALLERGEN_OPTIONS]
    other = (other or "").strip()
    if other:
        parts.append(f"Other: {other}")
    return ", ".join(parts)


def category_emoji(category_name: str) -> str:
    """A representative emoji for a menu category (icons + item thumbnails)."""
    n = (category_name or "").strip().lower()
    # Order matters: check specific keys before generic ones. "tea" is left out
    # of the coffee row on purpose — it is a substring of "steaks".
    table = [
        (("start", "appet"), "🥟"), (("soup",), "🍲"), (("salad",), "🥗"),
        (("burger",), "🍔"), (("sandwich", "wrap"), "🥪"), (("pasta",), "🍝"),
        (("pizza",), "🍕"), (("steak", "grill"), "🥩"), (("seafood", "fish"), "🐟"),
        (("chicken",), "🍗"), (("side",), "🍟"), (("dessert", "sweet"), "🍰"),
        (("beer",), "🍺"), (("wine",), "🍷"), (("cocktail",), "🍸"),
        (("coffee", "hot drink"), "☕"), (("drink", "soft", "beverage"), "🥤"),
        (("main", "entr"), "🍽️"), (("fav",), "⭐"),
    ]
    for keys, emoji in table:
        if any(k in n for k in keys):
            return emoji
    return "🍴"


def course_for_category(category_name: str) -> int:
    """A menu section's natural firing course.

    The menu category (Starters/Mains/Sides/Desserts/Drinks) and the kitchen
    course are separate concepts, but for most items they line up — so this is
    the sensible default the order screen pre-selects. Starters fire first,
    desserts fire last, and everything else (mains, sides, drinks) rides with
    the mains. The waiter can always override per line.
    """
    n = (category_name or "").strip().lower()
    if "start" in n or "appet" in n or "salad" in n or "soup" in n:
        return 1
    if "dessert" in n or "sweet" in n or "coffee" in n:
        return 3
    return 2


class DeliveryStatus:
    """Section 4.1.4 — Pending -> Preparing -> Ready -> On the way -> Delivered."""
    PENDING = "pending"
    PREPARING = "preparing"
    READY = "ready"
    ON_THE_WAY = "on_the_way"
    DELIVERED = "delivered"


class SeatStatus:
    OPEN = "open"
    PAID_PARTIAL = "paid_partial"   # Section 4.2.5 — guest left early, paid their portion
    PAID = "paid"


class Setting(Base):
    """Key/value system configuration set up by the owner (tax, identity).

    A flat store rather than a column per setting so a new one is a row, not a
    migration. Values are strings; the settings service parses and defaults.
    """
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    # Stores a salted PBKDF2 hash (~119 chars), not the 4–8 digit PIN, so it
    # must be wide. Postgres enforces this length; an existing narrow column is
    # widened by migrate.WIDENED_COLUMNS.
    pin_code: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # The person's usual job on the schedule (Server, Bartender…). Distinct from
    # `role`, which is their access level. Used to colour their shifts by default.
    position_id: Mapped[int | None] = mapped_column(ForeignKey("position.id"), nullable=True)
    # Hourly pay in cents (owner-only), feeds schedule labor cost. Optional photo
    # stored as a small resized data-URI so it survives redeploys without a disk.
    wage_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    photo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A short availability note shown in the schedule team panel (e.g. "Prefers
    # mornings", "Weekends only").
    availability_note: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    hired_on: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    position: Mapped["Position | None"] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner','manager','waiter','kitchen','delivery_coordinator')",
            name="ck_staff_role",
        ),
    )


class Floor(Base):
    """A storey of the restaurant. Section 4.1.1's floor plan, one per level.

    Grid coordinates are unique per floor, not globally: the second floor has
    its own square (0,0).
    """
    __tablename__ = "floor"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    zones: Mapped[list["Zone"]] = relationship(
        back_populates="floor", order_by="Zone.sort_order, Zone.name"
    )


# Distinct on the dark floor plan and distinguishable from the free/occupied/
# ready status colours, which the chip's border already uses.
# Zone colours deliberately avoid the status hues (green=free, amber=occupied,
# red=ready) and the blue accent, so a zone is never mistaken for a status.
ZONE_PALETTE = (
    "#eab308",  # Main   -> yellow
    "#3b82f6",  # Window -> blue
    "#111827",  # Patio  -> black
    "#ec4899",  # Bar    -> pink
    "#0d9488",  # teal   (further zones, e.g. Live Music)
    "#a855f7",  # purple (e.g. Meeting)
    "#f472b6",  # rose
    "#64748b",  # slate
)


def seat_color(n: int) -> str:
    """A stable accent colour for a seat number (0 = the table / shared).

    Cycles the zone palette so each seat has a consistent colour on the order
    screen; the table slot is neutral so it reads as "not one seat".
    """
    if not n:
        return "#8a9bb0"   # slate — the shared / table slot
    return ZONE_PALETTE[(n - 1) % len(ZONE_PALETTE)]


class Zone(Base):
    """A named area within one floor — Zone A, Patio, Bar.

    Zones belong to a floor rather than being a global list, so each floor
    carries its own set and count.
    """
    __tablename__ = "zone"

    id: Mapped[int] = mapped_column(primary_key=True)
    floor_id: Mapped[int] = mapped_column(ForeignKey("floor.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Null means "not chosen yet" and falls back to the palette, so zones are
    # colour-coded from the moment they exist without a backfill.
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)

    floor: Mapped["Floor"] = relationship(back_populates="zones", lazy="joined")

    __table_args__ = (
        UniqueConstraint("floor_id", "name", name="uq_zone_floor_name"),
    )

    @property
    def label(self) -> str:
        return f"{self.floor.name} · {self.name}"

    @property
    def swatch(self) -> str:
        """The colour to draw for this zone, chosen or defaulted."""
        if self.color:
            return self.color
        return ZONE_PALETTE[(self.sort_order or 0) % len(ZONE_PALETTE)]


class RestaurantTable(Base):
    """Section 4.1.1 — the floor plan. 26-50 tables."""
    __tablename__ = "restaurant_table"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    zone_id: Mapped[int | None] = mapped_column(ForeignKey("zone.id"), nullable=True)
    # Denormalized copy of zone_ref.name, kept in sync on every write. The ETL
    # (etl.py) and the floor plan read this string directly, and DimTable
    # stores it; carrying it here keeps a historical zone label resolvable even
    # if the Zone row is later renamed. zone_id is the source of truth.
    zone: Mapped[str] = mapped_column(String(40), default="Main")
    capacity: Mapped[int] = mapped_column(Integer, default=4)
    status: Mapped[str] = mapped_column(String(20), default=TableStatus.FREE, nullable=False)
    # A retired table keeps its history but leaves the floor plan. Tables are
    # never deleted: past orders, payments and DimTable rows still point here.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Floor-plan grid coordinates for the visual layout.
    pos_x: Mapped[int] = mapped_column(Integer, default=0)
    pos_y: Mapped[int] = mapped_column(Integer, default=0)
    current_waiter_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)

    current_waiter: Mapped["Staff | None"] = relationship("Staff", lazy="joined")
    zone_ref: Mapped["Zone | None"] = relationship("Zone", lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "status IN ('free','occupied','ready_to_pay')", name="ck_table_status"
        ),
    )

    @property
    def floor(self) -> "Floor | None":
        return self.zone_ref.floor if self.zone_ref else None

    @property
    def floor_id(self) -> int | None:
        """Which grid this table lives on. Coordinates are unique per floor."""
        return self.zone_ref.floor_id if self.zone_ref else None


class Channel(Base):
    """Sales channel: dine-in, own delivery, UberEats, DoorDash (section 4.1.4)."""
    __tablename__ = "channel"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(20), nullable=False)  # dine_in | delivery
    is_third_party: Mapped[bool] = mapped_column(Boolean, default=False)


class MenuCategory(Base):
    __tablename__ = "menu_category"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    items: Mapped[list["MenuItem"]] = relationship(back_populates="category")


class MenuItem(Base):
    __tablename__ = "menu_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("menu_category.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # is_active is the owner's "on the menu at all" flag (admin). available is
    # the kitchen's "in stock right now" flag — 86'd mid-service and put back
    # later. An item is orderable only when both are true; separating them keeps
    # a temporary sell-out distinct from a permanent menu change.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Section 4.2.4 — shared items (bread, appetizers) can be split across seats.
    is_shareable: Mapped[bool] = mapped_column(Boolean, default=False)
    # A photo for the item row (4.1.2). Empty falls back to a category emoji.
    image_url: Mapped[str] = mapped_column(String(300), default="")

    category: Mapped["MenuCategory"] = relationship(back_populates="items")

    @property
    def thumb_emoji(self) -> str:
        """A food emoji standing in for a missing photo, from the category."""
        return category_emoji(self.category.name if self.category else "")

    @property
    def requires_choice(self) -> bool:
        """True if adding needs the configurator (a required modifier group)."""
        return any(g.required or g.min_select for g in self.modifier_groups)
    modifier_groups: Mapped[list["ModifierGroup"]] = relationship(
        back_populates="menu_item", cascade="all, delete-orphan",
        order_by="ModifierGroup.sort_order, ModifierGroup.id",
    )

    @property
    def default_course(self) -> int:
        """The course this item falls into by default, from its menu section."""
        return course_for_category(self.category.name if self.category else "")


class Modifier(Base):
    """Section 4.1.2 — modifiers and special instructions per item."""
    __tablename__ = "modifier"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    price_delta_cents: Mapped[int] = mapped_column(Integer, default=0)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("menu_category.id"), nullable=True)


class ModifierGroup(Base):
    """Section 4.1.2 — a set of choices for a menu item (e.g. "Cooking level",
    "Cheese", "Add-ons"), with rules on how many may be picked.

    required + min_select force a choice ("choose one cooking level"); max_select
    caps it (0 = unlimited). Each option carries its own price delta, so add-ons
    can cost extra while a cooking level is free.
    """
    __tablename__ = "modifier_group"

    id: Mapped[int] = mapped_column(primary_key=True)
    menu_item_id: Mapped[int] = mapped_column(
        ForeignKey("menu_item.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    min_select: Mapped[int] = mapped_column(Integer, default=0)
    max_select: Mapped[int] = mapped_column(Integer, default=0)   # 0 = unlimited
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    menu_item: Mapped["MenuItem"] = relationship(back_populates="modifier_groups")
    options: Mapped[list["ModifierOption"]] = relationship(
        back_populates="group", cascade="all, delete-orphan",
        order_by="ModifierOption.sort_order, ModifierOption.id",
        lazy="selectin",
    )

    @property
    def single(self) -> bool:
        """True when at most one option may be chosen (radio, not checkboxes)."""
        return self.max_select == 1


class ModifierOption(Base):
    """One choice within a ModifierGroup, with its price delta (4.1.2)."""
    __tablename__ = "modifier_option"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("modifier_group.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    price_delta_cents: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    group: Mapped["ModifierGroup"] = relationship(back_populates="options")


class DayMenu(Base):
    """A fixed-price prix fixe ("day menu"). The manager composes it from any
    set of course slots (see DAY_MENU_COURSES) — nothing is mandatory — and each
    included slot offers one or more items the guest chooses between.

    Scheduling is open: a menu is tied to EITHER a specific calendar date OR a
    recurring weekday (0=Mon..6=Sun). When both a dated and a weekday menu match
    a day, the specific date wins (see services/daymenu.resolve_for).
    """
    __tablename__ = "day_menu"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    menu_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    choices: Mapped[list["DayMenuChoice"]] = relationship(
        back_populates="day_menu", cascade="all, delete-orphan",
        order_by="DayMenuChoice.course, DayMenuChoice.sort_order, DayMenuChoice.id",
        lazy="selectin",
    )

    @property
    def courses(self) -> list[int]:
        """The distinct course slots this menu actually uses, in order."""
        seen: list[int] = []
        for c in self.choices:
            if c.course not in seen:
                seen.append(c.course)
        return sorted(seen)


class DayMenuChoice(Base):
    """One selectable item within a day menu's course slot. Several rows for the
    same (day_menu, course) means the guest picks one; a single row is fixed."""
    __tablename__ = "day_menu_choice"

    id: Mapped[int] = mapped_column(primary_key=True)
    day_menu_id: Mapped[int] = mapped_column(
        ForeignKey("day_menu.id", ondelete="CASCADE"), nullable=False
    )
    course: Mapped[int] = mapped_column(Integer, nullable=False)   # DAY_MENU_COURSES slot
    menu_item_id: Mapped[int] = mapped_column(ForeignKey("menu_item.id"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    day_menu: Mapped["DayMenu"] = relationship(back_populates="choices")
    menu_item: Mapped["MenuItem"] = relationship(lazy="joined")


class PaymentInstrument(Base):
    """Section 4.2.1 — the accepted payment instruments."""
    __tablename__ = "payment_instrument"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    # card | cash | contactless | etransfer | platform
    instrument_type: Mapped[str] = mapped_column(String(20), nullable=False)
    card_brand: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Visa/Mastercard/Amex
    is_third_party: Mapped[bool] = mapped_column(Boolean, default=False)
    # UberEats/DoorDash are valid on delivery orders only (section 4.2.1).
    delivery_only: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

class Order(Base):
    __tablename__ = "order"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    table_id: Mapped[int | None] = mapped_column(ForeignKey("restaurant_table.id"), nullable=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channel.id"), nullable=False)
    waiter_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.OPEN, nullable=False)
    kitchen_status: Mapped[str] = mapped_column(String(20), default=KitchenStatus.PENDING)
    guest_count: Mapped[int] = mapped_column(Integer, default=1)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    sent_to_kitchen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    table: Mapped["RestaurantTable | None"] = relationship(lazy="joined")
    channel: Mapped["Channel"] = relationship(lazy="joined")
    waiter: Mapped["Staff | None"] = relationship(lazy="joined")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan",
        foreign_keys="OrderItem.order_id",
    )
    seats: Mapped[list["Seat"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="Seat.seat_number"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    delivery: Mapped["DeliveryOrder | None"] = relationship(
        back_populates="order", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_order_status_opened", "status", "opened_at"),)


class Seat(Base):
    """Section 4.2.4 — each seat at a table is an independent payer."""
    __tablename__ = "seat"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("order.id", ondelete="CASCADE"), nullable=False)
    seat_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(20), default=SeatStatus.OPEN, nullable=False)
    tip_cents: Mapped[int] = mapped_column(Integer, default=0)

    order: Mapped["Order"] = relationship(back_populates="seats")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="seat")

    __table_args__ = (
        UniqueConstraint("order_id", "seat_number", name="uq_seat_order_number"),
        CheckConstraint("status IN ('open','paid_partial','paid')", name="ck_seat_status"),
    )


class OrderItem(Base):
    __tablename__ = "order_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("order.id", ondelete="CASCADE"), nullable=False)
    menu_item_id: Mapped[int] = mapped_column(ForeignKey("menu_item.id"), nullable=False)
    # Section 4.2.4 — item assigned to a seat at order creation or at payment time,
    # so this is nullable until assigned. NULL also means "shared/table item".
    seat_id: Mapped[int | None] = mapped_column(ForeignKey("seat.id"), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    # 4.1.2 — allergy flags for this line (e.g. "Lactose, Nuts, Other: shellfish
    # broth"). Kept distinct from notes so the kitchen ticket can surface them
    # prominently. Comma-separated; empty when none.
    allergens: Mapped[str] = mapped_column(String(200), default="")
    kitchen_status: Mapped[str] = mapped_column(String(20), default=KitchenStatus.PENDING)
    # Section 4.1.3 — coursing. Which stage of the meal this line belongs to, so
    # the kitchen fires starters, then mains, then dessert rather than all at
    # once. Defaults to Mains (2): an untagged item is treated as a main and
    # fires normally, so an order that ignores courses behaves as before.
    course: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # Section 4.2.4 — shared items split proportionally across selected seats.
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)
    # Day-menu (prix fixe) grouping. All the component lines of one ordered combo
    # share a combo_id; the first (header) line carries combo_name and the whole
    # fixed price is spread across the group's unit prices, so it bills as one
    # entry. NULL for an ordinary à-la-carte line.
    combo_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    combo_name: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # Provenance: set when this line arrives via a table merge (4.1.1), pointing
    # at the (now-dissolved) order it came from. Drives the "from Table N" badge
    # and keeps an on-record trail of what moved between tables.
    merged_from_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("order.id"), nullable=True
    )

    order: Mapped["Order"] = relationship(
        back_populates="items", foreign_keys=[order_id]
    )
    merged_from_order: Mapped["Order | None"] = relationship(
        "Order", foreign_keys=[merged_from_order_id], lazy="joined"
    )
    menu_item: Mapped["MenuItem"] = relationship(lazy="joined")
    seat: Mapped["Seat | None"] = relationship(back_populates="items")
    modifiers: Mapped[list["OrderItemModifier"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    options: Mapped[list["OrderItemOption"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin",
        order_by="OrderItemOption.id",
    )
    shares: Mapped[list["SharedItemShare"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    allocations: Mapped[list["PaymentAllocation"]] = relationship(back_populates="order_item")

    @property
    def modifier_total_cents(self) -> int:
        # Both legacy flat modifiers and grouped-option selections add to the line.
        return (sum(m.price_delta_cents for m in self.modifiers)
                + sum(o.price_delta_cents for o in self.options))

    @property
    def line_total_cents(self) -> int:
        """Gross value of this line, including modifiers."""
        return (self.unit_price_cents + self.modifier_total_cents) * self.quantity


class OrderItemModifier(Base):
    __tablename__ = "order_item_modifier"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("order_item.id", ondelete="CASCADE"), nullable=False
    )
    modifier_id: Mapped[int] = mapped_column(ForeignKey("modifier.id"), nullable=False)
    # Price captured at time of order — the modifier's price may change later.
    price_delta_cents: Mapped[int] = mapped_column(Integer, default=0)

    modifier: Mapped["Modifier"] = relationship(lazy="joined")


class OrderItemOption(Base):
    """A chosen modifier-group option on an order line (4.1.2), snapshotted.

    Records the group and option names and the price delta at order time, so
    the line, receipt and kitchen ticket stay correct even if the menu's
    modifier definitions change later.
    """
    __tablename__ = "order_item_option"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("order_item.id", ondelete="CASCADE"), nullable=False
    )
    option_id: Mapped[int | None] = mapped_column(
        ForeignKey("modifier_option.id"), nullable=True
    )
    group_name: Mapped[str] = mapped_column(String(80), default="")
    label: Mapped[str] = mapped_column(String(80), default="")
    price_delta_cents: Mapped[int] = mapped_column(Integer, default=0)


class SharedItemShare(Base):
    """Section 4.2.4 — proportional split of one shared item across seats.

    Shares are stored in cents and always sum exactly to the item's line total;
    the remainder from integer division is distributed rather than dropped.
    """
    __tablename__ = "shared_item_share"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("order_item.id", ondelete="CASCADE"), nullable=False
    )
    seat_id: Mapped[int] = mapped_column(ForeignKey("seat.id", ondelete="CASCADE"), nullable=False)
    share_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("order_item_id", "seat_id", name="uq_share_item_seat"),
    )


class DeliveryOrder(Base):
    """Section 4.1.4 — delivery-specific attributes."""
    __tablename__ = "delivery_order"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("order.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # Third-party platform reference number (section 4.1.4).
    platform_ref: Mapped[str | None] = mapped_column(String(60), nullable=True)
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=DeliveryStatus.PENDING, nullable=False)
    customer_name: Mapped[str] = mapped_column(String(120), default="")
    customer_phone: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    order: Mapped["Order"] = relationship(back_populates="delivery")
    driver: Mapped["Staff | None"] = relationship(lazy="joined")


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------

class Discount(Base):
    """Section 4.2.6 — discounts require manager approval and are recorded separately."""
    __tablename__ = "discount"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("order.id", ondelete="CASCADE"), nullable=False)
    seat_id: Mapped[int | None] = mapped_column(ForeignKey("seat.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # percent | fixed
    value: Mapped[int] = mapped_column(Integer, nullable=False)     # percent points, or cents
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), default="")
    approved_by_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    approved_by: Mapped["Staff"] = relationship(lazy="joined")


class Payment(Base):
    """One tender event: a seat (or a whole order) paying with one instrument.

    A single order can have many payments across different instruments
    (section 4.2.2), and a seat that leaves early produces a payment flagged
    with is_partial_close (section 4.2.5).
    """
    __tablename__ = "payment"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("order.id", ondelete="CASCADE"), nullable=False)
    seat_id: Mapped[int | None] = mapped_column(ForeignKey("seat.id"), nullable=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("payment_instrument.id"), nullable=False)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)

    items_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tip_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 4.2.6 — mandatory house service charge, a percent of (items - discount).
    # House revenue (not the waiter's tip) and, like the tip, not itself taxed,
    # so the stored total is items - discount + tax + tip + service_charge.
    service_charge_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Optional card surcharge (settings.card_surcharge_rate), charged only on
    # card payments. Like the service charge, it is not itself taxed, so the
    # stored total is items - discount + tax + tip + service_charge + card_surcharge.
    card_surcharge_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Combined sales tax (GST + PST) on (items - discount). Zero on payments
    # taken before tax existed, so their stored total still equals
    # items - discount + tip and reconciles. The GST/PST split for the receipt
    # lives in the receipt payload, not here — reporting needs only the total.
    tax_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Security (section 5): never store a raw card number — brand + last 4 only.
    card_brand: Mapped[str | None] = mapped_column(String(20), nullable=True)
    card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)

    # Section 4.2.5 — partial closes are recorded separately but stay linked to
    # the same table order ID for traceability.
    is_partial_close: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)

    # A voided payment is kept, not deleted, so the money history stays
    # auditable. Its allocations are removed on void, which reopens the items
    # and drops it out of every "sum of allocations" calculation; the sums over
    # order.payments (revenue, ETL) must additionally skip voided rows.
    voided: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    void_reason: Mapped[str] = mapped_column(String(200), default="")

    order: Mapped["Order"] = relationship(back_populates="payments")
    seat: Mapped["Seat | None"] = relationship(lazy="joined")
    instrument: Mapped["PaymentInstrument"] = relationship(lazy="joined")
    # Two FKs point at staff (who took it, who voided it), so each relationship
    # must name its column explicitly.
    staff: Mapped["Staff"] = relationship(lazy="joined", foreign_keys=[staff_id])
    voided_by: Mapped["Staff | None"] = relationship(
        lazy="joined", foreign_keys=[voided_by_id]
    )
    allocations: Mapped[list["PaymentAllocation"]] = relationship(
        back_populates="payment", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("total_cents >= 0", name="ck_payment_total_nonneg"),
        Index("ix_payment_order_created", "order_id", "created_at"),
    )


class PaymentAllocation(Base):
    """Section 4.2.2 — the item-to-instrument link.

    This is what makes "2 items on Visa, 1 item in Cash" answerable. Every
    payment distributes its item value across the specific order items it
    covers, so each item is traceable to the instrument that paid for it.
    """
    __tablename__ = "payment_allocation"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payment.id", ondelete="CASCADE"), nullable=False
    )
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_item.id"), nullable=False)
    seat_id: Mapped[int | None] = mapped_column(ForeignKey("seat.id"), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    payment: Mapped["Payment"] = relationship(back_populates="allocations")
    order_item: Mapped["OrderItem"] = relationship(back_populates="allocations")

    __table_args__ = (Index("ix_alloc_item", "order_item_id"),)


class Receipt(Base):
    """Sections 4.2.3 / 4.2.4 / 4.2.7 — sub-receipt per guest, receipt per seat."""
    __tablename__ = "receipt"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("order.id", ondelete="CASCADE"), nullable=False)
    seat_id: Mapped[int | None] = mapped_column(ForeignKey("seat.id"), nullable=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payment.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="seat")  # full | seat | partial
    delivery_method: Mapped[str] = mapped_column(String(10), default="print")  # print|email|sms
    destination: Mapped[str] = mapped_column(String(160), default="")
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


class DayClose(Base):
    """End-of-day cash reconciliation — workflow 6.3, the Z-report.

    One row per close. A close snapshots every non-voided payment taken in its
    window (from the previous close's timestamp up to this one), so successive
    closes tile the trading history without gaps or overlap. The money figures
    are frozen at close time: this is an operational record of what the drawer
    should have held versus what was counted, not a live query.
    """
    __tablename__ = "day_close"

    id: Mapped[int] = mapped_column(primary_key=True)
    closed_by_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)

    # Drawer reconciliation (cash only). Expected cash already nets cash refunds
    # paid out in the window.
    opening_float_cents: Mapped[int] = mapped_column(Integer, default=0)
    expected_cash_cents: Mapped[int] = mapped_column(Integer, default=0)
    counted_cash_cents: Mapped[int] = mapped_column(Integer, default=0)
    variance_cents: Mapped[int] = mapped_column(Integer, default=0)  # counted - float - expected

    # Post-settlement refunds paid out in the window.
    refund_cents: Mapped[int] = mapped_column(Integer, default=0)
    cash_refund_cents: Mapped[int] = mapped_column(Integer, default=0)

    # Snapshot of the whole window, all instruments.
    gross_sales_cents: Mapped[int] = mapped_column(Integer, default=0)   # item value
    discount_cents: Mapped[int] = mapped_column(Integer, default=0)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)
    tip_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_collected_cents: Mapped[int] = mapped_column(Integer, default=0)
    payment_count: Mapped[int] = mapped_column(Integer, default=0)
    # Per-instrument totals as JSON: {"Cash": 12345, "Visa": 6789, ...}
    by_instrument_json: Mapped[str] = mapped_column(Text, default="{}")
    notes: Mapped[str] = mapped_column(String(300), default="")

    closed_by: Mapped["Staff"] = relationship(lazy="joined")


class ReceiptDelivery(Base):
    """One record per time a receipt is emailed or texted (section 4.2.7).

    Kept separate from the Receipt row so a receipt can be re-sent, to more than
    one destination, without losing the trail. Each row is who sent what, where,
    when, and whether it went out.
    """
    __tablename__ = "receipt_delivery"

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipt.id", ondelete="CASCADE"), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)   # email | sms
    destination: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="sent")   # sent | failed
    detail: Mapped[str] = mapped_column(String(300), default="")      # outbox path or error
    sent_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)

    sent_by: Mapped["Staff | None"] = relationship(lazy="joined")


class Refund(Base):
    """A post-settlement reversal of money to the guest (section 4.2, manager).

    A void handles a mistake before the table is settled by removing the
    payment's allocations. A refund is different: the order is already closed
    and the table freed, so nothing is un-allocated — the item genuinely was
    sold and paid. The refund is a separate reversing entry that reduces net
    revenue and the drawer, while gross sales and the "every item is allocated"
    invariant stay exactly as they were. Partial refunds are allowed; the sum
    of refunds against a payment can never exceed what it collected.
    """
    __tablename__ = "refund"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payment.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("order.id"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    # How the money went back — usually the original instrument's name.
    method: Mapped[str] = mapped_column(String(60), default="")
    is_cash: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), default="")
    approved_by_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)

    approved_by: Mapped["Staff"] = relationship(lazy="joined")
    payment: Mapped["Payment"] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_refund_amount_pos"),
        Index("ix_refund_order", "order_id"),
    )


class AuditEvent(Base):
    """An immutable record of a table-level action, for the security trail.

    Moves and merges relocate a party's whole order, so who did it, when, and
    between which tables is worth keeping on record — both to explain a line
    that appears on a table it wasn't ordered at, and to answer "what happened
    to table N" after the fact. Append-only: rows are written, never edited.
    """
    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)  # move_table | merge_tables
    # Human-readable summary, e.g. "Table 4 → Table 9" or "Table 7 merged into Table 4".
    detail: Mapped[str] = mapped_column(String(300), default="")
    order_id: Mapped[int | None] = mapped_column(ForeignKey("order.id"), nullable=True)

    staff: Mapped["Staff | None"] = relationship(lazy="joined")

    __table_args__ = (Index("ix_audit_at", "at"),)


class ReservationStatus:
    WAITING = "waiting"        # booked ahead, or in the walk-in queue
    SEATED = "seated"          # arrived and given a table
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class Reservation(Base):
    """Section 4.1.5 — future bookings and the walk-in waitlist.

    One model serves both: a `reservation` is booked for a time; a `waitlist`
    entry is a walk-in queued now with a quoted wait. Both wait for a table,
    then get seated (which opens an order), or are cancelled / marked no-show.
    Money never touches this table, so it stays out of the star schema.
    """
    __tablename__ = "reservation"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # reservation | waitlist
    guest_name: Mapped[str] = mapped_column(String(120), nullable=False)
    party_size: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    phone: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(String(300), default="")
    # Booking time for a reservation; the moment they joined the queue for a
    # walk-in. Either way it's what the list sorts by.
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    quoted_minutes: Mapped[int] = mapped_column(Integer, default=0)   # waitlist estimate
    status: Mapped[str] = mapped_column(String(20), default=ReservationStatus.WAITING, nullable=False)
    table_id: Mapped[int | None] = mapped_column(ForeignKey("restaurant_table.id"), nullable=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("order.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)

    table: Mapped["RestaurantTable | None"] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('reservation','waitlist')", name="ck_reservation_kind"
        ),
        CheckConstraint(
            "status IN ('waiting','seated','cancelled','no_show')",
            name="ck_reservation_status",
        ),
        Index("ix_reservation_status_at", "status", "at"),
    )


class Shift(Base):
    """A scheduled work block for one staff member, plus its attendance.

    Section: staff scheduling. Each shift belongs to a single person (the
    industry-standard model) and carries both the *scheduled* window
    (starts_at/ends_at) and the *actual* clock in/out — so one row shows planned
    vs worked hours. staff_id is nullable so an open slot can be created and
    filled later. Money never touches this table; it stays out of the star schema.
    """
    __tablename__ = "shift"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff.id"), nullable=True
    )
    # Scheduled window. ends_at may fall on the next calendar day (a close shift
    # that runs past midnight), so both are full datetimes, not a date + times.
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Position worked this shift (Server, Bartender, Line Cook…). Colour-codes the
    # calendar block. `role` is kept as a plain-text fallback label.
    position_id: Mapped[int | None] = mapped_column(ForeignKey("position.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=Role.WAITER)
    notes: Mapped[str] = mapped_column(String(300), default="")
    # Attendance — the actual times, filled in when the member clocks in/out.
    clock_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    clock_out_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )

    staff: Mapped["Staff | None"] = relationship(lazy="joined")
    position: Mapped["Position | None"] = relationship(lazy="joined")

    __table_args__ = (
        Index("ix_shift_staff_starts", "staff_id", "starts_at"),
        Index("ix_shift_starts", "starts_at"),
    )


class TimeOffStatus:
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class TimeOffRequest(Base):
    """A staff member's request to be off for a date range.

    Staff file it; owner/manager approve or deny. Approved time off shows on the
    schedule and is a visible conflict against any shift placed over it. Money
    never touches this table.
    """
    __tablename__ = "time_off_request"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    # Inclusive day range: starts_at = 00:00 of the first day off, ends_at =
    # 23:59:59 of the last day off, so overlap maths match the shift datetimes.
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default=TimeOffStatus.PENDING, nullable=False)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)

    staff: Mapped["Staff"] = relationship(foreign_keys=[staff_id], lazy="joined")

    __table_args__ = (
        Index("ix_timeoff_status", "status", "starts_at"),
    )


class SwapStatus:
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class SwapRequest(Base):
    """A staff member offering one of their shifts to someone else (or to anyone).

    Approving reassigns the shift to the target (or opens it if no target).
    Owner/manager decide. Money never touches this table.
    """
    __tablename__ = "swap_request"

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shift.id"), nullable=False)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), nullable=False)
    # None = open swap (any teammate can be assigned by a manager on approval).
    target_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=SwapStatus.PENDING, nullable=False)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)

    shift: Mapped["Shift"] = relationship(lazy="joined")
    requested_by: Mapped["Staff"] = relationship(foreign_keys=[requested_by_id], lazy="joined")
    target: Mapped["Staff | None"] = relationship(foreign_keys=[target_staff_id], lazy="joined")

    __table_args__ = (
        Index("ix_swap_status", "status", "created_at"),
    )


class SalesForecast(Base):
    """An owner's forecast sales for a day, used to compute the schedule's labor
    percentage (labor cost ÷ forecast). Money-planning input, not actual sales."""
    __tablename__ = "sales_forecast"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    forecast_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Position(Base):
    """A schedulable job (Server, Bartender, Host, Line Cook…) with a colour.

    Richer than the five auth Roles — a schedule can distinguish a Bartender from
    a Host though both log in as 'waiter'. Tags a shift and colour-codes its block
    on the calendar. Owner-managed under Manage.
    """
    __tablename__ = "position"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default="#3b82f6")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
