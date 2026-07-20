"""Dimensional model (star schema) for Reports & Analytics — section 4.3.

Design notes
------------
The OLTP schema in oltp.py is normalized for correctness during service. It is
a poor fit for the reports in section 4.3, which slice revenue by day, hour,
staff, item, channel and instrument. Those are classic star-schema questions,
so reporting reads from conformed dimensions and three fact tables instead of
joining six operational tables per query.

Grain — declared explicitly, because grain is the contract of a fact table:

  fact_order_item     one row per order item per order        (what was sold)
  fact_payment        one row per payment allocation          (how it was paid)
  fact_order_header   one row per closed order                (ticket-level rollup)

fact_order_item and fact_payment are deliberately separate. They answer
different questions at different grains, and a single order item may be paid by
more than one instrument (section 4.2.2), so merging them would either double
count revenue or lose the instrument detail.

Surrogate keys are integers assigned by the ETL. Every dimension reserves key
-1 as the "Unknown / Not applicable" member so facts never carry a NULL foreign
key (a delivery order has no table; a third-party order has no waiter).

dim_staff and dim_menu_item are Type 2 slowly-changing: if a waiter is
promoted or an item is repriced, historical facts stay attached to the version
that was true at the time, so last month's report does not silently change.
"""
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

UNKNOWN_KEY = -1


# --------------------------------------------------------------------------
# Dimensions
# --------------------------------------------------------------------------

class DimDate(Base):
    """Calendar dimension. Key is YYYYMMDD, the standard readable date key."""
    __tablename__ = "dim_date"

    date_key: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    full_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    quarter: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    month_name: Mapped[str] = mapped_column(String(12), nullable=False)
    day: Mapped[int] = mapped_column(Integer, nullable=False)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 1=Mon .. 7=Sun
    day_name: Mapped[str] = mapped_column(String(12), nullable=False)
    week_of_year: Mapped[int] = mapped_column(Integer, nullable=False)
    is_weekend: Mapped[bool] = mapped_column(Boolean, nullable=False)


class DimTime(Base):
    """Time-of-day dimension at minute grain. Key is HHMM.

    Exists to serve the peak-hours chart (4.3.1) and shift filtering (4.3.4)
    without date arithmetic in the report queries.
    """
    __tablename__ = "dim_time"

    time_key: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    hour: Mapped[int] = mapped_column(Integer, nullable=False)
    minute: Mapped[int] = mapped_column(Integer, nullable=False)
    hour_label: Mapped[str] = mapped_column(String(8), nullable=False)   # "19:00"
    service_period: Mapped[str] = mapped_column(String(20), nullable=False)
    shift: Mapped[str] = mapped_column(String(20), nullable=False)       # Morning|Afternoon|Evening|Late


class DimStaff(Base):
    """Type 2 SCD — preserves a staff member's role as of each transaction."""
    __tablename__ = "dim_staff"

    staff_key: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    staff_id: Mapped[int] = mapped_column(Integer, nullable=False)  # natural key
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    valid_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (Index("ix_dim_staff_natural", "staff_id", "is_current"),)


class DimMenuItem(Base):
    """Type 2 SCD — preserves the price and category in force at sale time."""
    __tablename__ = "dim_menu_item"

    item_key: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    menu_item_id: Mapped[int] = mapped_column(Integer, nullable=False)  # natural key
    item_name: Mapped[str] = mapped_column(String(120), nullable=False)
    category_name: Mapped[str] = mapped_column(String(60), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    is_shareable: Mapped[bool] = mapped_column(Boolean, default=False)

    valid_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (Index("ix_dim_item_natural", "menu_item_id", "is_current"),)


class DimTable(Base):
    """Type 1 — table layout changes are corrections, not history worth keeping."""
    __tablename__ = "dim_table"

    table_key: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    table_number: Mapped[int] = mapped_column(Integer, nullable=False)
    zone: Mapped[str] = mapped_column(String(40), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)


class DimChannel(Base):
    """Serves 4.3.5 — delivery vs dine-in, and UberEats vs DoorDash vs own driver."""
    __tablename__ = "dim_channel"

    channel_key: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    channel_code: Mapped[str] = mapped_column(String(30), nullable=False)
    channel_name: Mapped[str] = mapped_column(String(60), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(20), nullable=False)  # dine_in | delivery
    is_third_party: Mapped[bool] = mapped_column(Boolean, default=False)


class DimPaymentInstrument(Base):
    """Serves 4.3.3 — revenue by method, with card-level Visa/MC/Amex detail."""
    __tablename__ = "dim_payment_instrument"

    instrument_key: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    instrument_code: Mapped[str] = mapped_column(String(30), nullable=False)
    instrument_name: Mapped[str] = mapped_column(String(60), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(20), nullable=False)
    card_brand: Mapped[str] = mapped_column(String(20), default="N/A")
    # Pre-computed rollup so the payment-breakdown report groups without CASE logic.
    method_group: Mapped[str] = mapped_column(String(20), nullable=False)
    is_card: Mapped[bool] = mapped_column(Boolean, default=False)
    is_third_party: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------
# Facts
# --------------------------------------------------------------------------

class FactOrderItem(Base):
    """Grain: one row per order item. Drives best sellers (4.3.2).

    Additive measures: quantity, gross/discount/net cents.
    """
    __tablename__ = "fact_order_item"

    order_item_sk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    date_key: Mapped[int] = mapped_column(ForeignKey("dim_date.date_key"), nullable=False)
    time_key: Mapped[int] = mapped_column(ForeignKey("dim_time.time_key"), nullable=False)
    item_key: Mapped[int] = mapped_column(ForeignKey("dim_menu_item.item_key"), nullable=False)
    staff_key: Mapped[int] = mapped_column(ForeignKey("dim_staff.staff_key"), nullable=False)
    table_key: Mapped[int] = mapped_column(ForeignKey("dim_table.table_key"), nullable=False)
    channel_key: Mapped[int] = mapped_column(ForeignKey("dim_channel.channel_key"), nullable=False)

    # Degenerate dimensions — operational identifiers kept for drill-back.
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    order_code: Mapped[str] = mapped_column(String(20), nullable=False)
    order_item_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    seat_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    modifier_cents: Mapped[int] = mapped_column(Integer, default=0)
    gross_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0)
    net_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_foi_date", "date_key"),
        Index("ix_foi_item_date", "item_key", "date_key"),
        Index("ix_foi_channel_date", "channel_key", "date_key"),
    )


class FactPayment(Base):
    """Grain: one row per payment allocation (payment x order item).

    This is the fact that makes section 4.2.2 reportable: because the grain
    descends to the allocation, "which instrument paid for this item" is a
    lookup, not a reconstruction. Drives the payment breakdown (4.3.3).

    tip_cents and discount_cents are allocated down from the parent payment
    proportionally, so they remain additive at this grain and summing them
    across a day reproduces the payment-level totals exactly.
    """
    __tablename__ = "fact_payment"

    payment_sk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    date_key: Mapped[int] = mapped_column(ForeignKey("dim_date.date_key"), nullable=False)
    time_key: Mapped[int] = mapped_column(ForeignKey("dim_time.time_key"), nullable=False)
    instrument_key: Mapped[int] = mapped_column(
        ForeignKey("dim_payment_instrument.instrument_key"), nullable=False
    )
    staff_key: Mapped[int] = mapped_column(ForeignKey("dim_staff.staff_key"), nullable=False)
    channel_key: Mapped[int] = mapped_column(ForeignKey("dim_channel.channel_key"), nullable=False)
    item_key: Mapped[int] = mapped_column(ForeignKey("dim_menu_item.item_key"), nullable=False)
    table_key: Mapped[int] = mapped_column(ForeignKey("dim_table.table_key"), nullable=False)

    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    order_code: Mapped[str] = mapped_column(String(20), nullable=False)
    payment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    order_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seat_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)

    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    tip_cents: Mapped[int] = mapped_column(Integer, default=0)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    is_partial_close: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_fp_date", "date_key"),
        Index("ix_fp_instrument_date", "instrument_key", "date_key"),
        Index("ix_fp_staff_date", "staff_key", "date_key"),
    )


class FactOrderHeader(Base):
    """Grain: one row per closed order. Drives 4.3.1, 4.3.4 and 4.3.5.

    A ticket-level rollup so "average ticket value" and "number of orders" are
    single-table scans rather than distinct-counts over the item fact.
    guest_count and the *_cents totals are additive; avg ticket is computed as
    SUM(total)/COUNT(*) at query time rather than stored, since averages do not
    aggregate correctly across rows.
    """
    __tablename__ = "fact_order_header"

    order_sk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Role-playing date dimension. date_key/time_key are when the order OPENED
    # (demand: when guests arrive, what the peak-hours chart is about);
    # close_date_key/close_time_key are when it SETTLED (revenue recognition).
    # They differ for orders that run past midnight, and the end-of-day cash
    # reconciliation in 6.3 counts money on the day it was taken, so revenue
    # reports must key on the close date or they will not tie to the drawer.
    date_key: Mapped[int] = mapped_column(ForeignKey("dim_date.date_key"), nullable=False)
    time_key: Mapped[int] = mapped_column(ForeignKey("dim_time.time_key"), nullable=False)
    close_date_key: Mapped[int] = mapped_column(ForeignKey("dim_date.date_key"), nullable=False)
    close_time_key: Mapped[int] = mapped_column(ForeignKey("dim_time.time_key"), nullable=False)
    staff_key: Mapped[int] = mapped_column(ForeignKey("dim_staff.staff_key"), nullable=False)
    table_key: Mapped[int] = mapped_column(ForeignKey("dim_table.table_key"), nullable=False)
    channel_key: Mapped[int] = mapped_column(ForeignKey("dim_channel.channel_key"), nullable=False)

    order_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    order_code: Mapped[str] = mapped_column(String(20), nullable=False)

    guest_count: Mapped[int] = mapped_column(Integer, default=1)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    seats_paid: Mapped[int] = mapped_column(Integer, default=0)
    payment_count: Mapped[int] = mapped_column(Integer, default=0)
    distinct_instruments: Mapped[int] = mapped_column(Integer, default=0)
    had_partial_close: Mapped[bool] = mapped_column(Boolean, default=False)

    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0)
    tip_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_foh_date", "date_key"),
        Index("ix_foh_close_date", "close_date_key"),
        Index("ix_foh_channel_date", "channel_key", "close_date_key"),
        Index("ix_foh_staff_date", "staff_key", "close_date_key"),
    )


class EtlWatermark(Base):
    """Tracks what the ETL has already loaded so runs are incremental."""
    __tablename__ = "etl_watermark"

    id: Mapped[int] = mapped_column(primary_key=True)
    process_name: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    last_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_order_id: Mapped[int] = mapped_column(Integer, default=0)
    rows_loaded: Mapped[int] = mapped_column(Integer, default=0)
