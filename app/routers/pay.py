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
    PaymentInstrument,
    Receipt,
    Role,
    Seat,
    Staff,
)
from app.services.money import GST_NUMBER, GST_RATE, money, pct
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
)

router = APIRouter()


def _load(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


@router.get("/orders/{order_id}/pay")
def payment_screen(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """4.2.4 — per-seat totals and the live balance panel."""
    order = _load(db, order_id)
    ledgers, unassigned = build_ledgers(db, order)
    panel = balance_panel(db, order)

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

    return render(request, "pay.html", {
        "db": db, "staff": staff, "order": order,
        "ledgers": [ledgers[s.id] for s in order.seats if s.id in ledgers],
        "unassigned": unassigned, "panel": panel, "instruments": usable,
        "managers": managers, "gst_rate": GST_RATE,
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

    # 4.2.6 — tip as percentage or custom amount.
    if tip_mode == "custom":
        try:
            tip_cents = int(round(float(tip_custom or 0) * 100))
        except ValueError:
            raise HTTPException(400, "Invalid custom tip amount.")
    elif tip_mode in ("15", "18", "20"):
        # 4.2.5 — proportional to the items actually being paid.
        tip_cents = pct(base, int(tip_mode))
    else:
        tip_cents = 0

    discount_cents = pct(base, discount_pct) if discount_pct else 0
    if discount_cents and not can(staff, "discount.approve") and not approved_by_id:
        raise HTTPException(403, "A discount requires manager approval.")

    try:
        pay_seat(
            db, order, seat,
            instrument_id=instrument_id,
            staff_id=staff.id,
            tip_cents=max(0, tip_cents),
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
    return RedirectResponse(f"/orders/{order_id}/pay", status_code=303)


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
    if tip_mode == "custom":
        try:
            tip_cents = int(round(float(tip_custom or 0) * 100))
        except ValueError:
            raise HTTPException(400, "Invalid custom tip amount.")
    elif tip_mode in ("15", "18", "20"):
        tip_cents = pct(outstanding, int(tip_mode))
    else:
        tip_cents = 0

    discount_cents = pct(outstanding, discount_pct) if discount_pct else 0
    try:
        pay_whole_order(
            db, order,
            instrument_id=instrument_id,
            staff_id=staff.id,
            tip_cents=max(0, tip_cents),
            discount_cents=discount_cents,
            discount_approved_by_id=(
                approved_by_id or (staff.id if can(staff, "discount.approve") else None)
            ),
            card_last4=(card_last4.strip()[-4:] or None) if card_last4.strip() else None,
        )
    except PaymentError as e:
        raise HTTPException(400, str(e))

    db.commit()
    return RedirectResponse(f"/orders/{order_id}/pay", status_code=303)


@router.get("/payments/{payment_id}/receipt", response_class=HTMLResponse)
def view_receipt(
    payment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(current_staff),
):
    """4.2.7 — receipt with items, subtotal, tip, discount, instrument.

    Keyed by payment: a receipt is issued per tender, so the seat that paid
    early (4.2.5) gets its own document immediately.
    """
    receipt = db.execute(
        select(Receipt).where(Receipt.payment_id == payment_id)
    ).scalars().first()
    if receipt is None:
        raise HTTPException(404, "No receipt was issued for that payment.")
    data = json.loads(receipt.payload_json)

    # Tip guide is figured on the pre-tax subtotal (the convention), parsed back
    # from the stored display string so the receipt stays a pure render of the
    # saved payload.
    subtotal_cents = round(float(data["subtotal"].replace(",", "")) * 100)
    tip_guide = {
        "p20": money(pct(subtotal_cents, 20)),
        "p18": money(pct(subtotal_cents, 18)),
        "p15": money(pct(subtotal_cents, 15)),
    }
    return render(request, "receipt.html", {
        "db": db, "staff": staff, "receipt": receipt, "data": data,
        "tip_guide": tip_guide, "gst_number": GST_NUMBER, "title": "Receipt",
    })
