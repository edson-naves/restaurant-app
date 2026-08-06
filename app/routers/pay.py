"""Payment Processing routes — section 4.2."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import can, current_staff, render, require
from app.models.oltp import (
    KitchenStatus,
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
from app.services import square
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


def order_has_served(order: Order) -> bool:
    """True once anything on the order has been served — i.e. there's delivered
    food to pay for. Lets a guest at a served seat settle up while the rest of
    the table's food is still cooking."""
    return (
        order.status in (OrderStatus.SERVED, OrderStatus.PARTIALLY_PAID)
        or any(i.kitchen_status == KitchenStatus.SERVED for i in order.items)
    )


def _require_served(order: Order) -> None:
    """Payment needs something served first — you can't pay for food that hasn't
    been delivered. A served seat can be settled even while the rest cooks."""
    if not order_has_served(order):
        raise HTTPException(
            400, "Serve an item before taking payment."
        )


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


def _discount_approver_id(
    db: Session, staff: Staff, approved_by_id: int | None, has_discount: bool
) -> int | None:
    """Section 4.2.6 — resolve (and authorize) who approved a discount.

    A manager/owner self-approves. Otherwise ``approved_by_id`` must name an
    active staff member who actually holds ``discount.approve`` — a waiter can't
    grant themselves a discount by putting any id in the form. Enforced here on
    the server because the discount UI is only rendered for managers, so a raw
    POST is the only path an unapproved discount could take. Returns the approver
    id, or None when there is no discount.
    """
    if not has_discount:
        return None
    if can(staff, "discount.approve"):
        return staff.id
    approver = db.get(Staff, approved_by_id) if approved_by_id else None
    if approver is not None and approver.is_active and can(approver, "discount.approve"):
        return approver.id
    raise HTTPException(403, "A discount requires approval by a manager.")


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
    # When a card terminal is available, cards go through it (💳 Card) and the
    # manual form becomes cash-first (💵 Cash), with card tenders demoted to a
    # "terminal down" fallback. Split so the template can group them.
    cash_instruments = [i for i in usable if i.instrument_type not in ("card", "contactless")]
    card_instruments = [i for i in usable if i.instrument_type in ("card", "contactless")]

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
        "cash_instruments": cash_instruments, "card_instruments": card_instruments,
        "managers": managers,
        "settled": settled, "refundable": refundable, "refunded": refunded,
        "tax_cfg": settings_svc.tax_config(db),
        # 4.2.6 — auto-gratuity for large parties; the template pre-selects it.
        "gratuity": settings_svc.gratuity_config(db),
        "auto_gratuity": settings_svc.gratuity_config(db).applies(order.guest_count),
        # 4.2.6 — mandatory service charge rate (0 = none), shown on the screen.
        "service_charge_rate": settings_svc.service_charge_rate(db),
        # 4.2.6 — optional card surcharge rate (0 = none); previewed on screen.
        "card_surcharge_rate": settings_svc.card_surcharge_rate(db),
        "receipt_view": receipt_view,
        # Card-terminal path is offered only when Square is configured.
        "terminal_enabled": square.is_configured(),
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
    _require_served(order)
    seat = db.get(Seat, seat_id)
    if seat is None or seat.order_id != order.id:
        raise HTTPException(404, "Seat not found")

    ledgers, _ = build_ledgers(db, order)
    ledger = ledgers.get(seat.id)
    if ledger is None:
        raise HTTPException(404, "Seat has no ledger")

    # Only served items can be paid — food not yet delivered stays on the order.
    served_ids = {i.id for i in order.items if i.kitchen_status == KitchenStatus.SERVED}
    if item_ids:
        selected = [i for i in item_ids if i in served_ids]
    else:
        selected = [
            l.item.id for l in ledger.lines
            if l.item.id in served_ids and l.outstanding_cents > 0
        ]
    if not selected:
        raise HTTPException(400, "No served items to pay on this seat yet.")
    base = sum(l.outstanding_cents for l in ledger.lines if l.item.id in set(selected))

    # 4.2.6 — tip as a percentage, auto-gratuity, or a custom amount,
    # proportional to the items actually being paid (4.2.5).
    tip_cents = _tip_for(db, tip_mode, tip_custom, base)

    discount_cents = pct(base, discount_pct) if discount_pct else 0
    # 4.2.6 — mandatory service charge on the net items being paid.
    service_charge_cents = pct(base - discount_cents, settings_svc.service_charge_rate(db))
    approver_id = _discount_approver_id(db, staff, approved_by_id, discount_cents > 0)

    try:
        payment = pay_seat(
            db, order, seat,
            instrument_id=instrument_id,
            staff_id=staff.id,
            tip_cents=max(0, tip_cents),
            service_charge_cents=max(0, service_charge_cents),
            card_surcharge_rate=settings_svc.card_surcharge_rate(db),
            item_ids=selected,
            discount_cents=discount_cents,
            discount_approved_by_id=approver_id,
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


# --------------------------------------------------------------------------
# Card-terminal payment (Square Terminal API)
#
# The physical terminal — not this app — takes the card and the tip. We send it
# the amount owed, the customer taps and tips on the machine, and we read the
# result back (real tip + card last-4). The waiter never types the tip.
#
# The flow is three routes: create the checkout, a waiting page that polls, and
# a status endpoint that records the payment once the terminal completes.
# --------------------------------------------------------------------------

def _terminal_instrument(db: Session) -> PaymentInstrument:
    """The instrument card-terminal payments are booked under (created once)."""
    inst = db.execute(
        select(PaymentInstrument).where(PaymentInstrument.name == "Card (terminal)")
    ).scalars().first()
    if inst is None:
        inst = PaymentInstrument(
            code="card_terminal", name="Card (terminal)", instrument_type="card"
        )
        db.add(inst)
        db.flush()
    return inst


@router.post("/orders/{order_id}/seats/{seat_id}/pay-terminal")
def start_terminal_payment(
    order_id: int,
    seat_id: int,
    item_ids: list[int] = Form(default=[]),
    partial: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """Send the seat's served balance to the card terminal for the customer to
    pay + tip on. Returns a waiting page that polls for the result."""
    if not square.is_configured():
        raise HTTPException(400, "No card terminal is set up.")
    order = _load(db, order_id)
    _require_served(order)
    seat = db.get(Seat, seat_id)
    if seat is None or seat.order_id != order.id:
        raise HTTPException(404, "Seat not found")

    ledgers, _ = build_ledgers(db, order)
    ledger = ledgers.get(seat.id)
    if ledger is None:
        raise HTTPException(404, "Seat has no ledger")

    served_ids = {i.id for i in order.items if i.kitchen_status == KitchenStatus.SERVED}
    wanted = set(item_ids) if item_ids else None
    lines = [
        l for l in ledger.lines
        if l.item.id in served_ids and l.outstanding_cents > 0
        and (wanted is None or l.item.id in wanted)
    ]
    if not lines:
        raise HTTPException(400, "No served items to charge on this seat yet.")

    base = sum(l.outstanding_cents for l in lines)
    svc_cents = pct(base, settings_svc.service_charge_rate(db))
    cfg = settings_svc.tax_config(db)
    tax_cents = pct(base, cfg.gst_rate) + pct(base, cfg.pst_rate)
    # A terminal payment is a card tender, so the card surcharge applies (on the
    # pre-tip bill). The terminal adds the customer's tip on top of all this.
    surcharge_cents = pct(base + tax_cents + svc_cents, settings_svc.card_surcharge_rate(db))
    amount = base + tax_cents + svc_cents + surcharge_cents

    selected = [l.item.id for l in lines]
    table = order.table.number if order.table else "—"
    try:
        checkout = square.create_checkout(
            amount,
            reference_id=f"O{order.id}S{seat.id}",
            note=f"Table {table} · Seat {seat.seat_number} · {order.code}",
        )
    except square.SquareError as e:
        raise HTTPException(502, f"Card terminal error: {e}")

    items_q = ",".join(str(i) for i in selected)
    url = (
        f"/orders/{order.id}/seats/{seat.id}/terminal/{checkout['id']}"
        f"?items={items_q}&partial={'1' if partial else ''}"
    )
    return RedirectResponse(url, status_code=303)


@router.get("/orders/{order_id}/seats/{seat_id}/terminal/{checkout_id}")
def terminal_wait(
    order_id: int,
    seat_id: int,
    checkout_id: str,
    request: Request,
    items: str = "",
    partial: str = "",
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """The 'Waiting for the terminal…' page. It polls the status endpoint."""
    order = _load(db, order_id)
    seat = db.get(Seat, seat_id)
    if seat is None or seat.order_id != order.id:
        raise HTTPException(404, "Seat not found")
    return render(request, "terminal_wait.html", {
        "db": db, "staff": staff, "order": order, "seat": seat,
        "checkout_id": checkout_id, "items": items, "partial": partial,
        "title": f"Terminal · Seat {seat.seat_number}",
    })


@router.get("/orders/{order_id}/seats/{seat_id}/terminal/{checkout_id}/status")
def terminal_status(
    order_id: int,
    seat_id: int,
    checkout_id: str,
    items: str = "",
    partial: str = "",
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """Polled by the waiting page. Reports the terminal's progress and, on
    completion, records the payment with the real tip and card last-4.

    Idempotent: once the seat's chosen items are settled, a repeat poll finds
    nothing outstanding and simply reports done — no second payment.
    """
    order = _load(db, order_id)
    seat = db.get(Seat, seat_id)
    if seat is None or seat.order_id != order.id:
        raise HTTPException(404, "Seat not found")

    try:
        checkout = square.get_checkout(checkout_id)
    except square.SquareError as e:
        return JSONResponse({"state": "error", "message": str(e)})

    status = checkout.get("status")
    if status == square.CANCELED:
        return JSONResponse({"state": "canceled"})
    if status != square.COMPLETED:
        return JSONResponse({"state": "pending", "status": status})

    # Completed on the machine — settle the chosen served items on our side.
    wanted = {int(x) for x in items.split(",") if x.strip().isdigit()}
    served_ids = {i.id for i in order.items if i.kitchen_status == KitchenStatus.SERVED}
    payable = [
        i.id for i in order.items
        if i.id in served_ids and i.id in wanted
        and i.line_total_cents - sum(a.amount_cents for a in i.allocations) > 0
    ]
    if not payable:
        # Already recorded by an earlier poll — find that receipt to hand back.
        pay = _latest_terminal_payment(order, seat)
        rid = pay.id if pay else ""
        return JSONResponse({"state": "done", "receipt_url":
                             f"/orders/{order.id}/pay?receipt={rid}" if rid
                             else f"/orders/{order.id}/pay"})

    tip_cents, brand, last4 = square.tip_and_card(checkout)
    svc_cents = pct(
        sum(i.line_total_cents - sum(a.amount_cents for a in i.allocations)
            for i in order.items if i.id in set(payable)),
        settings_svc.service_charge_rate(db),
    )
    inst = _terminal_instrument(db)
    try:
        payment = pay_seat(
            db, order, seat,
            instrument_id=inst.id,
            staff_id=staff.id,
            tip_cents=tip_cents,
            service_charge_cents=svc_cents,
            card_surcharge_rate=settings_svc.card_surcharge_rate(db),
            item_ids=payable,
            card_last4=last4,
            card_brand=brand,
            is_partial_close=bool(partial),
        )
    except PaymentError as e:
        return JSONResponse({"state": "error", "message": str(e)})
    db.commit()
    return JSONResponse({
        "state": "done",
        "tip_cents": tip_cents,
        "receipt_url": f"/orders/{order.id}/pay?receipt={payment.id}",
    })


def _latest_terminal_payment(order: Order, seat: Seat) -> Payment | None:
    """The most recent live card-terminal payment on a seat (for the receipt
    link when a completed checkout was already recorded)."""
    cand = [
        p for p in order.payments
        if not p.voided and p.seat_id == seat.id
        and p.instrument and p.instrument.name == "Card (terminal)"
    ]
    return max(cand, key=lambda p: p.id) if cand else None


@router.post("/orders/{order_id}/seats/{seat_id}/terminal/{checkout_id}/cancel")
def terminal_cancel(
    order_id: int,
    seat_id: int,
    checkout_id: str,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("payments.take")),
):
    """Cancel a still-pending terminal checkout and return to the pay screen."""
    try:
        square.cancel_checkout(checkout_id)
    except square.SquareError:
        pass  # already terminal (completed/canceled) — nothing to undo
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
    _require_served(order)
    # Paying the whole order at once only makes sense when everything's been
    # served — otherwise you'd be charging for undelivered food. Un-served items
    # must be settled seat by seat (once they're up) instead.
    if any(
        i.kitchen_status != KitchenStatus.SERVED
        and i.line_total_cents - sum(a.amount_cents for a in i.allocations) > 0
        for i in order.items
    ):
        raise HTTPException(
            400,
            "Some items aren't served yet — settle those seats once they're up, "
            "or pay this seat by seat.",
        )
    outstanding = sum(
        i.line_total_cents - sum(a.amount_cents for a in i.allocations)
        for i in order.items
    )
    tip_cents = _tip_for(db, tip_mode, tip_custom, outstanding)

    discount_cents = pct(outstanding, discount_pct) if discount_pct else 0
    service_charge_cents = pct(outstanding - discount_cents, settings_svc.service_charge_rate(db))
    approver_id = _discount_approver_id(db, staff, approved_by_id, discount_cents > 0)
    try:
        payment = pay_whole_order(
            db, order,
            instrument_id=instrument_id,
            staff_id=staff.id,
            tip_cents=max(0, tip_cents),
            service_charge_cents=max(0, service_charge_cents),
            card_surcharge_rate=settings_svc.card_surcharge_rate(db),
            discount_cents=discount_cents,
            discount_approved_by_id=approver_id,
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
