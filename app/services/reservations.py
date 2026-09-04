"""Reservation availability and live floor-plan table holds."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.oltp import Reservation, ReservationStatus, RestaurantTable, TableStatus
from app.services import settings

DurationResolver = Callable[[int], int]


class AvailabilityConflict(str, Enum):
    INVALID_REQUEST = "invalid_request"
    REQUESTED_TABLE_NOT_FOUND = "requested_table_not_found"
    REQUESTED_TABLE_INACTIVE = "requested_table_inactive"
    REQUESTED_TABLE_OCCUPIED = "requested_table_occupied"
    REQUESTED_TABLE_RESERVED = "requested_table_reserved"
    INSUFFICIENT_CAPACITY = "insufficient_capacity"
    NO_SINGLE_TABLE_CAPACITY = "no_single_table_capacity"


@dataclass(frozen=True)
class AvailabilityRequest:
    at: datetime
    party_size: int
    now: datetime
    requested_table_ids: tuple[int, ...] = ()
    exclude_reservation_id: int | None = None
    hold_before_minutes: int = 0
    drop_after_minutes: int = 0
    turnover_minutes: int = 0


@dataclass(frozen=True)
class CandidateAllocation:
    table_ids: tuple[int, ...]
    capacity: int
    unused_capacity: int


@dataclass(frozen=True)
class AvailabilityIssue:
    code: AvailabilityConflict
    table_id: int | None = None
    reservation_id: int | None = None


@dataclass(frozen=True)
class AvailabilityResult:
    available: bool
    duration_minutes: int
    required_from: datetime
    service_ends_at: datetime
    required_until: datetime
    candidates: tuple[CandidateAllocation, ...]
    issues: tuple[AvailabilityIssue, ...]
    unallocated_reservation_ids: tuple[int, ...]
    capacity_is_precise: bool


def _overlaps(start_a: datetime, end_a: datetime,
              start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def _valid_minutes(value: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _expired(reservation: Reservation, now: datetime, drop_after: int) -> bool:
    return (reservation.created_at < reservation.at
            and now > reservation.at + timedelta(minutes=drop_after))


def calculate_availability(
    db: Session,
    request: AvailabilityRequest,
    duration_for: DurationResolver,
) -> AvailabilityResult:
    try:
        duration = duration_for(request.party_size)
    except (TypeError, ValueError):
        duration = 0
    valid = (
        request.party_size > 0 and _valid_minutes(duration) and duration > 0
        and _valid_minutes(request.hold_before_minutes)
        and _valid_minutes(request.drop_after_minutes)
        and _valid_minutes(request.turnover_minutes)
    )
    before = request.hold_before_minutes if _valid_minutes(request.hold_before_minutes) else 0
    required_from = request.at - timedelta(minutes=before)
    service_ends_at = request.at + timedelta(minutes=max(duration, 0))
    release = max(
        max(duration, 0) + (request.turnover_minutes if _valid_minutes(request.turnover_minutes) else 0),
        request.drop_after_minutes if _valid_minutes(request.drop_after_minutes) else 0,
    )
    required_until = request.at + timedelta(minutes=release)
    if not valid:
        return AvailabilityResult(False, duration, required_from, service_ends_at,
                                 required_until, (),
                                 (AvailabilityIssue(AvailabilityConflict.INVALID_REQUEST),),
                                 (), True)

    with db.no_autoflush:
        tables = db.execute(select(RestaurantTable).order_by(
            RestaurantTable.number, RestaurantTable.id)).scalars().all()
        reservations = db.execute(select(Reservation).where(
            Reservation.kind == "reservation",
            Reservation.status == ReservationStatus.WAITING,
        ).order_by(Reservation.at, Reservation.id)).scalars().all()

    table_by_id = {table.id: table for table in tables}
    blocked_by: dict[int, int] = {}
    unallocated: list[int] = []
    for reservation in reservations:
        if reservation.id == request.exclude_reservation_id:
            continue
        if _expired(reservation, request.now, request.drop_after_minutes):
            continue
        if not reservation.tables:
            unallocated.append(reservation.id)
            continue
        try:
            existing_duration = duration_for(reservation.party_size)
        except (TypeError, ValueError):
            existing_duration = 0
        if not _valid_minutes(existing_duration) or existing_duration <= 0:
            existing_duration = duration
        existing_from = reservation.at - timedelta(minutes=request.hold_before_minutes)
        existing_until = reservation.at + timedelta(minutes=max(
            existing_duration + request.turnover_minutes, request.drop_after_minutes))
        if _overlaps(required_from, required_until, existing_from, existing_until):
            for table in reservation.tables:
                if table.is_active:
                    blocked_by.setdefault(table.id, reservation.id)

    issues: list[AvailabilityIssue] = []
    candidates: list[CandidateAllocation] = []
    requested_ids = tuple(dict.fromkeys(request.requested_table_ids))
    if requested_ids:
        requested_tables = []
        for table_id in requested_ids:
            table = table_by_id.get(table_id)
            if table is None:
                issues.append(AvailabilityIssue(AvailabilityConflict.REQUESTED_TABLE_NOT_FOUND,
                                                table_id=table_id))
                continue
            requested_tables.append(table)
            if not table.is_active:
                issues.append(AvailabilityIssue(AvailabilityConflict.REQUESTED_TABLE_INACTIVE,
                                                table_id=table_id))
            elif table.status != TableStatus.FREE:
                issues.append(AvailabilityIssue(AvailabilityConflict.REQUESTED_TABLE_OCCUPIED,
                                                table_id=table_id))
            elif table.id in blocked_by:
                issues.append(AvailabilityIssue(AvailabilityConflict.REQUESTED_TABLE_RESERVED,
                                                table_id=table_id,
                                                reservation_id=blocked_by[table.id]))
        capacity = sum(table.capacity for table in requested_tables if table.is_active)
        if capacity < request.party_size:
            issues.append(AvailabilityIssue(AvailabilityConflict.INSUFFICIENT_CAPACITY))
        if not issues:
            candidates.append(CandidateAllocation(requested_ids, capacity,
                                                  capacity - request.party_size))
    else:
        for table in tables:
            if (table.is_active and table.status == TableStatus.FREE
                    and table.id not in blocked_by
                    and table.capacity >= request.party_size):
                candidates.append(CandidateAllocation((table.id,), table.capacity,
                                                      table.capacity - request.party_size))
        candidates.sort(key=lambda item: (
            item.unused_capacity, len(item.table_ids),
            tuple(table_by_id[i].number for i in item.table_ids), item.table_ids))
        if not candidates:
            total = sum(t.capacity for t in tables if t.is_active
                        and t.status == TableStatus.FREE and t.id not in blocked_by)
            code = (AvailabilityConflict.NO_SINGLE_TABLE_CAPACITY
                    if total >= request.party_size else AvailabilityConflict.INSUFFICIENT_CAPACITY)
            issues.append(AvailabilityIssue(code))

    return AvailabilityResult(bool(candidates) and not issues, duration, required_from,
                              service_ends_at, required_until, tuple(candidates),
                              tuple(issues), tuple(sorted(unallocated)), not unallocated)


def active_holds(db: Session, now: datetime | None = None) -> dict[int, Reservation]:
    now = now or datetime.now()
    hold_before, drop_after = settings.reservation_window(db)
    waiting = db.execute(select(Reservation).where(
        Reservation.kind == "reservation",
        Reservation.status == ReservationStatus.WAITING,
    ).order_by(Reservation.at)).scalars().all()
    holds: dict[int, Reservation] = {}
    dropped = False
    for reservation in waiting:
        if not reservation.tables:
            continue
        late = (now - reservation.at).total_seconds() / 60
        if late > drop_after and reservation.created_at < reservation.at:
            reservation.status = ReservationStatus.NO_SHOW
            dropped = True
            continue
        if late < -hold_before:
            continue
        for table in reservation.tables:
            if table.is_active:
                holds.setdefault(table.id, reservation)
    if dropped:
        db.commit()
    return holds
