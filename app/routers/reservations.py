"""Reservations & waitlist — section 4.1.5 (front of house)."""
from __future__ import annotations

from datetime import datetime, timedelta

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
    Zone,
    reservation_table,
)
from app.routers.sales import open_order_on_table
from app.services import reservations as reservations_svc
from app.services import settings

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
    held = reservations_svc.active_holds(db, now)
    free_tables = [table for table in db.execute(
        select(RestaurantTable).where(
            RestaurantTable.is_active.is_(True),
            RestaurantTable.status == TableStatus.FREE,
        ).order_by(RestaurantTable.number)
    ).scalars().all() if table.id not in held]

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
    # Arrivals due within the next hour — the host's "what's imminent" cue.
    soon = now + timedelta(hours=1)
    next_hour = [r for r in bookings if now <= r.at <= soon]
    stats = {
        # Status-based, mutually exclusive tiles (no double-counting):
        "expected_count": len(today_res), "expected_guests": guests(today_res),
        "wl_count": len(waitlist), "wl_avg": avg_wait,
        "seated_count": len(seated_today), "seated_guests": guests(seated_today),
        "soon_count": len(next_hour), "soon_guests": guests(next_hour),
    }

    map_tables = db.execute(
        select(RestaurantTable).where(RestaurantTable.is_active.is_(True))
        .order_by(RestaurantTable.number)
    ).scalars().all()
    booked_ids = {table.id for reservation in bookings for table in reservation.tables}
    floors: dict = {}
    for table in map_tables:
        floor = table.floor
        floor_entry = floors.setdefault(floor.id if floor else 0, {"floor": floor, "zones": {}})
        zone = table.zone_ref
        zone_entry = floor_entry["zones"].setdefault(
            zone.id if zone else 0, {"zone": zone, "tables": [], "seats": 0}
        )
        zone_entry["tables"].append(table)
        zone_entry["seats"] += table.capacity
    map_floors = []
    for floor_entry in sorted(floors.values(), key=lambda item: (
        item["floor"].sort_order if item["floor"] else 999,
        item["floor"].name if item["floor"] else "zzz",
    )):
        zones = sorted(floor_entry["zones"].values(), key=lambda item: (
            item["zone"].sort_order if item["zone"] else 999,
            item["zone"].name if item["zone"] else "zzz",
        ))
        map_floors.append({"floor": floor_entry["floor"], "zones": zones})

    return render(request, "reservations.html", {
        "db": db, "staff": staff,
        "bookings": bookings, "today_res": today_res, "upcoming_res": upcoming_res,
        "waitlist": waitlist, "free_tables": free_tables,
        "stats": stats, "now": now, "today_str": now.strftime("%Y-%m-%d"),
        "map_floors": map_floors, "booked_ids": booked_ids,
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
    table_ids: list[int] = Form(default=[]),
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
    party = max(1, party_size)

    tables = []
    if table_ids:
        # Lock the requested tables before checking availability: a concurrent
        # booking on the same table blocks here instead of both requests
        # reading "available" before either has written its reservation.
        # Locks the scalar id, not the mapped entity — RestaurantTable's eager
        # relationships (current_waiter, zone_ref) become LEFT OUTER JOINs, and
        # Postgres refuses FOR UPDATE on the nullable side of one. Same shape
        # as the Order lock in sales.py.
        ids = sorted(set(table_ids))
        locked_ids = db.execute(
            select(RestaurantTable.id)
            .where(RestaurantTable.id.in_(ids), RestaurantTable.is_active.is_(True))
            .order_by(RestaurantTable.id)
            .with_for_update()
        ).scalars().all()
        tables = [db.get(RestaurantTable, tid) for tid in locked_ids]
        if len(tables) != len(ids):
            raise HTTPException(400, "One or more selected tables are unavailable.")
        hold_before, drop_after = settings.reservation_window(db)
        duration = settings.reservation_duration_minutes(db)
        result = reservations_svc.calculate_availability(
            db,
            reservations_svc.AvailabilityRequest(
                at=when, party_size=party, now=datetime.now(),
                requested_table_ids=tuple(t.id for t in tables),
                hold_before_minutes=hold_before, drop_after_minutes=drop_after,
            ),
            duration_for=lambda _party_size: duration,
        )
        if not result.available:
            raise HTTPException(
                409, "One or more selected tables are already booked around that time."
            )

    reservation = Reservation(
        kind="reservation", guest_name=guest_name.strip(),
        party_size=party, phone=phone.strip(),
        notes=note, at=when,
    )
    if tables:
        reservation.tables = tables
    db.add(reservation)
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
    next: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reservations")),
):
    return _close(db, res_id, ReservationStatus.CANCELLED, next)


@router.post("/reservations/{res_id}/no-show")
def no_show_reservation(
    res_id: int,
    next: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reservations")),
):
    return _close(db, res_id, ReservationStatus.NO_SHOW, next)


@router.post("/reservations/{res_id}/seat-here")
def seat_reservation_here(
    res_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reservations")),
):
    reservation = db.get(Reservation, res_id)
    if reservation is None:
        raise HTTPException(404, "Reservation not found")
    if reservation.status != ReservationStatus.WAITING:
        raise HTTPException(400, "That party is no longer waiting.")
    # Held table ids via the raw association table, NOT reservation.tables —
    # touching that relationship here would load RestaurantTable into this
    # session's identity map before the lock below, and db.get() afterward
    # would then hand back that pre-lock (stale) instance instead of rereading
    # it. Selecting only the id column off reservation_table never populates a
    # RestaurantTable identity, so there is nothing to go stale.
    held_ids = db.execute(
        select(reservation_table.c.table_id).where(
            reservation_table.c.reservation_id == res_id
        )
    ).scalars().all()
    if not held_ids:
        raise HTTPException(400, "This booking has no tables to seat.")
    # Lock the held tables: a concurrent seat/open on the same table blocks
    # here instead of racing past the FREE check below. Locks the scalar id,
    # not the mapped entity — RestaurantTable's eager relationships become
    # LEFT OUTER JOINs, and Postgres refuses FOR UPDATE on the nullable side
    # of one (same note as add_reservation()).
    locked_ids = db.execute(
        select(RestaurantTable.id)
        .where(RestaurantTable.id.in_(held_ids))
        .order_by(RestaurantTable.id)
        .with_for_update()
    ).scalars().all()
    # Re-read with populate_existing: even though nothing above loaded these
    # rows into the identity map, this guarantees the FREE/is_active check
    # below reflects what the lock actually sees, not a cached instance from
    # elsewhere in the request.
    tables = [
        table for table in db.execute(
            select(RestaurantTable)
            .where(RestaurantTable.id.in_(locked_ids))
            .execution_options(populate_existing=True)
        ).scalars().all()
        if table.is_active and table.status == TableStatus.FREE
    ]
    if not tables:
        raise HTTPException(400, "None of this booking's tables are free.")
    floor_id = tables[0].floor_id or 0
    base, extra = divmod(max(1, reservation.party_size), len(tables))
    first_order = None
    for index, table in enumerate(tables):
        order = open_order_on_table(db, table, base + (index < extra), staff.id)
        first_order = first_order or order
    reservation.status = ReservationStatus.SEATED
    reservation.table_id = tables[0].id
    reservation.order_id = first_order.id
    db.commit()
    return RedirectResponse(f"/?floor={floor_id}", status_code=303)


def _close(db: Session, res_id: int, status: str, to: str = "") -> RedirectResponse:
    res = db.get(Reservation, res_id)
    if res is None:
        raise HTTPException(404, "Reservation not found")
    if res.status != ReservationStatus.WAITING:
        raise HTTPException(400, "That party is no longer waiting.")
    res.status = status
    db.commit()
    destination = to if to.startswith("/") and not to.startswith("//") else "/reservations"
    return RedirectResponse(destination, status_code=303)
