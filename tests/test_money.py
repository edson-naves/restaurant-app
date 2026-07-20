"""The invariant that matters: a split never creates or destroys a cent."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.money import distribute, pct, split_evenly


def check(cond, label):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    return cond


ok = True

# Equal split of an amount that does not divide evenly.
r = split_evenly(1000, 3)
ok &= check(r == [334, 333, 333], f"10.00 / 3 guests -> {r} sums to {sum(r)}")

r = split_evenly(1001, 4)
ok &= check(sum(r) == 1001, f"10.01 / 4 guests sums exactly -> {r}")

# Shared appetizer across seats.
r = distribute(1000, [1, 1, 1])
ok &= check(sum(r) == 1000 and r == [334, 333, 333], f"shared 10.00 across 3 seats -> {r}")

# Weighted (uneven) split.
r = distribute(1000, [2, 1, 1])
ok &= check(sum(r) == 1000, f"weighted 2:1:1 of 10.00 -> {r} sums to {sum(r)}")

# A seat with zero weight must never receive a cent.
r = distribute(1000, [1, 0, 1])
ok &= check(r[1] == 0 and sum(r) == 1000, f"zero-weight seat gets nothing -> {r}")

# Degenerate cases.
ok &= check(distribute(0, [1, 1]) == [0, 0], "zero total -> zeros")
ok &= check(distribute(100, []) == [], "no seats -> empty")
ok &= check(sum(distribute(100, [0, 0])) == 100, "all-zero weights falls back to even split")

# Tip percentages from section 4.2.6.
ok &= check(pct(4550, 15) == 683, f"15% tip on 45.50 -> {pct(4550, 15)/100:.2f}")
ok &= check(pct(4550, 18) == 819, f"18% tip on 45.50 -> {pct(4550, 18)/100:.2f}")
ok &= check(pct(4550, 20) == 910, f"20% tip on 45.50 -> {pct(4550, 20)/100:.2f}")

# Randomized: the sum invariant must hold for any total and any weights.
random.seed(7)
bad = 0
for _ in range(20000):
    total = random.randint(0, 500_00)
    weights = [random.randint(0, 20) for _ in range(random.randint(1, 15))]
    if sum(distribute(total, weights)) != total:
        bad += 1
ok &= check(bad == 0, f"20,000 random splits all sum exactly ({bad} failures)")

# Randomized even splits.
bad = 0
for _ in range(20000):
    total = random.randint(0, 500_00)
    parts = random.randint(1, 30)
    if sum(split_evenly(total, parts)) != total:
        bad += 1
ok &= check(bad == 0, f"20,000 random equal splits all sum exactly ({bad} failures)")

print("\nRESULT:", "all money invariants hold" if ok else "FAILURES PRESENT")
sys.exit(0 if ok else 1)
