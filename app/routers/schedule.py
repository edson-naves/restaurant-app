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
from app.models.oltp import (
    Position, Shift, Staff, SwapRequest, SwapStatus, TimeOffRequest, TimeOffStatus,
)
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
    """The week calendar. Managers see every active staff member plus open
    shifts; anyone else sees only their own week and clocks in/out."""
    week_start = sched.week_start_for(week or None)
    manage = can(staff, "schedule.manage")

    if manage:
        staff_list = db.execute(
            select(Staff).where(Staff.is_active.is_(True)).order_by(Staff.name)
        ).scalars().all()
    else:
        staff_list = [staff]

    cal = sched.build_calendar(db, week_start, staff_list, include_open=manage)
    positions = db.execute(
        select(Position).where(Position.is_active.is_(True)).order_by(Position.sort_order)
    ).scalars().all()
    return render(request, "schedule.html", {
        "db": db, "staff": staff,
        "cal": cal, "positions": positions,
        "week_start": week_start,
        "prev_week": (week_start - timedelta(days=7)).isoformat(),
        "next_week": (week_start + timedelta(days=7)).isoformat(),
        "this_week": sched.monday_of(datetime.now().date()).isoformat(),
        "today": datetime.now().date(),
        # Requests panel: managers see everyone's pending time off; everyone sees
        # their own recent requests + a form to file a new one.
        "pending_timeoff": sched.pending_timeoff(db) if manage else [],
        "my_timeoff": sched.timeoff_for(db, staff.id),
        "pending_swaps": sched.pending_swaps(db) if manage else [],
        "my_swaps": sched.swaps_for(db, staff.id),
        "title": "Schedule",
    })


@router.post("/schedule/timeoff")
def file_timeoff(
    start_date: str = Form(...),
    end_date: str = Form(""),
    reason: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.request")),
):
    """File a time-off request for yourself (a whole-day range)."""
    try:
        s = datetime.strptime(start_date, "%Y-%m-%d").date()
        e = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else s
    except ValueError:
        raise HTTPException(400, "Enter valid dates.")
    if e < s:
        s, e = e, s
    db.add(TimeOffRequest(
        staff_id=staff.id,
        starts_at=datetime(s.year, s.month, s.day),
        ends_at=datetime(e.year, e.month, e.day, 23, 59, 59),
        reason=reason.strip(),
    ))
    db.commit()
    return RedirectResponse("/schedule", status_code=303)


def _decide_timeoff(db, staff, req_id, status) -> RedirectResponse:
    req = db.get(TimeOffRequest, req_id)
    if req is None:
        raise HTTPException(404, "Request not found.")
    req.status = status
    req.decided_by_id = staff.id
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.monday_of(req.starts_at.date()).isoformat()}", status_code=303
    )


@router.post("/schedule/timeoff/{req_id}/approve")
def approve_timeoff(
    req_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    return _decide_timeoff(db, staff, req_id, TimeOffStatus.APPROVED)


@router.post("/schedule/timeoff/{req_id}/deny")
def deny_timeoff(
    req_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    return _decide_timeoff(db, staff, req_id, TimeOffStatus.DENIED)


@router.post("/schedule/shifts/{shift_id}/swap")
def request_swap(
    shift_id: int,
    target_staff_id: int = Form(0),           # 0 = open (any teammate)
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.request")),
):
    """Offer one of your shifts for a swap (to a teammate, or open to anyone)."""
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise HTTPException(404, "Shift not found.")
    if shift.staff_id != staff.id and not can(staff, "schedule.manage"):
        raise HTTPException(403, "You can only offer your own shift.")
    target = db.get(Staff, target_staff_id) if target_staff_id else None
    if target_staff_id and target is None:
        raise HTTPException(404, "Unknown teammate.")
    db.add(SwapRequest(
        shift_id=shift.id, requested_by_id=staff.id,
        target_staff_id=(target.id if target else None),
    ))
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.monday_of(shift.starts_at.date()).isoformat()}", status_code=303
    )


@router.post("/schedule/swaps/{swap_id}/approve")
def approve_swap(
    swap_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    """Approve a swap — reassign the shift to the target (or open it)."""
    sw = db.get(SwapRequest, swap_id)
    if sw is None:
        raise HTTPException(404, "Swap not found.")
    shift = sw.shift
    new_staff = sw.target_staff_id                # None = open
    if new_staff and sched.overlaps(db, new_staff, shift.starts_at, shift.ends_at, exclude_id=shift.id):
        raise HTTPException(400, "That teammate already works an overlapping shift.")
    shift.staff_id = new_staff
    if new_staff:
        member = db.get(Staff, new_staff)
        if shift.position_id is None and member and member.position_id:
            shift.position_id = member.position_id
    sw.status = SwapStatus.APPROVED
    sw.decided_by_id = staff.id
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.monday_of(shift.starts_at.date()).isoformat()}", status_code=303
    )


@router.post("/schedule/swaps/{swap_id}/deny")
def deny_swap(
    swap_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    sw = db.get(SwapRequest, swap_id)
    if sw is None:
        raise HTTPException(404, "Swap not found.")
    sw.status = SwapStatus.DENIED
    sw.decided_by_id = staff.id
    db.commit()
    return RedirectResponse("/schedule", status_code=303)


def _resolve(db, staff_id: int, position_id: int) -> tuple[Staff | None, Position | None]:
    """Look up the (optional) assignee and position. staff_id 0 = open shift."""
    member = db.get(Staff, staff_id) if staff_id else None
    if staff_id and member is None:
        raise HTTPException(404, "Unknown staff member.")
    pos = db.get(Position, position_id) if position_id else None
    return member, pos


@router.post("/schedule/shifts")
def create_shift(
    staff_id: int = Form(0),          # 0 = open (unassigned) shift
    position_id: int = Form(0),
    date: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    member, pos = _resolve(db, staff_id, position_id)
    # No position picked (e.g. a dragged-in shift) → use the person's usual one,
    # so their shifts auto-colour without a manual pick.
    if pos is None and member is not None and member.position_id:
        pos = member.position
    starts, ends = _parse_window(date, start, end)
    _guard_overlap(db, member.id if member else None, starts, ends)
    db.add(Shift(
        staff_id=(member.id if member else None),
        position_id=(pos.id if pos else None),
        starts_at=starts, ends_at=ends,
        role=(pos.name if pos else (member.role if member else "Open")),
        notes=notes.strip(),
    ))
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.monday_of(starts.date()).isoformat()}", status_code=303
    )


@router.post("/schedule/shifts/{shift_id}/edit")
def edit_shift(
    shift_id: int,
    staff_id: int = Form(0),          # 0 = leave open / unassign
    position_id: int = Form(0),
    date: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise HTTPException(404, "Shift not found.")
    member, pos = _resolve(db, staff_id, position_id)
    starts, ends = _parse_window(date, start, end)
    _guard_overlap(db, member.id if member else None, starts, ends, exclude_id=shift.id)
    shift.staff_id = member.id if member else None
    shift.position_id = pos.id if pos else None
    shift.starts_at, shift.ends_at = starts, ends
    if pos:
        shift.role = pos.name
    shift.notes = notes.strip()
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.monday_of(starts.date()).isoformat()}", status_code=303
    )


@router.post("/schedule/shifts/{shift_id}/repeat")
def repeat_shift(
    shift_id: int,
    dates: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    """Copy a shift onto other days (same person, position, time-of-day and
    length) — the 'works the same hours several days' case. Days that would
    double-book the person are skipped."""
    src = db.get(Shift, shift_id)
    if src is None:
        raise HTTPException(404, "Shift not found.")
    dur = src.ends_at - src.starts_at
    tod = src.starts_at.time()
    for token in dates.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            d = datetime.strptime(token, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d == src.starts_at.date():
            continue
        new_start = datetime.combine(d, tod)
        new_end = new_start + dur
        if src.staff_id and sched.overlaps(db, src.staff_id, new_start, new_end):
            continue
        db.add(Shift(
            staff_id=src.staff_id, position_id=src.position_id,
            starts_at=new_start, ends_at=new_end, role=src.role, notes=src.notes,
        ))
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.monday_of(src.starts_at.date()).isoformat()}", status_code=303
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
