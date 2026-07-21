"""Normalized transactional (OLTP) schema.

This is the operational side of the system: what waiters, kitchen staff and
the delivery coordinator touch during service. It is the source of truth and
the source system for the dimensional model in star.py.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
    READY = "ready"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class KitchenStatus:
    """Section 4.1.3 — Pending -> Preparing -> Ready."""
    PENDING = "pending"
    PREPARING = "preparing"
    READY = "ready"


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


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    pin_code: Mapped[str] = mapped_column(String(8), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    hired_on: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

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
ZONE_PALETTE = (
    "#e2a03f",  # amber
    "#4f8cff",  # blue
    "#2f9e5e",  # green
    "#9b6dff",  # purple
    "#28b3b3",  # teal
    "#e06c9f",  # pink
    "#d4834b",  # clay
    "#8a9bb0",  # slate
)


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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Section 4.2.4 — shared items (bread, appetizers) can be split across seats.
    is_shareable: Mapped[bool] = mapped_column(Boolean, default=False)

    category: Mapped["MenuCategory"] = relationship(back_populates="items")


class Modifier(Base):
    """Section 4.1.2 — modifiers and special instructions per item."""
    __tablename__ = "modifier"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    price_delta_cents: Mapped[int] = mapped_column(Integer, default=0)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("menu_category.id"), nullable=True)


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
        back_populates="order", cascade="all, delete-orphan"
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
    kitchen_status: Mapped[str] = mapped_column(String(20), default=KitchenStatus.PENDING)
    # Section 4.2.4 — shared items split proportionally across selected seats.
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    order: Mapped["Order"] = relationship(back_populates="items")
    menu_item: Mapped["MenuItem"] = relationship(lazy="joined")
    seat: Mapped["Seat | None"] = relationship(back_populates="items")
    modifiers: Mapped[list["OrderItemModifier"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    shares: Mapped[list["SharedItemShare"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    allocations: Mapped[list["PaymentAllocation"]] = relationship(back_populates="order_item")

    @property
    def modifier_total_cents(self) -> int:
        return sum(m.price_delta_cents for m in self.modifiers)

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
    discount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # GST on (items - discount). Zero on payments taken before tax existed, so
    # their stored total still equals items - discount + tip and reconciles.
    tax_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Security (section 5): never store a raw card number — brand + last 4 only.
    card_brand: Mapped[str | None] = mapped_column(String(20), nullable=True)
    card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)

    # Section 4.2.5 — partial closes are recorded separately but stay linked to
    # the same table order ID for traceability.
    is_partial_close: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)

    order: Mapped["Order"] = relationship(back_populates="payments")
    seat: Mapped["Seat | None"] = relationship(lazy="joined")
    instrument: Mapped["PaymentInstrument"] = relationship(lazy="joined")
    staff: Mapped["Staff"] = relationship(lazy="joined")
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
