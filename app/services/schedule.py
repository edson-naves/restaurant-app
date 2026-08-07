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
    Position, SalesForecast, Shift, Staff, SwapRequest, SwapStatus,
    TimeOffRequest, TimeOffStatus,
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


def my_upcoming_shifts(db: Session, staff_id: int, limit: int = 30) -> list[Shift]:
    """A person's shifts from today onward — the pick-list for proposing a new
    day/time for one of them."""
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.execute(
        select(Shift).where(Shift.staff_id == staff_id, Shift.starts_at >= start)
        .order_by(Shift.starts_at)
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


def week_forecast(db: Session, week_start: date) -> dict[date, int]:
    """{day: forecast_cents} for the week (0 where unset)."""
    days = week_days(week_start)
    rows = db.execute(
        select(SalesForecast).where(SalesForecast.date.in_(days))
    ).scalars().all()
    have = {r.date: r.forecast_cents for r in rows}
    return {d: have.get(d, 0) for d in days}


def labor_summary(db: Session, week_start: date) -> dict:
    """Week labor cost/hours vs forecast sales, plus coverage alerts."""
    start_dt = datetime(week_start.year, week_start.month, week_start.day)
    end_dt = start_dt + timedelta(days=7)
    shifts = db.execute(
        select(Shift).where(Shift.starts_at >= start_dt, Shift.starts_at < end_dt)
    ).scalars().all()

    assigned = [s for s in shifts if s.staff_id is not None]
    hours = sum(shift_hours(s) for s in assigned)
    cost_cents = int(round(sum(
        shift_hours(s) * (s.staff.wage_cents or 0) for s in assigned
    )))
    open_count = sum(1 for s in shifts if s.staff_id is None)

    fc = week_forecast(db, week_start)
    forecast_cents = sum(fc.values())
    pct = round(cost_cents / forecast_cents * 100, 1) if forecast_cents else 0.0
    target = settings_svc.labor_pct_target(db)

    covered = {s.starts_at.date() for s in shifts if s.staff_id is not None}
    uncovered = [d for d in week_days(week_start) if d not in covered]

    return {
        "hours": hours,
        "cost_cents": cost_cents,
        "forecast_cents": forecast_cents,
        "pct": pct,
        "target": target,
        "is_high": bool(forecast_cents) and pct > target,
        "open_count": open_count,
        "uncovered": uncovered,
        "forecast_by_day": fc,
    }


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


def approved_timeoff_conflict(
    db: Session, staff_id: int | None, starts_at: datetime, ends_at: datetime,
) -> TimeOffRequest | None:
    """An APPROVED time-off request for this staff overlapping [starts_at, ends_at),
    or None — so a shift is never scheduled onto a day they're approved off."""
    if staff_id is None:
        return None
    return db.execute(
        select(TimeOffRequest).where(
            TimeOffRequest.staff_id == staff_id,
            TimeOffRequest.status == TimeOffStatus.APPROVED,
            TimeOffRequest.starts_at < ends_at,
            TimeOffRequest.ends_at > starts_at,
        )
    ).scalars().first()


def shifts_in_window(
    db: Session, staff_id: int | None, starts_at: datetime, ends_at: datetime,
) -> list[Shift]:
    """This staff's shifts overlapping [starts_at, ends_at) — used to stop time
    off being approved on top of shifts they're already scheduled for."""
    if staff_id is None:
        return []
    return db.execute(
        select(Shift).where(
            Shift.staff_id == staff_id,
            Shift.starts_at < ends_at,
            Shift.ends_at > starts_at,
        ).order_by(Shift.starts_at)
    ).scalars().all()


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


def _week_hours(db: Session, week_start: date, ids: list[int]) -> dict[int, float]:
    """Scheduled hours per staff over the week containing the view, so the team
    panel always reads weekly totals even when the calendar shows a single day."""
    if not ids:
        return {}
    s = datetime(week_start.year, week_start.month, week_start.day)
    e = s + timedelta(days=7)
    rows = db.execute(
        select(Shift).where(Shift.staff_id.in_(ids), Shift.starts_at >= s, Shift.starts_at < e)
    ).scalars().all()
    out: dict[int, float] = {}
    for sh in rows:
        if sh.staff_id is not None:
            out[sh.staff_id] = out.get(sh.staff_id, 0.0) + shift_hours(sh)
    return out


def _build_span(db: Session, dates: list[date], staff_list: list[Staff],
                include_open: bool, position_id: int | None = None,
                only_staff_id: int | None = None) -> Calendar:
    """Lay out shifts on one or more day columns by time-of-day. Shared by the
    week and day views. `position_id`/`only_staff_id` filter which shifts show."""
    days = [CalDay(date=d) for d in dates]
    index = {d: i for i, d in enumerate(dates)}
    start_dt = datetime(dates[0].year, dates[0].month, dates[0].day)
    end_dt = datetime(dates[-1].year, dates[-1].month, dates[-1].day) + timedelta(days=1)

    ids = [s.id for s in staff_list]
    conds = [Shift.starts_at >= start_dt, Shift.starts_at < end_dt]
    if only_staff_id:
        who = Shift.staff_id == only_staff_id
    else:
        who = Shift.staff_id.in_(ids)
        who = (who | Shift.staff_id.is_(None)) if include_open else who
    if position_id:
        conds.append(Shift.position_id == position_id)
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

    for sh in shifts:
        idx = index.get(sh.starts_at.date())
        if idx is None:
            continue
        smin = sh.starts_at.hour * 60 + sh.starts_at.minute
        emin = sh.ends_at.hour * 60 + sh.ends_at.minute
        if sh.ends_at.date() > sh.starts_at.date():
            emin += 24 * 60                            # crosses midnight
        h = shift_hours(sh)
        days[idx].blocks.append(CalBlock(
            shift=sh, state=clock_state(sh), hours=h, is_open=sh.staff_id is None,
            top_pct=0.0, height_pct=0.0, lane=0, lanes=1,
            top_off=smin - win_start_min, end_off=emin - win_start_min,
        ))
        days[idx].labor_hours += h

    # Approved time off → full-height bands, so a shift laid over someone's day
    # off is a visible conflict. Hidden when a position filter is active (bands
    # have no position); scoped to the selected person when filtering by staff.
    band_ids = [only_staff_id] if only_staff_id else ids
    if band_ids and not position_id:
        offs = db.execute(
            select(TimeOffRequest).where(
                TimeOffRequest.staff_id.in_(band_ids),
                TimeOffRequest.status == TimeOffStatus.APPROVED,
                TimeOffRequest.starts_at < end_dt,
                TimeOffRequest.ends_at >= start_dt,
            )
        ).scalars().all()
        for off in offs:
            d = max(off.starts_at.date(), dates[0])
            last = min(off.ends_at.date(), dates[-1])
            while d <= last:
                idx = index.get(d)
                if idx is not None:
                    days[idx].blocks.append(CalBlock(
                        shift=None, state="", hours=0.0, is_open=False,
                        top_pct=0.0, height_pct=0.0, lane=0, lanes=1,
                        top_off=0, end_off=span_min, is_timeoff=True, label=off.staff.name,
                    ))
                d += timedelta(days=1)

    for day in days:
        _layout_day(day.blocks, span_min)

    week_start = monday_of(dates[0])
    team_hours = _week_hours(db, week_start, ids)
    team = [TeamMember(staff=m, weekly_hours=team_hours.get(m.id, 0.0)) for m in staff_list]
    return Calendar(
        week_start=week_start, days=days,
        start_hour=win_start, end_hour=win_end,
        hours=list(range(win_start, win_end + 1)), team=team,
    )


def team_for(db: Session, week_start: date, staff_list: list[Staff]) -> list[TeamMember]:
    """The team-panel rows (each with their weekly scheduled hours). Shared by
    every view so the panel renders the same in week, day and month."""
    hrs = _week_hours(db, week_start, [s.id for s in staff_list])
    return [TeamMember(staff=m, weekly_hours=hrs.get(m.id, 0.0)) for m in staff_list]


def build_calendar(db: Session, week_start: date, staff_list: list[Staff],
                   include_open: bool, position_id: int | None = None,
                   only_staff_id: int | None = None) -> Calendar:
    """The week's shifts laid out on day columns by time (7 columns)."""
    return _build_span(db, week_days(week_start), staff_list, include_open,
                       position_id, only_staff_id)


def build_day(db: Session, day: date, staff_list: list[Staff], include_open: bool,
              position_id: int | None = None, only_staff_id: int | None = None) -> Calendar:
    """A single day laid out as one wide column (the Day view)."""
    return _build_span(db, [day], staff_list, include_open, position_id, only_staff_id)


# --------------------------------------------------------------------------
# Month view (a calendar grid of day cells with shift chips)
# --------------------------------------------------------------------------

@dataclass
class MonthChip:
    label: str                # staff name, or "Open"
    time: str                 # "09:00–14:05" (empty for a time-off chip)
    color: str
    is_open: bool = False
    is_timeoff: bool = False


@dataclass
class MonthDay:
    date: date
    in_month: bool            # False for the greyed lead/trail days of adjacent months
    is_today: bool
    chips: list[MonthChip] = field(default_factory=list)
    more: int = 0             # shifts beyond the chip cap, shown as "+N"


@dataclass
class MonthView:
    year: int
    month: int
    label: str                # "August 2026"
    anchor: date              # the first of the month (for day/week links)
    weeks: list[list[MonthDay]]


def _first_of_next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def build_month(db: Session, anchor: date, staff_list: list[Staff], include_open: bool,
                position_id: int | None = None, only_staff_id: int | None = None,
                max_chips: int = 4) -> MonthView:
    """A month grid (Mon-first weeks) with each day's shifts as coloured chips."""
    first = anchor.replace(day=1)
    last = _first_of_next_month(first) - timedelta(days=1)
    grid_start = monday_of(first)
    grid_end = monday_of(last) + timedelta(days=7)      # exclusive; whole weeks
    n_days = (grid_end - grid_start).days
    today = date.today()

    ids = [s.id for s in staff_list]
    start_dt = datetime(grid_start.year, grid_start.month, grid_start.day)
    end_dt = datetime(grid_end.year, grid_end.month, grid_end.day)
    conds = [Shift.starts_at >= start_dt, Shift.starts_at < end_dt]
    if only_staff_id:
        who = Shift.staff_id == only_staff_id
    else:
        who = Shift.staff_id.in_(ids)
        who = (who | Shift.staff_id.is_(None)) if include_open else who
    if position_id:
        conds.append(Shift.position_id == position_id)
    shifts = db.execute(
        select(Shift).where(*conds, who).order_by(Shift.starts_at)
    ).scalars().all()

    by_day: dict[date, list[MonthChip]] = {}
    for sh in shifts:
        chip = MonthChip(
            label="Open" if sh.staff_id is None else (sh.staff.name.split()[0] if sh.staff else "?"),
            time=f"{sh.starts_at.strftime('%H:%M')}–{sh.ends_at.strftime('%H:%M')}",
            color=sh.position.color if sh.position else "#64748b",
            is_open=sh.staff_id is None,
        )
        by_day.setdefault(sh.starts_at.date(), []).append(chip)

    weeks: list[list[MonthDay]] = []
    for w in range(n_days // 7):
        row: list[MonthDay] = []
        for i in range(7):
            d = grid_start + timedelta(days=w * 7 + i)
            chips = by_day.get(d, [])
            row.append(MonthDay(
                date=d, in_month=(d.month == first.month), is_today=(d == today),
                chips=chips[:max_chips], more=max(0, len(chips) - max_chips),
            ))
        weeks.append(row)

    return MonthView(
        year=first.year, month=first.month,
        label=first.strftime("%B %Y"), anchor=first, weeks=weeks,
    )


def copy_last_week(db: Session, week_start: date) -> int:
    """Clone the previous week's shifts into `week_start` (same weekday + time,
    staff, position and notes), skipping any that would overlap an existing shift
    or exactly duplicate an open one. Clock times are not copied. Returns the
    number of shifts created."""
    src_start = week_start - timedelta(days=7)
    s = datetime(src_start.year, src_start.month, src_start.day)
    e = s + timedelta(days=7)
    src = db.execute(
        select(Shift).where(Shift.starts_at >= s, Shift.starts_at < e)
        .order_by(Shift.starts_at)
    ).scalars().all()

    created = 0
    for sh in src:
        ns = sh.starts_at + timedelta(days=7)
        ne = sh.ends_at + timedelta(days=7)
        if sh.staff_id is not None:
            if overlaps(db, sh.staff_id, ns, ne) is not None:
                continue
            if approved_timeoff_conflict(db, sh.staff_id, ns, ne) is not None:
                continue          # never copy a shift onto approved time off
        else:
            dup = db.execute(select(Shift).where(
                Shift.staff_id.is_(None), Shift.starts_at == ns, Shift.ends_at == ne,
            )).scalars().first()
            if dup is not None:
                continue
        db.add(Shift(
            staff_id=sh.staff_id, position_id=sh.position_id,
            starts_at=ns, ends_at=ne, role=sh.role, notes=sh.notes,
        ))
        created += 1
    db.commit()
    return created
