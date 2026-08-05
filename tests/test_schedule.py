"""Staff scheduling + attendance — behaviour tests.

Drives the real routes with TestClient (staff_id cookie auth, like test_e2e),
plus a couple of pure hours-math checks. Idempotent: it clears any shifts it
would collide with for the test staff this week, and cleans up after itself.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models.oltp import Position, Role, Shift, Staff, SwapRequest, TimeOffRequest
from app.services import schedule as sched

fails = 0
def check(cond, msg, extra=""):
    global fails
    print(("PASS  " if cond else "FAIL  ") + msg + (f"  -> {extra}" if extra else ""))
    if not cond:
        fails += 1

# ---- hours math (no DB) ---------------------------------------------------
tmp = Shift(
    starts_at=datetime(2026, 1, 1, 16, 0), ends_at=datetime(2026, 1, 1, 22, 0),
    clock_in_at=datetime(2026, 1, 1, 16, 5), clock_out_at=datetime(2026, 1, 1, 22, 11),
)
check(sched.shift_hours(tmp) == 6.0, "scheduled hours = 6.0", sched.shift_hours(tmp))
check(abs(sched.worked_hours(tmp) - 6.1) < 0.01, "worked hours from clock times", round(sched.worked_hours(tmp), 3))
check(sched.clock_state(tmp) == "done", "clock state resolves to done")
# A close shift that ends 'before' it starts rolls to the next day.
over = Shift(starts_at=datetime(2026, 1, 1, 20, 0), ends_at=datetime(2026, 1, 2, 2, 0))
check(sched.shift_hours(over) == 6.0, "overnight shift spans midnight (6h)")

# ---- setup ----------------------------------------------------------------
db = SessionLocal()
owner = db.query(Staff).filter(Staff.role == Role.OWNER, Staff.is_active.is_(True)).first()
waiter = db.query(Staff).filter(Staff.role == Role.WAITER, Staff.is_active.is_(True)).first()
other = db.query(Staff).filter(
    Staff.is_active.is_(True), Staff.id.notin_([owner.id, waiter.id])
).first()
server_pos = db.query(Position).filter(Position.name == "Server").first()
check(server_pos is not None, "default positions seeded (Server exists)")
pos_id = server_pos.id if server_pos else 0
mon = sched.monday_of(date.today())
day = mon.isoformat()
win_start = datetime(mon.year, mon.month, mon.day)
win_end = win_start + timedelta(days=7)
# Pre-clean any shifts for these staff (and open shifts) in the test week.
for sh in db.query(Shift).filter(
    Shift.starts_at >= win_start, Shift.starts_at < win_end,
    (Shift.staff_id.in_([waiter.id, other.id]) | Shift.staff_id.is_(None)),
).all():
    db.delete(sh)
db.commit()
db.close()

owner_c = TestClient(app); owner_c.cookies.set("staff_id", str(owner.id))
waiter_c = TestClient(app); waiter_c.cookies.set("staff_id", str(waiter.id))
created: list[int] = []

def week_shift(staff_id):
    d = SessionLocal()
    sh = d.query(Shift).filter(
        Shift.staff_id == staff_id, Shift.starts_at >= win_start, Shift.starts_at < win_end
    ).order_by(Shift.id.desc()).first()
    d.close()
    return sh

# 1. Owner creates a waiter's shift with a position.
r = owner_c.post("/schedule/shifts", follow_redirects=False, data={
    "staff_id": waiter.id, "position_id": pos_id, "date": day,
    "start": "16:00", "end": "22:00", "notes": "dinner"})
check(r.status_code == 303, "owner creates a shift", r.status_code)
sh = week_shift(waiter.id)
check(sh is not None and sched.shift_hours(sh) == 6.0, "shift saved, 6h scheduled")
check(sh is not None and sh.position_id == pos_id, "shift tagged with the position")
if sh:
    created.append(sh.id)

# 2. It shows in the week calendar (block + team panel).
r = owner_c.get(f"/schedule?week={day}")
check(r.status_code == 200 and "16:00" in r.text and waiter.name in r.text,
      "shift appears in the calendar")
check("team-panel" in r.text and (server_pos.color in r.text if server_pos else True),
      "team panel + position colour render")

# 2b. An open (unassigned) shift renders as OPEN SHIFT for managers.
owner_c.post("/schedule/shifts", follow_redirects=False, data={
    "staff_id": 0, "position_id": pos_id, "date": day, "start": "11:00", "end": "15:00"})
d = SessionLocal()
osh_open = d.query(Shift).filter(
    Shift.staff_id.is_(None), Shift.starts_at >= win_start, Shift.starts_at < win_end
).order_by(Shift.id.desc()).first()
d.close()
if osh_open:
    created.append(osh_open.id)
r = owner_c.get(f"/schedule?week={day}")
check(osh_open is not None and "OPEN SHIFT" in r.text, "open shift renders as OPEN SHIFT")

# 3. A waiter cannot create shifts.
r = waiter_c.post("/schedule/shifts", follow_redirects=False, data={
    "staff_id": waiter.id, "date": day, "start": "10:00", "end": "14:00"})
check(r.status_code == 403, "waiter cannot create a shift", r.status_code)

# 4. Another member's shift is hidden from the waiter's own view.
owner_c.post("/schedule/shifts", follow_redirects=False, data={
    "staff_id": other.id, "date": day, "start": "09:00", "end": "15:00"})
osh = week_shift(other.id)
if osh:
    created.append(osh.id)
r = waiter_c.get(f"/schedule?week={day}")
check("16:00" in r.text and "09:00" not in r.text,
      "waiter sees only their own shift, not others'")

# 5. Clock in / out on own shift; forbidden on someone else's.
r = waiter_c.post(f"/schedule/shifts/{sh.id}/clock-in", follow_redirects=False)
check(r.status_code == 303 and week_shift(waiter.id).clock_in_at is not None, "waiter clocks in")
r = waiter_c.post(f"/schedule/shifts/{sh.id}/clock-out", follow_redirects=False)
after = week_shift(waiter.id)
check(r.status_code == 303 and after.clock_out_at is not None, "waiter clocks out")
r = waiter_c.post(f"/schedule/shifts/{osh.id}/clock-in", follow_redirects=False)
check(r.status_code == 403, "waiter cannot clock into another member's shift", r.status_code)

# 5b. Repeat a shift across days (same time on other days).
tue = (mon + timedelta(days=1)).isoformat()
wed = (mon + timedelta(days=2)).isoformat()
r = owner_c.post(f"/schedule/shifts/{sh.id}/repeat", follow_redirects=False,
                 data={"dates": tue + "," + wed})
check(r.status_code == 303, "repeat across days accepted", r.status_code)
d = SessionLocal()
copies = d.query(Shift).filter(
    Shift.staff_id == waiter.id, Shift.starts_at >= win_start, Shift.starts_at < win_end
).all()
for csh in copies:
    if csh.id != sh.id and csh.starts_at.date().isoformat() in (tue, wed):
        created.append(csh.id)
made = {c.starts_at.date().isoformat() for c in copies if c.id != sh.id}
d.close()
check(tue in made and wed in made, "shift copied onto Tue and Wed at the same time", made)

# 6. Overlapping shift for the same staff is rejected.
r = owner_c.post("/schedule/shifts", follow_redirects=False, data={
    "staff_id": waiter.id, "date": day, "start": "18:00", "end": "23:00"})
check(r.status_code == 400, "overlapping shift is rejected", r.status_code)

# 7. Time off: file → pending → approve → shows on the calendar; only managers decide.
tod = date.today() + timedelta(days=3)
r = waiter_c.post("/schedule/timeoff", follow_redirects=False,
                  data={"start_month": tod.month, "start_day": tod.day, "reason": "QA"})
check(r.status_code == 303, "staff files a time-off request", r.status_code)
d = SessionLocal()
req = d.query(TimeOffRequest).filter(TimeOffRequest.staff_id == waiter.id).order_by(TimeOffRequest.id.desc()).first()
d.close()
check(req is not None and req.status == "pending", "time-off request is pending")
check(waiter_c.post(f"/schedule/timeoff/{req.id}/approve", follow_redirects=False).status_code == 403,
      "a non-manager cannot decide time off")
check(owner_c.post(f"/schedule/timeoff/{req.id}/approve", follow_redirects=False).status_code == 303,
      "owner approves the request")
d = SessionLocal(); approved = d.get(TimeOffRequest, req.id).status == "approved"; d.close()
check(approved, "time off is approved")
check("cblock timeoff" in owner_c.get(f"/schedule?week={sched.monday_of(tod).isoformat()}").text,
      "approved time off shows as a band on the calendar")

# 8. Swap: waiter offers their shift to `other`; owner approves → reassigned.
r = waiter_c.post(f"/schedule/shifts/{sh.id}/swap", follow_redirects=False,
                  data={"target_staff_id": other.id})
check(r.status_code == 303, "staff files a swap request", r.status_code)
d = SessionLocal()
sw = d.query(SwapRequest).filter(SwapRequest.shift_id == sh.id).order_by(SwapRequest.id.desc()).first()
d.close()
check(sw is not None and sw.status == "pending", "swap request is pending")
check(waiter_c.post(f"/schedule/swaps/{sw.id}/approve", follow_redirects=False).status_code == 403,
      "a non-manager cannot approve a swap")
check(owner_c.post(f"/schedule/swaps/{sw.id}/approve", follow_redirects=False).status_code == 303,
      "owner approves the swap")
d = SessionLocal(); reassigned = d.get(Shift, sh.id).staff_id == other.id; d.close()
check(reassigned, "approving the swap reassigns the shift to the target")

# ---- cleanup --------------------------------------------------------------
db = SessionLocal()
so = db.get(SwapRequest, sw.id)
if so:
    db.delete(so)
db.commit(); db.close()
db = SessionLocal()
for sid in created:
    o = db.get(Shift, sid)
    if o:
        db.delete(o)
r = db.get(TimeOffRequest, req.id)
if r:
    db.delete(r)
db.commit(); db.close()

print()
print("RESULT:", "schedule behaves to spec" if fails == 0 else f"{fails} FAILURES")
sys.exit(1 if fails else 0)
