"""Payment Processing — section 4.2.

The central idea is the *seat ledger*. Section 4.2.4 makes every seat an
independent payer, so the unit of billing is not the order and not the item but
a seat's claim on a set of item value:

  - items assigned to the seat contribute their whole line total
  - shared items contribute only that seat's proportional share (4.2.4)

Payment then draws down each line's outstanding balance and writes a
PaymentAllocation per line, which is what links every item to the specific
instrument that paid for it (4.2.2) and what lets one order be settled across
several instruments.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.oltp import (
    Discount,
    KitchenStatus,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentAllocation,
    PaymentInstrument,
    Receipt,
    Seat,
    SeatStatus,
    SharedItemShare,
    TableStatus,
)
from app.services import settings as settings_svc
from app.services.money import distribute, money, pct, split_evenly


# --------------------------------------------------------------------------
# Read model
# --------------------------------------------------------------------------

@dataclass
class SeatLine:
    """One seat's claim on one order item."""
    item: OrderItem
    claim_cents: int          # what this seat owes for this item
    paid_cents: int           # already settled by this seat against this item
    is_shared: bool

    @property
    def outstanding_cents(self) -> int:
        return max(0, self.claim_cents - self.paid_cents)


@dataclass
class SeatLedger:
    seat: Seat
    lines: list[SeatLine] = field(default_factory=list)

    @property
    def subtotal_cents(self) -> int:
        return sum(l.claim_cents for l in self.lines)

    @property
    def paid_cents(self) -> int:
        return sum(l.paid_cents for l in self.lines)

    @property
    def outstanding_cents(self) -> int:
        return sum(l.outstanding_cents for l in self.lines)

    @property
    def tip_cents(self) -> int:
        return self.seat.tip_cents or 0

    @property
    def total_owing_cents(self) -> int:
        """Section 4.2.4 — seat displays items, tip, and total owing."""
        return self.outstanding_cents + (self.tip_cents if self.outstanding_cents else 0)

    @property
    def is_settled(self) -> bool:
        return self.outstanding_cents == 0 and self.subtotal_cents > 0


@dataclass
class BalancePanel:
    """Section 4.2.4 — live balance panel, updated as each seat pays."""
    table_total_cents: int
    collected_cents: int
    owed_cents: int
    seats_total: int
    seats_paid: int
    tips_cents: int
    # 4.2.6 — previews of what settling the rest will add. Estimated on the
    # amount still owed; zero when the setting is off.
    service_charge_cents: int = 0
    service_charge_rate: float = 0.0
    auto_gratuity_cents: int = 0
    auto_gratuity_rate: float = 0.0

    @property
    def seats_remaining(self) -> int:
        return self.seats_total - self.seats_paid


def _paid_by_seat_for_item(item: OrderItem, seat_id: int | None) -> int:
    return sum(
        a.amount_cents for a in item.allocations if a.seat_id == seat_id
    )


def build_ledgers(db: Session, order: Order) -> tuple[dict[int, SeatLedger], list[OrderItem]]:
    """Return per-seat ledgers plus any items not yet assigned to a seat.

    Unassigned items are surfaced rather than silently absorbed — a waiter must
    assign them (4.2.4 allows assignment at order creation or at payment time)
    before the table can be fully settled.
    """
    ledgers: dict[int, SeatLedger] = {s.id: SeatLedger(seat=s) for s in order.seats}
    unassigned: list[OrderItem] = []

    for item in order.items:
        if item.is_shared:
            if not item.shares:
                unassigned.append(item)
                continue
            for share in item.shares:
                led = ledgers.get(share.seat_id)
                if led is None:
                    continue
                led.lines.append(
                    SeatLine(
                        item=item,
                        claim_cents=share.share_cents,
                        paid_cents=_paid_by_seat_for_item(item, share.seat_id),
                        is_shared=True,
                    )
                )
        elif item.seat_id is None:
            unassigned.append(item)
        else:
            led = ledgers.get(item.seat_id)
            if led is None:
                unassigned.append(item)
                continue
            led.lines.append(
                SeatLine(
                    item=item,
                    claim_cents=item.line_total_cents,
                    paid_cents=_paid_by_seat_for_item(item, item.seat_id),
                    is_shared=False,
                )
            )

    return ledgers, unassigned


def balance_panel(db: Session, order: Order) -> BalancePanel:
    ledgers, unassigned = build_ledgers(db, order)

    table_total = sum(i.line_total_cents for i in order.items)
    live = active_payments(order)
    collected = sum(p.total_cents for p in live)
    tips = sum(p.tip_cents for p in live)
    discounts = sum(p.discount_cents for p in live)
    # Owed is item value still outstanding; tips/discounts are not part of the
    # item debt, so they are excluded from the owed figure.
    owed = sum(led.outstanding_cents for led in ledgers.values())
    owed += sum(i.line_total_cents for i in unassigned)

    seats_paid = sum(
        1 for led in ledgers.values()
        if led.seat.status in (SeatStatus.PAID, SeatStatus.PAID_PARTIAL) and led.outstanding_cents == 0
    )

    # 4.2.6 — preview the mandatory service charge and any auto-gratuity on the
    # amount still owed, so they're visible before a payment is taken.
    svc_rate = settings_svc.service_charge_rate(db)
    grat = settings_svc.gratuity_config(db)
    grat_applies = grat.applies(order.guest_count)

    return BalancePanel(
        table_total_cents=table_total,
        collected_cents=collected,
        owed_cents=owed,
        seats_total=len(order.seats),
        seats_paid=seats_paid,
        tips_cents=tips,
        service_charge_cents=pct(owed, svc_rate),
        service_charge_rate=svc_rate,
        auto_gratuity_cents=pct(owed, grat.rate) if grat_applies else 0,
        auto_gratuity_rate=grat.rate if grat_applies else 0.0,
    )


# --------------------------------------------------------------------------
# Seat & share management
# --------------------------------------------------------------------------

def ensure_seats(db: Session, order: Order, count: int) -> list[Seat]:
    """Create seats 1..count for an order, preserving any that already exist."""
    existing = {s.seat_number: s for s in order.seats}
    for n in range(1, count + 1):
        if n not in existing:
            seat = Seat(order_id=order.id, seat_number=n, label=f"Seat {n}")
            db.add(seat)
    db.flush()
    db.refresh(order)
    return order.seats


def set_shared_item_shares(
    db: Session, item: OrderItem, seat_ids: list[int], weights: list[int] | None = None
) -> list[SharedItemShare]:
    """Section 4.2.4 — split a shared item proportionally across selected seats.

    Existing shares are replaced. Shares always sum to the item's exact line
    total; weights allow an uneven split (default is equal).
    """
    if not seat_ids:
        return []

    w = weights if weights and len(weights) == len(seat_ids) else [1] * len(seat_ids)
    amounts = distribute(item.line_total_cents, w)

    # Mutate through the relationship, never db.add()/db.delete() on the side.
    # Writing share rows directly leaves item.shares holding whatever was loaded
    # earlier, and every reader of item.shares (the seat ledger, settlement)
    # then treats the item as unshared and silently drops its value.
    item.shares.clear()  # delete-orphan cascade removes the old rows
    db.flush()
    for seat_id, amount in zip(seat_ids, amounts):
        item.shares.append(SharedItemShare(seat_id=seat_id, share_cents=amount))

    item.is_shared = True
    item.seat_id = None
    db.flush()
    return list(item.shares)


def split_equally(db: Session, order: Order, guests: int) -> None:
    """Section 4.2.3 — split the whole order equally among N guests.

    Implemented by making every item shared across all N seats, so the equal
    split still resolves to exact per-seat claims and each guest keeps the
    ability to choose their own instrument.
    """
    seats = ensure_seats(db, order, guests)
    seat_ids = [s.id for s in seats]
    for item in order.items:
        set_shared_item_shares(db, item, seat_ids)
    db.flush()


def assign_item_to_seat(db: Session, item: OrderItem, seat_id: int | None) -> None:
    """Section 4.2.4 — assign an item to a seat (at creation or at payment)."""
    item.shares.clear()
    item.is_shared = False
    item.seat_id = seat_id
    db.flush()


# --------------------------------------------------------------------------
# Taking payment
# --------------------------------------------------------------------------

class PaymentError(Exception):
    pass


def _taxes(db: Session, taxable_cents: int) -> tuple[int, int]:
    """(gst_cents, pst_cents) on a taxable amount at the configured rates.

    Both taxes apply to the same base — the item subtotal after discount, tip
    excluded — and are rounded to the cent independently, which is how a
    two-tax jurisdiction (e.g. GST + PST) prints them: separate lines, not a
    blended rate on a rounded intermediate.
    """
    cfg = settings_svc.tax_config(db)
    base = max(taxable_cents, 0)
    return pct(base, cfg.gst_rate), pct(base, cfg.pst_rate)


def is_card(instrument: PaymentInstrument) -> bool:
    """True for tenders that incur a card-processing cost."""
    return instrument.instrument_type in ("card", "contactless")


def _card_surcharge(instrument: PaymentInstrument, rate: float, base_cents: int) -> int:
    """The card surcharge on a base amount — zero unless it's a card tender and
    a rate is set (4.2.6). Kept in one place so seat and whole-order payments,
    and the pre-charge preview, all agree."""
    if rate and is_card(instrument):
        return pct(max(base_cents, 0), rate)
    return 0


def active_payments(order: Order) -> list[Payment]:
    """Payments that still count — voided ones are kept for the record only."""
    return [p for p in order.payments if not p.voided]


def _validate_instrument(order: Order, instrument: PaymentInstrument) -> None:
    """Section 4.2.1 — UberEats/DoorDash are delivery orders only."""
    if instrument.delivery_only and order.channel.channel_type != "delivery":
        raise PaymentError(
            f"{instrument.name} can only be used on delivery orders, "
            f"not on {order.channel.name}."
        )


def pay_seat(
    db: Session,
    order: Order,
    seat: Seat,
    instrument_id: int,
    staff_id: int,
    tip_cents: int = 0,
    service_charge_cents: int = 0,
    card_surcharge_rate: float = 0.0,
    item_ids: list[int] | None = None,
    discount_cents: int = 0,
    discount_approved_by_id: int | None = None,
    discount_reason: str = "",
    card_last4: str | None = None,
    card_brand: str | None = None,
    is_partial_close: bool = False,
) -> Payment:
    """Settle a seat, in whole or in part, with a single instrument.

    item_ids restricts payment to specific ticked items — this is the
    early-departure path in section 4.2.5, where unticked items stay on the
    open order. When restricted, the tip is charged proportionally to the
    items actually being paid.

    Writes one PaymentAllocation per item covered, which is the record that
    ties each item to this instrument (4.2.2).
    """
    instrument = db.get(PaymentInstrument, instrument_id)
    if instrument is None:
        raise PaymentError("Unknown payment instrument.")
    _validate_instrument(order, instrument)

    ledgers, _ = build_ledgers(db, order)
    ledger = ledgers.get(seat.id)
    if ledger is None:
        raise PaymentError(f"Seat {seat.seat_number} is not part of this order.")

    lines = [l for l in ledger.lines if l.outstanding_cents > 0]
    if item_ids is not None:
        wanted = set(item_ids)
        lines = [l for l in lines if l.item.id in wanted]

    if not lines:
        raise PaymentError(f"Seat {seat.seat_number} has nothing outstanding to pay.")

    items_cents = sum(l.outstanding_cents for l in lines)

    if discount_cents:
        if discount_approved_by_id is None:
            # Section 4.2.6 — discounts require manager approval.
            raise PaymentError("A discount requires manager approval.")
        discount_cents = min(discount_cents, items_cents)

    gst_cents, pst_cents = _taxes(db, items_cents - discount_cents)
    tax_cents = gst_cents + pst_cents
    # 4.2.6 — a card surcharge is charged only on card tenders, on the pre-tip
    # bill (items - discount + tax + service charge). Cash is never surcharged.
    card_surcharge_cents = _card_surcharge(
        instrument, card_surcharge_rate,
        items_cents - discount_cents + tax_cents + service_charge_cents,
    )
    total_cents = (items_cents - discount_cents + tax_cents + tip_cents
                   + service_charge_cents + card_surcharge_cents)
    if total_cents < 0:
        raise PaymentError("Payment total cannot be negative.")

    payment = Payment(
        order_id=order.id,
        seat_id=seat.id,
        instrument_id=instrument_id,
        staff_id=staff_id,
        items_cents=items_cents,
        tip_cents=tip_cents,
        service_charge_cents=service_charge_cents,
        card_surcharge_cents=card_surcharge_cents,
        discount_cents=discount_cents,
        tax_cents=tax_cents,
        total_cents=total_cents,
        # A card-present terminal reports the actual brand it read (Interac,
        # Visa…); fall back to the instrument's fixed brand for manual entry.
        card_brand=card_brand or instrument.card_brand,
        card_last4=card_last4 if instrument.instrument_type in ("card", "contactless") else None,
        is_partial_close=is_partial_close,
        created_at=datetime.now(),
    )
    db.add(payment)
    db.flush()

    for line in lines:
        db.add(
            PaymentAllocation(
                payment_id=payment.id,
                order_item_id=line.item.id,
                seat_id=seat.id,
                amount_cents=line.outstanding_cents,
            )
        )

    if discount_cents and discount_approved_by_id:
        db.add(
            Discount(
                order_id=order.id,
                seat_id=seat.id,
                kind="fixed",
                value=discount_cents,
                amount_cents=discount_cents,
                reason=discount_reason,
                approved_by_id=discount_approved_by_id,
            )
        )

    db.flush()
    db.refresh(order)

    # Re-derive seat status from what remains outstanding.
    ledgers_after, _ = build_ledgers(db, order)
    after = ledgers_after.get(seat.id)
    if after and after.outstanding_cents == 0:
        seat.status = SeatStatus.PAID
    else:
        # Section 4.2.5 — settled their portion, rest of the seat stays open.
        seat.status = SeatStatus.PAID_PARTIAL
    seat.tip_cents = 0

    _issue_receipt(db, order, seat, payment, is_partial_close)
    recompute_order_status(db, order)
    db.flush()
    return payment


def pay_whole_order(
    db: Session,
    order: Order,
    instrument_id: int,
    staff_id: int,
    tip_cents: int = 0,
    service_charge_cents: int = 0,
    card_surcharge_rate: float = 0.0,
    discount_cents: int = 0,
    discount_approved_by_id: int | None = None,
    card_last4: str | None = None,
) -> Payment:
    """Single-instrument settlement of everything outstanding on the order."""
    instrument = db.get(PaymentInstrument, instrument_id)
    if instrument is None:
        raise PaymentError("Unknown payment instrument.")
    _validate_instrument(order, instrument)

    outstanding: list[tuple[OrderItem, int]] = []
    for item in order.items:
        paid = sum(a.amount_cents for a in item.allocations)
        rest = item.line_total_cents - paid
        if rest > 0:
            outstanding.append((item, rest))

    if not outstanding:
        raise PaymentError("This order is already fully paid.")

    items_cents = sum(amt for _, amt in outstanding)
    if discount_cents:
        if discount_approved_by_id is None:
            raise PaymentError("A discount requires manager approval.")
        discount_cents = min(discount_cents, items_cents)

    tax_cents = sum(_taxes(db, items_cents - discount_cents))
    card_surcharge_cents = _card_surcharge(
        instrument, card_surcharge_rate,
        items_cents - discount_cents + tax_cents + service_charge_cents,
    )
    payment = Payment(
        order_id=order.id,
        seat_id=None,
        instrument_id=instrument_id,
        staff_id=staff_id,
        items_cents=items_cents,
        tip_cents=tip_cents,
        service_charge_cents=service_charge_cents,
        card_surcharge_cents=card_surcharge_cents,
        discount_cents=discount_cents,
        tax_cents=tax_cents,
        total_cents=items_cents - discount_cents + tax_cents + tip_cents
        + service_charge_cents + card_surcharge_cents,
        card_brand=instrument.card_brand,
        card_last4=card_last4 if instrument.instrument_type in ("card", "contactless") else None,
        created_at=datetime.now(),
    )
    db.add(payment)
    db.flush()

    for item, amount in outstanding:
        db.add(
            PaymentAllocation(
                payment_id=payment.id,
                order_item_id=item.id,
                seat_id=item.seat_id,
                amount_cents=amount,
            )
        )

    if discount_cents and discount_approved_by_id:
        db.add(
            Discount(
                order_id=order.id,
                kind="fixed",
                value=discount_cents,
                amount_cents=discount_cents,
                reason="Order discount",
                approved_by_id=discount_approved_by_id,
            )
        )

    db.flush()
    db.refresh(order)
    for seat in order.seats:
        seat.status = SeatStatus.PAID

    _issue_receipt(db, order, None, payment, False)
    recompute_order_status(db, order)
    db.flush()
    return payment


def tip_for_items(items_cents: int, percent: float) -> int:
    """Section 4.2.6 — tip as a percentage (15/18/20%) of the items being paid."""
    return pct(items_cents, percent)


def recompute_order_status(db: Session, order: Order) -> None:
    """Advance the order and free the table once everything is settled (6.1 step 10)."""
    total_value = sum(i.line_total_cents for i in order.items)
    total_paid = sum(
        a.amount_cents for p in order.payments for a in p.allocations
    )

    if total_value > 0 and total_paid >= total_value:
        order.status = OrderStatus.PAID
        order.closed_at = order.closed_at or datetime.now()
        if order.table:
            order.table.status = TableStatus.FREE
            order.table.current_waiter_id = None
    elif total_paid > 0:
        order.status = OrderStatus.PARTIALLY_PAID
    else:
        # Nothing paid — reached when a void removes the last payment. Fall back
        # to the order's kitchen progress so a reopened order is not stuck in a
        # payment state, and drop any close stamp.
        order.status = {
            KitchenStatus.PREPARING: OrderStatus.PREPARING,
            KitchenStatus.READY: OrderStatus.READY,
        }.get(order.kitchen_status, OrderStatus.OPEN)
        order.closed_at = None
    db.flush()


# --------------------------------------------------------------------------
# Receipts (4.2.7)
# --------------------------------------------------------------------------

def _tax_breakdown(db: Session, payment: Payment) -> dict:
    """GST/PST lines for the receipt, reconstructed from the payment's base.

    Recomputed rather than stored so the payment table keeps a single tax
    figure. Any rounding remainder between (gst+pst) and the stored tax_cents
    is folded into GST so the two lines always sum to what was charged.
    """
    cfg = settings_svc.tax_config(db)
    base = payment.items_cents - payment.discount_cents
    pst = pct(max(base, 0), cfg.pst_rate)
    gst = payment.tax_cents - pst           # keeps gst + pst == tax_cents exactly
    return {
        "tax": money(payment.tax_cents),
        "gst": money(gst),
        "gst_rate": cfg.gst_rate,
        "gst_number": cfg.gst_number,
        "pst": money(pst),
        "pst_rate": cfg.pst_rate,
        "pst_number": cfg.pst_number,
        "has_pst": cfg.pst_rate > 0,
    }


def void_payment(db: Session, payment: Payment, staff_id: int, reason: str) -> None:
    """Reverse a payment (section 4.2, manager action). Kept, not deleted.

    The allocations are removed — which returns the items to outstanding and
    drops the payment out of every allocation-based total — and the payment is
    flagged voided with who and when. Its receipt is withdrawn. Seat statuses
    and the order/table are then re-derived from what remains.
    """
    if payment.voided:
        raise PaymentError("This payment is already voided.")

    order = payment.order
    for alloc in list(payment.allocations):
        db.delete(alloc)
    receipt = db.execute(
        select(Receipt).where(Receipt.payment_id == payment.id)
    ).scalars().first()
    if receipt is not None:
        db.delete(receipt)

    payment.voided = True
    payment.voided_at = datetime.now()
    payment.voided_by_id = staff_id
    payment.void_reason = reason.strip()
    db.flush()
    db.refresh(order)

    # Re-derive seat statuses from what is now outstanding.
    ledgers_after, _ = build_ledgers(db, order)
    for seat in order.seats:
        led = ledgers_after.get(seat.id)
        if led is None:
            continue
        paid = led.subtotal_cents - led.outstanding_cents
        if led.outstanding_cents == 0 and led.subtotal_cents > 0:
            seat.status = SeatStatus.PAID
        elif paid > 0:
            seat.status = SeatStatus.PAID_PARTIAL
        else:
            seat.status = SeatStatus.OPEN

    recompute_order_status(db, order)
    db.flush()


def _issue_receipt(
    db: Session, order: Order, seat: Seat | None, payment: Payment, partial: bool
) -> Receipt:
    """Section 4.2.7 — receipt shows items, subtotal, tip, discount, instrument."""
    lines = []
    for alloc in payment.allocations:
        item = alloc.order_item
        extras = [o.label for o in item.options] + [m.modifier.name for m in item.modifiers]
        lines.append(
            {
                "item": item.menu_item.name,
                "qty": item.quantity,
                "amount": money(alloc.amount_cents),
                "shared": item.is_shared,
                "options": extras,
            }
        )

    payload = {
        "order_code": order.code,
        "payment_id": payment.id,
        "seat": seat.seat_number if seat else None,
        "table": order.table.number if order.table else None,
        "issued_at": datetime.now().isoformat(timespec="seconds"),
        "lines": lines,
        # Field 5 on the printed chit: the guest counts the tray against it.
        "item_count": sum(alloc.order_item.quantity for alloc in payment.allocations),
        "subtotal": money(payment.items_cents),
        "discount": money(payment.discount_cents),
        # GST/PST split for display, recomputed from the same base and rates.
        # The stored payment carries only the combined tax_cents; the breakdown
        # is a receipt concern, so it lives here.
        **_tax_breakdown(db, payment),
        "service_charge": money(payment.service_charge_cents),
        "service_charge_cents": payment.service_charge_cents,
        "card_surcharge": money(payment.card_surcharge_cents),
        "card_surcharge_cents": payment.card_surcharge_cents,
        "tip": money(payment.tip_cents),
        "total": money(payment.total_cents),
        "total_cents": payment.total_cents,
        "instrument": payment.instrument.name,
        "card_last4": payment.card_last4,
        "served_by": payment.staff.name if payment.staff else "",
        "partial_close": partial,
    }

    receipt = Receipt(
        order_id=order.id,
        seat_id=seat.id if seat else None,
        payment_id=payment.id,
        kind="partial" if partial else ("seat" if seat else "full"),
        delivery_method="print",
        payload_json=json.dumps(payload, indent=2),
    )
    db.add(receipt)
    db.flush()
    return receipt
