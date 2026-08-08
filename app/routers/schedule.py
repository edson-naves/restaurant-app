"""Staff scheduling + attendance routes.

A weekly roster: owner/manager build shifts and assign one person to each;
everyone can open the page but a regular member sees only their own week and
clocks in/out against their shifts. Mirrors reservations.py (GET renders a
page; POSTs take Form(...), mutate, redirect 303). Money never touches this.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import can, render, require
from app.models.oltp import (
    Position, SalesForecast, Shift, Staff, SwapRequest, SwapStatus,
    TimeOffRequest, TimeOffStatus,
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


def _guard_timeoff(db, member, starts, ends, force=False) -> None:
    """Warn before scheduling a shift onto a day the person is approved off.

    Returns 409 (not a hard 400) so the UI can ask "proceed anyway?" — the plan
    may have changed. Resubmitting with force=1 goes ahead.
    """
    if member is None or force:
        return
    off = sched.approved_timeoff_conflict(db, member.id, starts, ends)
    if off is not None:
        raise HTTPException(
            409,
            f"{member.name} has approved time off on "
            f"{starts.strftime('%a %b %d')}. Assign the shift anyway?",
        )


def _guard_no_pending_request(db, shift_id) -> None:
    """One open request per shift. A shift with a swap or reschedule still
    awaiting a manager's decision can't collect another — resubmitting the form
    would otherwise pile up redundant duplicates for the same shift."""
    existing = db.execute(
        select(SwapRequest).where(
            SwapRequest.shift_id == shift_id,
            SwapRequest.status == SwapStatus.PENDING,
        )
    ).first()
    if existing is not None:
        raise HTTPException(
            400,
            "There's already a pending request for this shift — wait for the "
            "manager to approve or deny it before sending another.",
        )


def _parse_date(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@router.get("/schedule")
def schedule_page(
    request: Request,
    view: str = "week",
    week: str = "",
    date: str = "",
    pos: int = 0,
    who: int = 0,
    copied: int = -1,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.view")),
):
    """The schedule, in a Week, Day, or Month view. Managers see every active
    staff member plus open shifts and can filter by position/person; anyone else
    sees only their own shifts and clocks in/out."""
    manage = can(staff, "schedule.manage")
    if view not in ("week", "day", "month"):
        view = "week"

    # Focal date drives every view: ?date wins, then the legacy ?week, then today.
    focus = _parse_date(date) or _parse_date(week) or datetime.now().date()
    today = datetime.now().date()

    staff_list = (
        db.execute(select(Staff).where(Staff.is_active.is_(True)).order_by(Staff.name))
        .scalars().all()
        if manage else [staff]
    )
    positions = db.execute(
        select(Position).where(Position.is_active.is_(True)).order_by(Position.sort_order)
    ).scalars().all()

    # Filters. A regular member only ever sees themselves, so the staff filter is
    # manager-only; the position filter is available to everyone.
    position_id = pos or None
    only_staff_id = (who or None) if manage else None

    def url(v, d):
        u = f"/schedule?view={v}&date={d.isoformat()}"
        if pos:
            u += f"&pos={pos}"
        if who and manage:
            u += f"&who={who}"
        return u

    ctx = {
        "db": db, "staff": staff, "view": view, "positions": positions,
        "focus": focus, "today": today,
        # The team panel renders in every view, so it's built once here rather
        # than pulled off the (view-specific) calendar object.
        "team": sched.team_for(db, sched.week_start_of(focus), staff_list),
        "pos": pos, "who": who,
        "week_url": url("week", focus), "day_url": url("day", focus),
        "month_url": url("month", focus), "today_url": url(view, today),
        "copied": copied,
        # Requests panel (unchanged): managers see everyone's pending items.
        "pending_timeoff": sched.pending_timeoff(db) if manage else [],
        "my_timeoff": sched.timeoff_for(db, staff.id),
        "pending_swaps": sched.pending_swaps(db) if manage else [],
        "my_swaps": sched.swaps_for(db, staff.id),
        "my_shifts": sched.my_upcoming_shifts(db, staff.id),
        "labor": None,
        "title": "Schedule",
    }

    if view == "month":
        month = sched.build_month(db, focus, staff_list, include_open=manage,
                                  position_id=position_id, only_staff_id=only_staff_id)
        prev_focus = (focus.replace(day=1) - timedelta(days=1)).replace(day=1)
        next_focus = sched._first_of_next_month(focus.replace(day=1))
        ctx.update(month=month, period_label=month.label,
                   prev_url=url("month", prev_focus), next_url=url("month", next_focus))
    elif view == "day":
        cal = sched.build_day(db, focus, staff_list, include_open=manage,
                              position_id=position_id, only_staff_id=only_staff_id)
        ctx.update(cal=cal, period_label=f"{focus.strftime('%A, %b')} {focus.day}",
                   prev_url=url("day", focus - timedelta(days=1)),
                   next_url=url("day", focus + timedelta(days=1)))
    else:
        week_start = sched.week_start_of(focus)
        cal = sched.build_calendar(db, week_start, staff_list, include_open=manage,
                                   position_id=position_id, only_staff_id=only_staff_id)
        end = week_start + timedelta(days=6)
        ctx.update(
            cal=cal, week_start=week_start,
            period_label=f"{week_start.strftime('%b')} {week_start.day} – {end.strftime('%b')} {end.day}",
            prev_url=url("week", week_start - timedelta(days=7)),
            next_url=url("week", week_start + timedelta(days=7)),
            # Labor cost / coverage (owner-manager only, week-based).
            labor=sched.labor_summary(db, week_start) if manage else None,
        )

    return render(request, "schedule.html", ctx)


@router.post("/schedule/copy-week")
def copy_week(
    week: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    """Clone last week's roster into this week (skipping clashes)."""
    week_start = sched.week_start_for(week or None)
    created = sched.copy_last_week(db, week_start)
    return RedirectResponse(
        f"/schedule?view=week&date={week_start.isoformat()}&copied={created}",
        status_code=303,
    )


@router.post("/schedule/forecast")
def set_forecast(
    week: str = Form(""),
    dates: list[str] = Form(default=[]),
    amounts: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    """Set the forecast sales for each day of the week (dollars → cents)."""
    existing = {
        f.date: f for f in db.execute(select(SalesForecast)).scalars().all()
    }
    for ds, amt in zip(dates, amounts):
        try:
            d = datetime.strptime(ds, "%Y-%m-%d").date()
            cents = max(0, int(round(float(amt or 0) * 100)))
        except ValueError:
            continue
        row = existing.get(d)
        if row is None:
            db.add(SalesForecast(date=d, forecast_cents=cents))
        else:
            row.forecast_cents = cents
    db.commit()
    return RedirectResponse(f"/schedule?week={week or sched.week_start_of(datetime.now().date()).isoformat()}", status_code=303)


@router.get("/schedule/pending-count")
def pending_count(
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    """Live count of pending time-off + swap requests — polled to alert managers
    the moment a new request lands, without a full page reload."""
    return JSONResponse({"count": sched.pending_request_count(db)})


@router.get("/schedule/requests")
def requests_page(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.view")),
):
    """All requests in one place: managers see everyone's, staff see their own."""
    manage = can(staff, "schedule.manage")
    scope = None if manage else staff.id
    return render(request, "schedule_requests.html", {
        "db": db, "staff": staff,
        "timeoff": sched.all_timeoff(db, scope),
        "swaps": sched.all_swaps(db, scope),
        "title": "Requests",
    })


@router.post("/schedule/timeoff")
def file_timeoff(
    start_month: int = Form(...),
    start_day: int = Form(...),
    end_month: int = Form(0),
    end_day: int = Form(0),
    reason: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.request")),
):
    """File a time-off request as month/day — the year is inferred (this year, or
    next if the date already passed), so the form never shows a year."""
    today = date.today()

    def _make(m: int, d: int, year: int) -> date:
        try:
            return date(year, m, d)
        except ValueError:
            raise HTTPException(400, "Enter a valid date.")

    s = _make(start_month, start_day, today.year)
    if s < today:
        s = _make(start_month, start_day, today.year + 1)
    if end_month and end_day:
        e = _make(end_month, end_day, s.year)
        if e < s:                                  # range wraps into the new year
            e = _make(end_month, end_day, s.year + 1)
    else:
        e = s
    starts = datetime(s.year, s.month, s.day)
    ends = datetime(e.year, e.month, e.day, 23, 59, 59)
    # One live request per period — block a second overlapping pending/approved
    # request for the same person (so they can't be "booked off" twice).
    dup = sched.overlapping_timeoff(db, staff.id, starts, ends)
    if dup is not None:
        span = dup.starts_at.strftime('%b %d') + (
            f"–{dup.ends_at.strftime('%b %d')}" if dup.ends_at.date() != dup.starts_at.date() else ""
        )
        raise HTTPException(
            400,
            f"You already have time off {dup.status} for {span}. "
            f"Edit or cancel that request instead of filing another.",
        )
    db.add(TimeOffRequest(
        staff_id=staff.id,
        starts_at=starts,
        ends_at=ends,
        reason=reason.strip(),
    ))
    db.commit()
    return RedirectResponse("/schedule?requested=timeoff", status_code=303)


def _decide_timeoff(db, staff, req_id, status, force=False) -> RedirectResponse:
    req = db.get(TimeOffRequest, req_id)
    if req is None:
        raise HTTPException(404, "Request not found.")
    # Approving time off that lands on shifts they're already scheduled for is an
    # inconsistency — warn (409) and let the manager proceed if the plan changed.
    if status == TimeOffStatus.APPROVED and not force:
        clash = sched.shifts_in_window(db, req.staff_id, req.starts_at, req.ends_at)
        if clash:
            days = ", ".join(sorted({s.starts_at.strftime("%a %b %d") for s in clash}))
            raise HTTPException(
                409,
                f"{req.staff.name} has {len(clash)} shift(s) in that window ({days}). "
                f"Approve the time off anyway?",
            )
    req.status = status
    req.decided_by_id = staff.id
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.week_start_of(req.starts_at.date()).isoformat()}", status_code=303
    )


@router.post("/schedule/timeoff/{req_id}/approve")
def approve_timeoff(
    req_id: int,
    force: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    return _decide_timeoff(db, staff, req_id, TimeOffStatus.APPROVED, force=bool(force))


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
    _guard_no_pending_request(db, shift.id)
    db.add(SwapRequest(
        shift_id=shift.id, requested_by_id=staff.id,
        target_staff_id=(target.id if target else None),
    ))
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.week_start_of(shift.starts_at.date()).isoformat()}&requested=swap",
        status_code=303,
    )


@router.post("/schedule/propose")
def propose_shift_change(
    shift_id: int = Form(...),
    date: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.request")),
):
    """Propose a new day/time for one of your shifts (a reschedule request the
    manager approves), rather than handing it to a teammate."""
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise HTTPException(404, "Shift not found.")
    if shift.staff_id != staff.id and not can(staff, "schedule.manage"):
        raise HTTPException(403, "You can only propose a change to your own shift.")
    starts, ends = _parse_window(date, start, end)
    _guard_no_pending_request(db, shift.id)
    db.add(SwapRequest(
        shift_id=shift.id, requested_by_id=staff.id, target_staff_id=None,
        new_starts_at=starts, new_ends_at=ends,
    ))
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.week_start_of(shift.starts_at.date()).isoformat()}&requested=swap",
        status_code=303,
    )


@router.post("/schedule/swaps/{swap_id}/approve")
def approve_swap(
    swap_id: int,
    force: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    """Approve a swap: reschedule the shift to the proposed time, or reassign it
    to the target (open it if none)."""
    sw = db.get(SwapRequest, swap_id)
    if sw is None:
        raise HTTPException(404, "Swap not found.")
    shift = sw.shift
    if sw.new_starts_at is not None:
        # A reschedule request — move the shift to the proposed time, same person.
        _guard_overlap(db, shift.staff_id, sw.new_starts_at, sw.new_ends_at, exclude_id=shift.id)
        _guard_timeoff(db, shift.staff, sw.new_starts_at, sw.new_ends_at, force=bool(force))
        shift.starts_at = sw.new_starts_at
        shift.ends_at = sw.new_ends_at
    else:
        new_staff = sw.target_staff_id            # None = open
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
        f"/schedule?week={sched.week_start_of(shift.starts_at.date()).isoformat()}", status_code=303
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
    force: int = Form(0),             # 1 = proceed despite approved time off
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
    _guard_timeoff(db, member, starts, ends, force=bool(force))
    db.add(Shift(
        staff_id=(member.id if member else None),
        position_id=(pos.id if pos else None),
        starts_at=starts, ends_at=ends,
        role=(pos.name if pos else (member.role if member else "Open")),
        notes=notes.strip(),
    ))
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.week_start_of(starts.date()).isoformat()}", status_code=303
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
    force: int = Form(0),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise HTTPException(404, "Shift not found.")
    member, pos = _resolve(db, staff_id, position_id)
    starts, ends = _parse_window(date, start, end)
    _guard_overlap(db, member.id if member else None, starts, ends, exclude_id=shift.id)
    _guard_timeoff(db, member, starts, ends, force=bool(force))
    shift.staff_id = member.id if member else None
    shift.position_id = pos.id if pos else None
    shift.starts_at, shift.ends_at = starts, ends
    if pos:
        shift.role = pos.name
    shift.notes = notes.strip()
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.week_start_of(starts.date()).isoformat()}", status_code=303
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
        # Skip days the person is approved off — never repeat onto time off.
        if src.staff_id and sched.approved_timeoff_conflict(db, src.staff_id, new_start, new_end):
            continue
        db.add(Shift(
            staff_id=src.staff_id, position_id=src.position_id,
            starts_at=new_start, ends_at=new_end, role=src.role, notes=src.notes,
        ))
    db.commit()
    return RedirectResponse(
        f"/schedule?week={sched.week_start_of(src.starts_at.date()).isoformat()}", status_code=303
    )


@router.post("/schedule/shifts/{shift_id}/delete")
def delete_shift(
    shift_id: int,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("schedule.manage")),
):
    shift = db.get(Shift, shift_id)
    if shift is not None:
        week = sched.week_start_of(shift.starts_at.date()).isoformat()
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
        f"/schedule?week={sched.week_start_of(shift.starts_at.date()).isoformat()}",
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
