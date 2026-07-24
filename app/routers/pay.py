"""Payment Processing routes — section 4.2."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import can, current_staff, render, require
from app.models.oltp import (
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentInstrument,
    Receipt,
    ReceiptDelivery,
    Role,
    Seat,
    Staff,
)
from app.services import receipt_delivery
from app.services import refunds
from app.services import settings as settings_svc
from app.services.money import money, pct
from app.services.payments import (
    PaymentError,
    assign_item_to_seat,
    balance_panel,
    build_ledgers,
    ensure_seats,
    pay_seat,
    pay_whole_order,
    set_shared_item_shares,
    split_equally,
    void_payment,
)

router = APIRouter()


def _load(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


def _receipt_ctx(db: Session, payment_id: int) -> dict | None:
    """Everything a receipt render needs (chit + tip guide + deliveries), or
    None if no receipt was issued. Shared by the standalone page and the modal.
    """
    receipt = db.execute(
        select(Receipt).where(Receipt.payment_id == payment_id)
    ).scalars().first()
    if receipt is None:
        return None
    data = json.loads(receipt.payload_json)
    subtotal_cents = round(float(data["subtotal"].replace(",", "")) * 100)
    cfg = settings_svc.all_settings(db)
    deliveries = db.execute(
        select(ReceiptDelivery).where(ReceiptDelivery.receipt_id == receipt.id)
        .order_by(ReceiptDelivery.sent_at.desc())
    ).scalars().all()
    return {
        "receipt": receipt, "data": data, "deliveries": deliveries,
        "tip_guide": {
            "p20": money(pct(subtotal_cents, 20)),
            "p18": money(pct(subtotal_cents, 18)),
            "p15": money(pct(subtotal_cents, 15)),
        },
        "biz": {"name": cfg["biz_name"], "addr": cfg["biz_address"],
                "postal": cfg["biz_postal"], "phone": cfg["biz_phone"]},
    }


def _tip_for(db: Session, tip_mode: str, tip_custom: str, base: int) -> int:
    """Resolve the tip in cents from the chosen mode (4.2.6).

    'auto' applies the configured auto-gratuity rate for large parties; the
    percentage modes and a custom dollar amount work as before. All are
    proportional to the base actually being paid.
    """
    if tip_mode == "custom":
        try:
            return max(0, int(round(float(tip_custom or 0) * 100)))
        except ValueError:
            raise HTTPException(400, "Invalid custom tip amount.")
    if tip_mode == "auto":
        return pct(base, settings_svc.gratuity_config(db).rate)
    if tip_mode in ("15", "18", "20"):
        return pct(base, int(tip_mode))
    return 0


@router.get("/orders/{order_id}/pay")
def payment_screen(
    order_id: int,
    request: Request,
    receipt: int | None = None,
    sent: str = "",
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """4.2.4 — per-seat totals and the live balance panel."""
    order = _load(db, order_id)
    ledgers, unassigned = build_ledgers(db, order)
    panel = balance_panel(db, order)

    # ?receipt=N pops the just-issued receipt as a modal over this screen.
    receipt_view = None
    if receipt:
        rc = _receipt_ctx(db, receipt)
        if rc is not None:
            receipt_view = {**rc, "sent": sent}

    instruments = db.execute(
        select(PaymentInstrument).order_by(PaymentInstrument.id)
    ).scalars().all()
    # 4.2.1 — platform instruments only apply to delivery orders.
    usable = [
        i for i in instruments
        if not i.delivery_only or order.channel.channel_type == "delivery"
    ]

    managers = db.execute(
        select(Staff).where(Staff.role.in_((Role.OWNER, Role.MANAGER)))
    ).scalars().all()

    # Refunds apply once the order is settled; expose how much each payment has
    # left to refund so the template can offer the control.
    settled = order.status in (OrderStatus.PAID, OrderStatus.CLOSED)
    refundable = {p.id: refunds.refundable_cents(db, p) for p in order.payments}
    refunded = {p.id: refunds.refunded_so_far(db, p.id) for p in order.payments}

    return render(request, "pay.html", {
        "db": db, "staff": staff, "order": order,
        "ledgers": [ledgers[s.id] for s in order.seats if s.id in ledgers],
        "unassigned": unassigned, "panel": panel, "instruments": usable,
        "managers": managers,
        "settled": settled, "refundable": refundable, "refunded": refunded,
        "tax_cfg": settings_svc.tax_config(db),
        # 4.2.6 — auto-gratuity for large parties; the template pre-selects it.
        "gratuity": settings_svc.gratuity_config(db),
        "auto_gratuity": settings_svc.gratuity_config(db).applies(order.guest_count),
        # 4.2.6 — mandatory service charge rate (0 = none), shown on the screen.
        "service_charge_rate": settings_svc.service_charge_rate(db),
        "receipt_view": receipt_view,
        "title": f"Payment · {order.code}",
    })


@router.post("/orders/{order_id}/seats")
def set_seats(
    order_id: int,
    guests: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    order = _load(db, order_id)
    ensure_seats(db, order, max(1, guests))
    db.commit()
    return RedirectResponse(f"/orders/{order_id}/pay", status_code=303)


@router.post("/orders/{order_id}/split-equally")
def do_split_equally(
    order_id: int,
    guests: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """4.2.3 — split the order equally among N guests."""
    order = _load(db, order_id)
    if any(p.allocations for p in order.payments):
        raise HTTPException(400, "Cannot re-split: part of this order is already paid.")
    split_equally(db, order, max(1, guests))
    db.commit()
    return RedirectResponse(f"/orders/{order_id}/pay", status_code=303)


@router.post("/orders/{order_id}/reset-split")
def reset_split(
    order_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """Undo a split (4.2.3): un-share every item back to unassigned.

    People change their mind — after an equal split they may want to pay
    together or divide it differently. This clears the shares so items can be
    reassigned or paid as a whole. Blocked once any payment has landed, since
    that money is already tied to the current split.
    """
    order = _load(db, order_id)
    if any(p.allocations for p in order.payments):
        raise HTTPException(
            400,
            "Part of this order is already paid — void the payment before "
            "changing the split.",
        )
    for item in order.items:
        assign_item_to_seat(db, item, None)   # un-share, back to unassigned
    db.commit()
    return RedirectResponse(f"/orders/{order_id}/pay", status_code=303)


@router.post("/orders/{order_id}/items/{item_id}/assign")
def assign_seat(
    order_id: int,
    item_id: int,
    seat_number: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """4.2.4 — assign an item to a seat at payment time."""
    order = _load(db, order_id)
    item = db.get(OrderItem, item_id)
    if item is None or item.order_id != order.id:
        raise HTTPException(404, "Item not found")
    if item.allocations:
        raise HTTPException(400, "This item has already been paid for.")

    seat = db.execute(
        select(Seat).where(Seat.order_id == order.id, Seat.seat_number == seat_number)
    ).scalar_one_or_none()
    if seat is None:
        raise HTTPException(404, f"Seat {seat_number} not found on this order.")
    assign_item_to_seat(db, item, seat.id)
    db.commit()
    return RedirectResponse(f"/orders/{order_id}/pay", status_code=303)


@router.post("/orders/{order_id}/items/{item_id}/share")
def share_item(
    order_id: int,
    item_id: int,
    seat_numbers: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """4.2.4 — split a shared item proportionally across selected seats."""
    order = _load(db, order_id)
    item = db.get(OrderItem, item_id)
    if item is None or item.order_id != order.id:
        raise HTTPException(404, "Item not found")
    if item.allocations:
        raise HTTPException(400, "This item has already been paid for.")
    if not seat_numbers:
        raise HTTPException(400, "Select at least one seat to share across.")

    seats = db.execute(
        select(Seat).where(Seat.order_id == order.id, Seat.seat_number.in_(seat_numbers))
    ).scalars().all()
    set_shared_item_shares(db, item, [s.id for s in seats])
    db.commit()
    return RedirectResponse(f"/orders/{order_id}/pay", status_code=303)


@router.post("/orders/{order_id}/seats/{seat_id}/pay")
def take_seat_payment(
    order_id: int,
    seat_id: int,
    instrument_id: int = Form(...),
    tip_mode: str = Form("none"),
    tip_custom: str = Form("0"),
    item_ids: list[int] = Form(default=[]),
    partial: str = Form(""),
    discount_pct: int = Form(0),
    approved_by_id: int = Form(None),
    card_last4: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """4.2.4 / 4.2.5 — settle a seat, optionally only the ticked items."""
    order = _load(db, order_id)
    seat = db.get(Seat, seat_id)
    if seat is None or seat.order_id != order.id:
        raise HTTPException(404, "Seat not found")

    ledgers, _ = build_ledgers(db, order)
    ledger = ledgers.get(seat.id)
    if ledger is None:
        raise HTTPException(404, "Seat has no ledger")

    selected = [i for i in item_ids if i] or None
    if selected:
        base = sum(
            l.outstanding_cents for l in ledger.lines if l.item.id in set(selected)
        )
    else:
        base = ledger.outstanding_cents

    # 4.2.6 — tip as a percentage, auto-gratuity, or a custom amount,
    # proportional to the items actually being paid (4.2.5).
    tip_cents = _tip_for(db, tip_mode, tip_custom, base)

    discount_cents = pct(base, discount_pct) if discount_pct else 0
    # 4.2.6 — mandatory service charge on the net items being paid.
    service_charge_cents = pct(base - discount_cents, settings_svc.service_charge_rate(db))
    if discount_cents and not can(staff, "discount.approve") and not approved_by_id:
        raise HTTPException(403, "A discount requires manager approval.")

    try:
        payment = pay_seat(
            db, order, seat,
            instrument_id=instrument_id,
            staff_id=staff.id,
            tip_cents=max(0, tip_cents),
            service_charge_cents=max(0, service_charge_cents),
            item_ids=selected,
            discount_cents=discount_cents,
            discount_approved_by_id=(
                approved_by_id or (staff.id if can(staff, "discount.approve") else None)
            ),
            discount_reason=f"{discount_pct}% discount",
            card_last4=(card_last4.strip()[-4:] or None) if card_last4.strip() else None,
            is_partial_close=bool(partial),
        )
    except PaymentError as e:
        raise HTTPException(400, str(e))

    db.commit()
    # Pop the receipt as a modal over the pay screen (closing it returns here
    # for the next seat), rather than a full-page redirect away.
    return RedirectResponse(
        f"/orders/{order.id}/pay?receipt={payment.id}", status_code=303
    )


@router.post("/orders/{order_id}/pay-all")
def take_full_payment(
    order_id: int,
    instrument_id: int = Form(...),
    tip_mode: str = Form("none"),
    tip_custom: str = Form("0"),
    discount_pct: int = Form(0),
    approved_by_id: int = Form(None),
    card_last4: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """4.2.2 — single-instrument settlement of the whole order."""
    order = _load(db, order_id)
    outstanding = sum(
        i.line_total_cents - sum(a.amount_cents for a in i.allocations)
        for i in order.items
    )
    tip_cents = _tip_for(db, tip_mode, tip_custom, outstanding)

    discount_cents = pct(outstanding, discount_pct) if discount_pct else 0
    service_charge_cents = pct(outstanding - discount_cents, settings_svc.service_charge_rate(db))
    try:
        payment = pay_whole_order(
            db, order,
            instrument_id=instrument_id,
            staff_id=staff.id,
            tip_cents=max(0, tip_cents),
            service_charge_cents=max(0, service_charge_cents),
            discount_cents=discount_cents,
            discount_approved_by_id=(
                approved_by_id or (staff.id if can(staff, "discount.approve") else None)
            ),
            card_last4=(card_last4.strip()[-4:] or None) if card_last4.strip() else None,
        )
    except PaymentError as e:
        raise HTTPException(400, str(e))

    db.commit()
    return RedirectResponse(
        f"/orders/{order.id}/pay?receipt={payment.id}", status_code=303
    )


@router.post("/payments/{payment_id}/void")
def void_a_payment(
    payment_id: int,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("discount.approve")),
):
    """Reverse a payment (manager action). Kept as a record, not deleted.

    Guarded on the order not being settled: voiding after the table has been
    freed and possibly re-seated cannot cleanly reopen it, so that path is a
    refund question rather than a void.
    """
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(404, "Payment not found")
    if payment.voided:
        raise HTTPException(400, "This payment is already voided.")
    order = payment.order
    if order.status in (OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED):
        raise HTTPException(
            400,
            "This order is already settled — a paid-out payment cannot be voided "
            "from here.",
        )
    try:
        void_payment(db, payment, staff_id=staff.id, reason=reason)
    except PaymentError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return RedirectResponse(f"/orders/{order.id}/pay", status_code=303)


@router.post("/payments/{payment_id}/refund")
def refund_a_payment(
    payment_id: int,
    amount: str = Form(...),
    reason: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("discount.approve")),
):
    """4.2 — refund a settled payment (manager). Kept as a reversing record."""
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(404, "Payment not found")
    try:
        cents = round(float(amount.replace(",", "").replace("$", "").strip()) * 100)
    except ValueError:
        raise HTTPException(400, f"'{amount}' is not a valid amount.")
    try:
        refunds.refund_payment(db, payment, cents, staff, reason)
    except refunds.RefundError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/orders/{payment.order_id}/pay", status_code=303)


@router.get("/payments/{payment_id}/receipt", response_class=HTMLResponse)
def view_receipt(
    payment_id: int,
    request: Request,
    sent: str = "",
    db: Session = Depends(get_db),
    staff: Staff = Depends(current_staff),
):
    """4.2.7 — receipt with items, subtotal, tip, discount, instrument.

    Keyed by payment: a receipt is issued per tender, so the seat that paid
    early (4.2.5) gets its own document immediately.
    """
    ctx = _receipt_ctx(db, payment_id)
    if ctx is None:
        raise HTTPException(404, "No receipt was issued for that payment.")
    return render(request, "receipt.html", {
        "db": db, "staff": staff, "sent": sent, "title": "Receipt", **ctx,
    })


@router.post("/payments/{payment_id}/receipt/send")
def send_receipt(
    payment_id: int,
    method: str = Form(...),
    destination: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(current_staff),
):
    """4.2.7 — email or text the receipt to the guest."""
    receipt = db.execute(
        select(Receipt).where(Receipt.payment_id == payment_id)
    ).scalars().first()
    if receipt is None:
        raise HTTPException(404, "No receipt was issued for that payment.")
    try:
        receipt_delivery.send(db, receipt, method, destination, staff)
    except receipt_delivery.DeliveryError as e:
        raise HTTPException(400, str(e))
    # Stay in the receipt modal over the pay screen.
    return RedirectResponse(
        f"/orders/{receipt.order_id}/pay?receipt={payment_id}&sent={method.lower()}",
        status_code=303,
    )
