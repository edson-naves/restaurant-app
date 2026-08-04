"""Staff scheduling + attendance routes.

A weekly roster: owner/manager build shifts and assign one person to each;
everyone can open the page but a regular member sees only their own week and
clocks in/out against their shifts. Mirrors reservations.py (GET renders a
page; POSTs take Form(...), mutate, redirect 303). Money never touches this.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import can, render, require
from app.models.oltp import Role, Shift, Staff
from app.services import schedule as sched

router = APIRouter()


def _parse_window(date_s: str, start_s: str, end_s: str) -> tuple[datetime, datetime]:
    """Build (starts_at, ends_at) from a date + two HH:MM times. An end at or
    before the start means the shift runs past midnight, so roll it a day on."""
    try:
        d = datetime.strptime(date_s, "%Y-%m-%d").date()
        st = datetime.strptime(start_s, "%H:%M").time()
        en = datetime.strptime(end_s, "%H:%M").time()
    except ValueError:
        raise HTTPException(400, "Enter a valid date and start/end time.")
    starts = datetime.combine(d, st)
    ends = datetime.combine(d, en)
    if ends <= starts:
        ends += timedelta(days=1)
    return starts, ends


def _guard_overlap(db, staff_id, starts, ends, exclude_id=None) -> None:
    clash = sched.overlaps(db, staff_id, starts, ends, exclude_id=exclude_id)
    if clash is not None:
        raise HTTPException(
            400,
            f"That overlaps an existing shift "
            f"({clash.starts_at.strftime('%a %H:%M')}–{clash.ends_at.strftime('%H:%M')}). "
            f"Edit or remove it first.",
        )


@router.get("/schedule")
def schedule_page(
    request: Request,
    week: str = "",
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.view")),
):
    """The weekly grid. Managers see every active staff member; anyone else sees
    only their own row and can clock in/out on their shifts."""
    week_start = sched.week_start_for(week or None)

    if can(staff, "schedule.manage"):
        staff_list = db.execute(
            select(Staff).where(Staff.is_active.is_(True)).order_by(Staff.name)
        ).scalars().all()
    else:
        staff_list = [staff]

    grid = sched.build_grid(db, week_start, staff_list)
    return render(request, "schedule.html", {
        "db": db, "staff": staff,
        "grid": grid,
        "week_start": week_start,
        "prev_week": (week_start - timedelta(days=7)).isoformat(),
        "next_week": (week_start + timedelta(days=7)).isoformat(),
        "this_week": sched.monday_of(datetime.now().date()).isoformat(),
        "today": datetime.now().date(),
        "roles": Role.ALL,
        "title": "Schedule",
    })


@router.post("/schedule/shifts")
def create_shift(
    staff_id: int = Form(...),
    date: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    role: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    member = db.get(Staff, staff_id)
    if member is None:
        raise HTTPException(404, "Unknown staff member.")
    starts, ends = _parse_window(date, start, end)
    _guard_overlap(db, staff_id, starts, ends)
    db.add(Shift(
        staff_id=staff_id, starts_at=starts, ends_at=ends,
        role=(role or member.role), notes=notes.strip(),
    ))
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.monday_of(starts.date()).isoformat()}", status_code=303
    )


@router.post("/schedule/shifts/{shift_id}/edit")
def edit_shift(
    shift_id: int,
    date: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    role: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise HTTPException(404, "Shift not found.")
    starts, ends = _parse_window(date, start, end)
    _guard_overlap(db, shift.staff_id, starts, ends, exclude_id=shift.id)
    shift.starts_at, shift.ends_at = starts, ends
    if role:
        shift.role = role
    shift.notes = notes.strip()
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.monday_of(starts.date()).isoformat()}", status_code=303
    )


@router.post("/schedule/shifts/{shift_id}/delete")
def delete_shift(
    shift_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    shift = db.get(Shift, shift_id)
    if shift is not None:
        week = sched.monday_of(shift.starts_at.date()).isoformat()
        db.delete(shift)
        db.commit()
    else:
        week = ""
    return RedirectResponse(f"/schedule?week={week}", status_code=303)


def _clock(db: Session, staff: Staff, shift_id: int, out: bool) -> RedirectResponse:
    """Shared clock-in/out. A member may clock their own shift; managers anyone's."""
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise HTTPException(404, "Shift not found.")
    if shift.staff_id != staff.id and not can(staff, "schedule.manage"):
        raise HTTPException(403, "You can only clock in/out on your own shift.")
    now = datetime.now()
    if out:
        if shift.clock_in_at is None:
            raise HTTPException(400, "Clock in before clocking out.")
        shift.clock_out_at = now
    else:
        shift.clock_in_at = now
        shift.clock_out_at = None      # re-clocking in clears a prior clock-out
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.monday_of(shift.starts_at.date()).isoformat()}",
        status_code=303,
    )


@router.post("/schedule/shifts/{shift_id}/clock-in")
def clock_in(
    shift_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.view")),
):
    return _clock(db, staff, shift_id, out=False)


@router.post("/schedule/shifts/{shift_id}/clock-out")
def clock_out(
    shift_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.view")),
):
    return _clock(db, staff, shift_id, out=True)
