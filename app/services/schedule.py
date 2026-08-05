"""Staff scheduling helpers — week math, hours roll-ups and the grid model.

Kept out of the router so the date arithmetic and totals are unit-testable and
the route stays thin (same split as services/payments.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.oltp import Position, Shift, Staff
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
    shift: Shift
    state: str                # 'scheduled' | 'in' | 'done'
    hours: float
    is_open: bool             # unassigned (staff_id is None)
    top_pct: float
    height_pct: float
    lane: int
    lanes: int                # total lanes that day, for width


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


def _display_end_hour(sh: Shift) -> int:
    """The block's end hour on its start day (an overnight shift caps at 24)."""
    if sh.ends_at.date() > sh.starts_at.date():
        return 24
    return sh.ends_at.hour + (1 if sh.ends_at.minute else 0)


def _layout_day(blocks: list[CalBlock], start_min: int, span_min: int) -> None:
    """Greedy interval partition: give overlapping blocks distinct lanes so they
    render side by side. Sets .lane/.lanes/.top_pct/.height_pct in place."""
    lane_end: list[int] = []
    for b in blocks:                                   # already start-sorted
        s = b.shift.starts_at.hour * 60 + b.shift.starts_at.minute
        e = _display_end_hour(b.shift) * 60
        for i, end in enumerate(lane_end):
            if s >= end:
                lane_end[i] = e
                b.lane = i
                break
        else:
            b.lane = len(lane_end)
            lane_end.append(e)
    ncols = max(1, len(lane_end))
    for b in blocks:
        s = b.shift.starts_at.hour * 60 + b.shift.starts_at.minute
        e = _display_end_hour(b.shift) * 60
        top = max(0.0, (s - start_min) / span_min * 100)
        b.top_pct = min(top, 98.0)
        b.height_pct = max(4.0, min(100 - b.top_pct, (e - max(s, start_min)) / span_min * 100))
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

    # Visible time window comes from Settings (default 08:00–23:00) and still
    # expands to fit any shift that starts earlier or ends later.
    cfg_start, cfg_end = settings_svc.schedule_hours(db)
    if shifts:
        earliest = min(s.starts_at.hour for s in shifts)
        latest = max(_display_end_hour(s) for s in shifts)
    else:
        earliest, latest = cfg_start, cfg_end
    start_hour = max(0, min(earliest, cfg_start))
    end_hour = min(24, max(latest, cfg_end))
    start_min = start_hour * 60
    span_min = (end_hour - start_hour) * 60

    per_staff: dict[int, float] = {}
    for sh in shifts:
        idx = (sh.starts_at.date() - week_start).days
        if not (0 <= idx < 7):
            continue
        h = shift_hours(sh)
        blk = CalBlock(
            shift=sh, state=clock_state(sh), hours=h, is_open=sh.staff_id is None,
            top_pct=0.0, height_pct=0.0, lane=0, lanes=1,
        )
        days[idx].blocks.append(blk)
        days[idx].labor_hours += h
        if sh.staff_id is not None:
            per_staff[sh.staff_id] = per_staff.get(sh.staff_id, 0.0) + h

    for day in days:
        _layout_day(day.blocks, start_min, span_min)

    team = [TeamMember(staff=m, weekly_hours=per_staff.get(m.id, 0.0)) for m in staff_list]
    return Calendar(
        week_start=week_start, days=days,
        start_hour=start_hour, end_hour=end_hour,
        hours=list(range(start_hour, end_hour + 1)), team=team,
    )
