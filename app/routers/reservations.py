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
    """Bookings, the walk-in waitlist, and a live at-a-glance summary."""
    now = datetime.now()
    free_tables = db.execute(
        select(RestaurantTable).where(
            RestaurantTable.is_active.is_(True),
            RestaurantTable.status == TableStatus.FREE,
        ).order_by(RestaurantTable.number)
    ).scalars().all()

    bookings = _waiting(db, "reservation")
    waitlist = _waiting(db, "waitlist")
    today = now.date()
    today_res = [r for r in bookings if r.at.date() <= today]
    upcoming_res = [r for r in bookings if r.at.date() > today]

    seated_today = db.execute(
        select(Reservation).where(
            Reservation.status == ReservationStatus.SEATED,
            Reservation.at >= datetime(today.year, today.month, today.day),
        )
    ).scalars().all()

    def guests(rs):
        return sum(r.party_size for r in rs)

    avg_wait = round(sum(r.quoted_minutes for r in waitlist) / len(waitlist)) if waitlist else 0
    stats = {
        "res_count": len(bookings), "res_guests": guests(bookings),
        "wl_count": len(waitlist), "wl_avg": avg_wait,
        "seated_count": len(seated_today), "seated_guests": guests(seated_today),
        "upcoming_count": len(upcoming_res), "upcoming_guests": guests(upcoming_res),
    }

    return render(request, "reservations.html", {
        "db": db, "staff": staff,
        "bookings": bookings, "today_res": today_res, "upcoming_res": upcoming_res,
        "waitlist": waitlist, "free_tables": free_tables,
        "stats": stats, "now": now, "today_str": now.strftime("%Y-%m-%d"),
        "title": "Reservations & waitlist",
    })


@router.post("/reservations")
def add_reservation(
    guest_name: str = Form(...),
    party_size: int = Form(2),
    date: str = Form(""),
    time: str = Form(""),
    at: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    table_pref: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reservations")),
):
    """Book a table for a future time (4.1.5)."""
    if not guest_name.strip():
        raise HTTPException(400, "A name is required.")
    stamp = at or (f"{date}T{time or '00:00'}" if date else "")
    try:
        when = datetime.fromisoformat(stamp) if stamp else datetime.now()
    except ValueError:
        raise HTTPException(400, "That date and time could not be read.")
    note = notes.strip()
    if table_pref.strip() and table_pref.strip().lower() != "any":
        note = (f"Table pref: {table_pref.strip()}. " + note).strip()
    db.add(Reservation(
        kind="reservation", guest_name=guest_name.strip(),
        party_size=max(1, party_size), phone=phone.strip(),
        notes=note, at=when,
    ))
    db.commit()
    return RedirectResponse("/reservations", status_code=303)


@router.post("/reservations/{res_id}/edit")
def edit_reservation(
    res_id: int,
    guest_name: str = Form(...),
    party_size: int = Form(2),
    date: str = Form(""),
    time: str = Form(""),
    quoted_minutes: int = Form(0),
    phone: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reservations")),
):
    """Edit a waiting reservation or walk-in (4.1.5)."""
    res = db.get(Reservation, res_id)
    if res is None:
        raise HTTPException(404, "Reservation not found")
    if res.status != ReservationStatus.WAITING:
        raise HTTPException(400, "That party is no longer waiting.")
    if not guest_name.strip():
        raise HTTPException(400, "A name is required.")
    res.guest_name = guest_name.strip()
    res.party_size = max(1, party_size)
    res.phone = phone.strip()
    res.notes = notes.strip()
    if res.kind == "reservation" and (date or time):
        stamp = f"{date or res.at.date().isoformat()}T{time or res.at.strftime('%H:%M')}"
        try:
            res.at = datetime.fromisoformat(stamp)
        except ValueError:
            raise HTTPException(400, "That date and time could not be read.")
    if res.kind == "waitlist":
        res.quoted_minutes = max(0, quoted_minutes)
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
