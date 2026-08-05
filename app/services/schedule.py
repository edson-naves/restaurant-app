"""Staff scheduling helpers — week math, hours roll-ups and the grid model.

Kept out of the router so the date arithmetic and totals are unit-testable and
the route stays thin (same split as services/payments.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.oltp import (
    Position, Shift, Staff, SwapRequest, SwapStatus, TimeOffRequest, TimeOffStatus,
)
from app.services import settings as settings_svc


# Default restaurant positions with calendar colours, seeded once if none exist.
DEFAULT_POSITIONS: tuple[tuple[str, str], ...] = (
    ("Manager", "#14b8a6"),
    ("Server", "#3b82f6"),
    ("Bartender", "#8b5cf6"),
    ("Host", "#f59e0b"),
    ("Line Cook", "#22c55e"),
    ("Prep Cook", "#84cc16"),
    ("Dishwasher", "#9ca3af"),
)


def pending_timeoff(db: Session) -> list[TimeOffRequest]:
    """All pending time-off requests (for the manager approval panel)."""
    return db.execute(
        select(TimeOffRequest).where(TimeOffRequest.status == TimeOffStatus.PENDING)
        .order_by(TimeOffRequest.starts_at)
    ).scalars().all()


def timeoff_for(db: Session, staff_id: int, limit: int = 6) -> list[TimeOffRequest]:
    """A person's own recent time-off requests (to show them the status)."""
    return db.execute(
        select(TimeOffRequest).where(TimeOffRequest.staff_id == staff_id)
        .order_by(TimeOffRequest.created_at.desc())
    ).scalars().all()[:limit]


def pending_swaps(db: Session) -> list[SwapRequest]:
    """All pending swap requests (for the manager approval panel)."""
    return db.execute(
        select(SwapRequest).where(SwapRequest.status == SwapStatus.PENDING)
        .order_by(SwapRequest.created_at)
    ).scalars().all()


def swaps_for(db: Session, staff_id: int, limit: int = 6) -> list[SwapRequest]:
    """A person's own recent swap requests (to show them the status)."""
    return db.execute(
        select(SwapRequest).where(SwapRequest.requested_by_id == staff_id)
        .order_by(SwapRequest.created_at.desc())
    ).scalars().all()[:limit]


def all_timeoff(db: Session, staff_id: int | None = None) -> list[TimeOffRequest]:
    """Every time-off request (newest first). Scoped to one person if given."""
    q = select(TimeOffRequest).order_by(TimeOffRequest.created_at.desc())
    if staff_id is not None:
        q = q.where(TimeOffRequest.staff_id == staff_id)
    return db.execute(q).scalars().all()


def all_swaps(db: Session, staff_id: int | None = None) -> list[SwapRequest]:
    """Every swap request (newest first). Scoped to one requester if given."""
    q = select(SwapRequest).order_by(SwapRequest.created_at.desc())
    if staff_id is not None:
        q = q.where(SwapRequest.requested_by_id == staff_id)
    return db.execute(q).scalars().all()


def pending_request_count(db: Session) -> int:
    """Total pending time-off + swap requests — the schedule nav badge."""
    n = db.execute(
        select(func.count()).select_from(TimeOffRequest)
        .where(TimeOffRequest.status == TimeOffStatus.PENDING)
    ).scalar() or 0
    n += db.execute(
        select(func.count()).select_from(SwapRequest)
        .where(SwapRequest.status == SwapStatus.PENDING)
    ).scalar() or 0
    return n


def ensure_default_positions(db: Session) -> None:
    """Seed the standard positions on first run (idempotent). Runs on startup so
    an existing database — including production — gets them without a reseed."""
    if db.execute(select(func.count()).select_from(Position)).scalar():
        return
    for i, (name, color) in enumerate(DEFAULT_POSITIONS):
        db.add(Position(name=name, color=color, sort_order=i))
    db.commit()


# --------------------------------------------------------------------------
# Week math
# --------------------------------------------------------------------------

def monday_of(d: date) -> date:
    """The Monday on or before d (weeks run Monday..Sunday)."""
    return d - timedelta(days=d.weekday())


def week_start_for(param: str | None) -> date:
    """Resolve the ?week= param (any YYYY-MM-DD in the week) to that week's
    Monday; falls back to the current week for a missing/garbage value."""
    base = date.today()
    if param:
        try:
            base = datetime.strptime(param, "%Y-%m-%d").date()
        except ValueError:
            pass
    return monday_of(base)


def week_days(week_start: date) -> list[date]:
    return [week_start + timedelta(days=i) for i in range(7)]


# --------------------------------------------------------------------------
# Hours
# --------------------------------------------------------------------------

def shift_hours(shift: Shift) -> float:
    """Scheduled length in hours."""
    return max(0.0, (shift.ends_at - shift.starts_at).total_seconds() / 3600.0)


def worked_hours(shift: Shift) -> float:
    """Actual clocked hours, or 0 until both clock times exist."""
    if shift.clock_in_at and shift.clock_out_at:
        return max(0.0, (shift.clock_out_at - shift.clock_in_at).total_seconds() / 3600.0)
    return 0.0


def clock_state(shift: Shift) -> str:
    """'scheduled' (not started), 'in' (clocked in, not out), or 'done'."""
    if shift.clock_in_at and not shift.clock_out_at:
        return "in"
    if shift.clock_in_at and shift.clock_out_at:
        return "done"
    return "scheduled"


# --------------------------------------------------------------------------
# Overlap (double-booking) check
# --------------------------------------------------------------------------

def overlaps(
    db: Session, staff_id: int, starts_at: datetime, ends_at: datetime,
    exclude_id: int | None = None,
) -> Shift | None:
    """Return an existing shift for this staff that overlaps [starts_at, ends_at),
    or None. Two windows overlap when each starts before the other ends."""
    if staff_id is None:
        return None
    q = select(Shift).where(
        Shift.staff_id == staff_id,
        Shift.starts_at < ends_at,
        Shift.ends_at > starts_at,
    )
    if exclude_id is not None:
        q = q.where(Shift.id != exclude_id)
    return db.execute(q).scalars().first()


# --------------------------------------------------------------------------
# Calendar model the template renders (day columns × time-of-day)
# --------------------------------------------------------------------------

@dataclass
class CalBlock:
    """One shift positioned on a day column: top/height as % of the visible time
    window, plus a lane so overlapping shifts sit side by side."""
    shift: Shift | None
    state: str                # 'scheduled' | 'in' | 'done'
    hours: float
    is_open: bool             # unassigned (staff_id is None)
    top_pct: float
    height_pct: float
    lane: int
    lanes: int                # total lanes that day, for width
    top_off: int = 0          # minutes from the window start
    end_off: int = 0          # minutes from the window start (may exceed a day)
    is_timeoff: bool = False  # an approved time-off band, not a shift
    label: str = ""           # staff name for a time-off band


@dataclass
class CalDay:
    date: date
    blocks: list[CalBlock] = field(default_factory=list)
    labor_hours: float = 0.0


@dataclass
class TeamMember:
    staff: Staff
    weekly_hours: float = 0.0


@dataclass
class Calendar:
    week_start: date
    days: list[CalDay]
    start_hour: int
    end_hour: int
    hours: list[int]          # hour marks for the time axis
    team: list[TeamMember]


def _layout_day(blocks: list[CalBlock], span_min: int) -> None:
    """Greedy interval partition: give overlapping blocks distinct lanes so they
    render side by side. Positions from precomputed minute offsets, so it works
    the same for overnight windows that run past midnight."""
    lane_end: list[int] = []
    for b in blocks:                                   # already start-sorted
        for i, end in enumerate(lane_end):
            if b.top_off >= end:
                lane_end[i] = b.end_off
                b.lane = i
                break
        else:
            b.lane = len(lane_end)
            lane_end.append(b.end_off)
    ncols = max(1, len(lane_end))
    for b in blocks:
        top = max(0.0, b.top_off / span_min * 100)
        b.top_pct = min(top, 98.0)
        b.height_pct = max(4.0, min(100 - b.top_pct, (b.end_off - max(b.top_off, 0)) / span_min * 100))
        b.lanes = ncols


def build_calendar(db: Session, week_start: date, staff_list: list[Staff],
                   include_open: bool) -> Calendar:
    """The week's shifts laid out on day columns by time. `staff_list` is the
    rows for the team panel; managers also see open (unassigned) shifts."""
    days = [CalDay(date=d) for d in week_days(week_start)]
    start_dt = datetime(week_start.year, week_start.month, week_start.day)
    end_dt = start_dt + timedelta(days=7)

    ids = [s.id for s in staff_list]
    conds = [Shift.starts_at >= start_dt, Shift.starts_at < end_dt]
    who = Shift.staff_id.in_(ids)
    who = (who | Shift.staff_id.is_(None)) if include_open else who
    shifts = db.execute(
        select(Shift).where(*conds, who).order_by(Shift.starts_at)
    ).scalars().all()

    # Visible window from Settings. An end at or before the start is an OVERNIGHT
    # window that runs into the next morning (e.g. 07:00–04:00 → hours 7..28).
    cfg_start, cfg_end = settings_svc.schedule_hours(db)
    win_start = cfg_start
    win_end = cfg_end if cfg_end > cfg_start else cfg_end + 24
    win_start_min = win_start * 60
    span_min = max(60, (win_end - win_start) * 60)

    per_staff: dict[int, float] = {}
    for sh in shifts:
        idx = (sh.starts_at.date() - week_start).days
        if not (0 <= idx < 7):
            continue
        smin = sh.starts_at.hour * 60 + sh.starts_at.minute
        emin = sh.ends_at.hour * 60 + sh.ends_at.minute
        if sh.ends_at.date() > sh.starts_at.date():
            emin += 24 * 60                            # crosses midnight
        h = shift_hours(sh)
        blk = CalBlock(
            shift=sh, state=clock_state(sh), hours=h, is_open=sh.staff_id is None,
            top_pct=0.0, height_pct=0.0, lane=0, lanes=1,
            top_off=smin - win_start_min, end_off=emin - win_start_min,
        )
        days[idx].blocks.append(blk)
        days[idx].labor_hours += h
        if sh.staff_id is not None:
            per_staff[sh.staff_id] = per_staff.get(sh.staff_id, 0.0) + h

    # Approved time off overlapping the week → full-height bands, so a shift laid
    # over someone's day off is a visible conflict.
    if ids:
        offs = db.execute(
            select(TimeOffRequest).where(
                TimeOffRequest.staff_id.in_(ids),
                TimeOffRequest.status == TimeOffStatus.APPROVED,
                TimeOffRequest.starts_at < end_dt,
                TimeOffRequest.ends_at >= start_dt,
            )
        ).scalars().all()
        for off in offs:
            d = max(off.starts_at.date(), week_start)
            last = min(off.ends_at.date(), week_start + timedelta(days=6))
            while d <= last:
                idx = (d - week_start).days
                if 0 <= idx < 7:
                    days[idx].blocks.append(CalBlock(
                        shift=None, state="", hours=0.0, is_open=False,
                        top_pct=0.0, height_pct=0.0, lane=0, lanes=1,
                        top_off=0, end_off=span_min, is_timeoff=True, label=off.staff.name,
                    ))
                d += timedelta(days=1)

    for day in days:
        _layout_day(day.blocks, span_min)

    team = [TeamMember(staff=m, weekly_hours=per_staff.get(m.id, 0.0)) for m in staff_list]
    return Calendar(
        week_start=week_start, days=days,
        start_hour=win_start, end_hour=win_end,
        hours=list(range(win_start, win_end + 1)), team=team,
    )
