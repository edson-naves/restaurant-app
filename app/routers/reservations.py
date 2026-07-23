"""Reservations & waitlist — section 4.1.5 (front of house)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import render, require
from app.models.oltp import (
    Reservation,
    ReservationStatus,
    RestaurantTable,
    Staff,
    TableStatus,
)
from app.routers.sales import open_order_on_table

router = APIRouter()


def _waiting(db: Session, kind: str) -> list[Reservation]:
    return db.execute(
        select(Reservation).where(
            Reservation.kind == kind,
            Reservation.status == ReservationStatus.WAITING,
        ).order_by(Reservation.at)
    ).scalars().all()


@router.get("/reservations")
def reservations_page(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reservations")),
):
    """Upcoming bookings and the current walk-in waitlist."""
    free_tables = db.execute(
        select(RestaurantTable).where(
            RestaurantTable.is_active.is_(True),
            RestaurantTable.status == TableStatus.FREE,
        ).order_by(RestaurantTable.number)
    ).scalars().all()
    return render(request, "reservations.html", {
        "db": db, "staff": staff,
        "bookings": _waiting(db, "reservation"),
        "waitlist": _waiting(db, "waitlist"),
        "free_tables": free_tables,
        "now": datetime.now(),
        "title": "Reservations & waitlist",
    })


@router.post("/reservations")
def add_reservation(
    guest_name: str = Form(...),
    party_size: int = Form(2),
    at: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reservations")),
):
    """Book a table for a future time (4.1.5)."""
    if not guest_name.strip():
        raise HTTPException(400, "A name is required.")
    try:
        when = datetime.fromisoformat(at) if at else datetime.now()
    except ValueError:
        raise HTTPException(400, "That date and time could not be read.")
    db.add(Reservation(
        kind="reservation", guest_name=guest_name.strip(),
        party_size=max(1, party_size), phone=phone.strip(),
        notes=notes.strip(), at=when,
    ))
    db.commit()
    return RedirectResponse("/reservations", status_code=303)


@router.post("/waitlist")
def add_walkin(
    guest_name: str = Form(...),
    party_size: int = Form(2),
    quoted_minutes: int = Form(0),
    phone: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reservations")),
):
    """Add a walk-in party to the waitlist queue (4.1.5)."""
    if not guest_name.strip():
        raise HTTPException(400, "A name is required.")
    db.add(Reservation(
        kind="waitlist", guest_name=guest_name.strip(),
        party_size=max(1, party_size), phone=phone.strip(),
        quoted_minutes=max(0, quoted_minutes), at=datetime.now(),
    ))
    db.commit()
    return RedirectResponse("/reservations", status_code=303)


@router.post("/reservations/{res_id}/seat")
def seat_reservation(
    res_id: int,
    table_id: int = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reservations")),
):
    """Seat a waiting party: open an order on a free table and link it."""
    res = db.get(Reservation, res_id)
    if res is None:
        raise HTTPException(404, "Reservation not found")
    if res.status != ReservationStatus.WAITING:
        raise HTTPException(400, "That party is no longer waiting.")
    table = db.get(RestaurantTable, table_id)
    if table is None:
        raise HTTPException(404, "Table not found")
    if not table.is_active or table.status != TableStatus.FREE:
        raise HTTPException(400, f"Table {table.number} is not free.")

    order = open_order_on_table(db, table, res.party_size, staff.id)
    res.status = ReservationStatus.SEATED
    res.table_id = table.id
    res.order_id = order.id
    db.commit()
    return RedirectResponse(f"/orders/{order.id}", status_code=303)


@router.post("/reservations/{res_id}/cancel")
def cancel_reservation(
    res_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reservations")),
):
    return _close(db, res_id, ReservationStatus.CANCELLED)


@router.post("/reservations/{res_id}/no-show")
def no_show_reservation(
    res_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reservations")),
):
    return _close(db, res_id, ReservationStatus.NO_SHOW)


def _close(db: Session, res_id: int, status: str) -> RedirectResponse:
    res = db.get(Reservation, res_id)
    if res is None:
        raise HTTPException(404, "Reservation not found")
    if res.status != ReservationStatus.WAITING:
        raise HTTPException(400, "That party is no longer waiting.")
    res.status = status
    db.commit()
    return RedirectResponse("/reservations", status_code=303)
