"""Exact integer money arithmetic.

Every split in this system — equal splits (4.2.3), proportional shares of a
shared appetizer (4.2.4), tip allocation across items — divides a total that
rarely divides evenly. Naive rounding either loses cents or invents them, and
at a till that shows up as a drawer that will not reconcile (end-of-day
workflow step 6.3.2).

distribute() is the single place that rounding happens, using the
largest-remainder method: floor everything, then hand the leftover cents to the
largest fractional parts. The result always sums to exactly the input total.
"""
from __future__ import annotations


def distribute(total_cents: int, weights: list[int]) -> list[int]:
    """Split total_cents across len(weights) buckets in proportion to weights.

    Guarantees sum(result) == total_cents exactly. Cents left over from integer
    division go to the buckets with the largest fractional remainder, ties
    broken by position, so the split is deterministic and reproducible.

    A zero weight always receives 0 — a seat that ordered nothing is never
    handed a stray cent.
    """
    n = len(weights)
    if n == 0:
        return []

    total_weight = sum(weights)
    if total_weight <= 0:
        # No basis for proportion: fall back to an even split of the total.
        return split_evenly(total_cents, n)

    exact = [total_cents * w for w in weights]
    base = [e // total_weight for e in exact]
    remainder = total_cents - sum(base)

    # Rank buckets by the fractional part they lost to flooring.
    order = sorted(
        range(n),
        key=lambda i: (-(exact[i] % total_weight), i),
    )
    for i in range(remainder):
        base[order[i % n]] += 1
    return base


def split_evenly(total_cents: int, parts: int) -> list[int]:
    """Split into N equal parts (section 4.2.3), distributing the odd cents.

    e.g. 10.00 across 3 guests -> [3.34, 3.33, 3.33], not 3 x 3.33 with a
    cent unaccounted for.
    """
    if parts <= 0:
        return []
    base = total_cents // parts
    remainder = total_cents - base * parts
    return [base + (1 if i < remainder else 0) for i in range(parts)]


def pct(amount_cents: int, percent: float) -> int:
    """Percentage of an amount, rounded half-up to the nearest cent."""
    return int(amount_cents * percent / 100 + 0.5)


def money(cents: int) -> str:
    return f"{cents / 100:,.2f}"


def duration(minutes: int) -> str:
    """Elapsed minutes as a readable span: 8m, 1h 05m, 2d 3h.

    A table open five days reads "5d 2h", not "7042m" — the unit scales to the
    size so the number is legible at a glance on the floor and kitchen screens.
    Below an hour it stays in whole minutes; from an hour up it drops the
    minutes once days appear, since minute precision is noise at that range.
    """
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return "0m"
    if minutes < 0:
        minutes = 0
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins:02d}m"
    return f"{mins}m"
