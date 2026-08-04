"""Staff scheduling helpers — week math, hours roll-ups and the grid model.

Kept out of the router so the date arithmetic and totals are unit-testable and
the route stays thin (same split as services/payments.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.oltp import Shift, Staff


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
# Grid model the template renders
# --------------------------------------------------------------------------

@dataclass
class CellShift:
    """A shift plus its rendered attendance, so the template does no maths."""
    shift: Shift
    state: str                               # 'scheduled' | 'in' | 'done'
    sched_h: float
    worked_h: float


@dataclass
class GridRow:
    staff: Staff
    cells: list[list[CellShift]]             # 7 lists, Monday..Sunday
    sched_hours: float = 0.0
    worked_hours: float = 0.0


@dataclass
class Grid:
    week_start: date
    days: list[date] = field(default_factory=list)
    rows: list[GridRow] = field(default_factory=list)


def build_grid(db: Session, week_start: date, staff_list: list[Staff]) -> Grid:
    """Bucket the week's shifts into a per-staff, per-day grid with row totals."""
    days = week_days(week_start)
    start_dt = datetime(week_start.year, week_start.month, week_start.day)
    end_dt = start_dt + timedelta(days=7)

    ids = [s.id for s in staff_list]
    rows: list[GridRow] = []
    if ids:
        shifts = db.execute(
            select(Shift).where(
                Shift.staff_id.in_(ids),
                Shift.starts_at >= start_dt,
                Shift.starts_at < end_dt,
            ).order_by(Shift.starts_at)
        ).scalars().all()
    else:
        shifts = []

    by_staff: dict[int, list[Shift]] = {}
    for sh in shifts:
        by_staff.setdefault(sh.staff_id, []).append(sh)

    for member in staff_list:
        cells: list[list[CellShift]] = [[] for _ in range(7)]
        sched = worked = 0.0
        for sh in by_staff.get(member.id, []):
            idx = (sh.starts_at.date() - week_start).days
            if 0 <= idx < 7:
                sh_h, wk_h = shift_hours(sh), worked_hours(sh)
                cells[idx].append(CellShift(sh, clock_state(sh), sh_h, wk_h))
                sched += sh_h
                worked += wk_h
        rows.append(GridRow(staff=member, cells=cells, sched_hours=sched, worked_hours=worked))

    return Grid(week_start=week_start, days=days, rows=rows)
