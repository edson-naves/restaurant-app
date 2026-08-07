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
from app.models.oltp import (
    Position, Role, SalesForecast, Shift, Staff, SwapRequest, TimeOffRequest,
)
from app.security import sign_session
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

owner_c = TestClient(app); owner_c.cookies.set("staff_id", sign_session(owner.id))
waiter_c = TestClient(app); waiter_c.cookies.set("staff_id", sign_session(waiter.id))
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

# 9. Labor cost & coverage (Phase 4). `sh` now belongs to `other` (6h) after
#    the swap above; give them a wage and set a sales forecast for the week.
d = SessionLocal()
other_row = d.get(Staff, other.id)
prev_wage = other_row.wage_cents
other_row.wage_cents = 2500  # $25.00/hr
d.commit(); d.close()

# A non-manager cannot set the forecast.
check(waiter_c.post("/schedule/forecast", follow_redirects=False,
                    data={"week": day, "dates": day, "amounts": "1000"}).status_code == 403,
      "a non-manager cannot set the sales forecast")

# Owner sets a forecast: $1000 on Monday, $500 on Tuesday.
r = owner_c.post("/schedule/forecast", follow_redirects=False, data={
    "week": day,
    "dates": [day, tue],
    "amounts": ["1000", "500"]})
check(r.status_code == 303, "owner sets the sales forecast", r.status_code)
d = SessionLocal()
fc_mon = d.query(SalesForecast).filter(SalesForecast.date == mon).first()
d.close()
check(fc_mon is not None and fc_mon.forecast_cents == 100000,
      "forecast saved as cents ($1000 -> 100000)", fc_mon.forecast_cents if fc_mon else None)

d = SessionLocal()
summary = sched.labor_summary(d, mon)
d.close()
# `other` works 6h @ $25 on Monday (the swapped shift). Cost includes only
# assigned shifts; the open shift adds hours to none of it.
check(summary["cost_cents"] >= 15000, "labor cost includes wage x hours", summary["cost_cents"])
check(summary["forecast_cents"] == 150000, "forecast totals the week ($1500)", summary["forecast_cents"])
expected_pct = round(summary["cost_cents"] / 150000 * 100, 1)
check(summary["pct"] == expected_pct, "labor % = cost / forecast", summary["pct"])
check(summary["open_count"] >= 1, "coverage counts the open shift", summary["open_count"])

# The Labor summary panel shows for a manager, and is hidden from a waiter.
r = owner_c.get(f"/schedule?week={day}")
check("Labor summary" in r.text and "Coverage alerts" in r.text,
      "labor + coverage panels render for a manager")
r = waiter_c.get(f"/schedule?week={day}")
check("Labor summary" not in r.text, "wages/labor stay hidden from a waiter")

# Restore the wage.
d = SessionLocal(); d.get(Staff, other.id).wage_cents = prev_wage; d.commit(); d.close()

# 10. Phase 5 — position/staff filters, day + month views, and copy last week.
p5_created: list[int] = []
def _mk(staff_id, start_dt, end_dt, position_id=None):
    dd = SessionLocal()
    s = Shift(staff_id=staff_id, position_id=position_id, starts_at=start_dt, ends_at=end_dt)
    dd.add(s); dd.commit(); sid = s.id; dd.close()
    p5_created.append(sid)
    return sid

src_week = mon + timedelta(days=7)
tgt_week = mon + timedelta(days=14)
p5_week = mon + timedelta(days=21)
# Clear these future weeks so the counts below are deterministic.
d = SessionLocal()
for sh in d.query(Shift).filter(
    Shift.starts_at >= datetime(src_week.year, src_week.month, src_week.day),
    Shift.starts_at < datetime(p5_week.year, p5_week.month, p5_week.day) + timedelta(days=7),
).all():
    d.delete(sh)
d.commit(); d.close()

# Two shifts on the same day: waiter (Server position) and other (no position).
p5_mon = datetime(p5_week.year, p5_week.month, p5_week.day, 10, 0)
_mk(waiter.id, p5_mon, p5_mon + timedelta(hours=4), position_id=pos_id)
_mk(other.id, p5_mon + timedelta(hours=5), p5_mon + timedelta(hours=8), None)

d = SessionLocal()
def _blocks(cal):
    return [b for b in cal.days[0].blocks if not b.is_timeoff]
cal_all = sched.build_calendar(d, p5_week, [waiter, other], include_open=True)
check(len(_blocks(cal_all)) == 2, "unfiltered week shows both shifts", len(_blocks(cal_all)))
cal_pos = sched.build_calendar(d, p5_week, [waiter, other], include_open=True, position_id=pos_id)
check(len(_blocks(cal_pos)) == 1 and _blocks(cal_pos)[0].shift.staff_id == waiter.id,
      "position filter shows only that position's shift")
cal_who = sched.build_calendar(d, p5_week, [waiter, other], include_open=True, only_staff_id=other.id)
check(len(_blocks(cal_who)) == 1 and _blocks(cal_who)[0].shift.staff_id == other.id,
      "staff filter shows only that person's shift")

day_cal = sched.build_day(d, p5_week, [waiter, other], include_open=True)
check(len(day_cal.days) == 1 and len(_blocks(day_cal)) == 2,
      "day view holds a single day with its shifts")

mv = sched.build_month(d, p5_week, [waiter, other], include_open=True)
month_has_chip = any(
    cell.date == p5_week and cell.chips for wk in mv.weeks for cell in wk
)
check(month_has_chip, "month view places chips on the scheduled day")
d.close()

# Copy last week: clone src_week into tgt_week.
src_mon = datetime(src_week.year, src_week.month, src_week.day, 9, 0)
_mk(waiter.id, src_mon, src_mon + timedelta(hours=5), position_id=pos_id)
d = SessionLocal(); n = sched.copy_last_week(d, tgt_week); d.close()
check(n == 1, "copy last week clones the source week's shift", n)
d = SessionLocal()
copied_sh = d.query(Shift).filter(
    Shift.staff_id == waiter.id, Shift.starts_at == src_mon + timedelta(days=7)
).first()
if copied_sh:
    p5_created.append(copied_sh.id)
d.close()
check(copied_sh is not None, "the cloned shift lands a week later at the same time")
d = SessionLocal(); n2 = sched.copy_last_week(d, tgt_week); d.close()
check(n2 == 0, "copy last week skips shifts that would clash", n2)

# Routes: the day and month views load; a non-manager cannot copy a week.
check(owner_c.get(f"/schedule?view=day&date={p5_week.isoformat()}").status_code == 200,
      "day view route loads")
check(owner_c.get(f"/schedule?view=month&date={p5_week.isoformat()}").status_code == 200,
      "month view route loads")
check(waiter_c.post("/schedule/copy-week", data={"week": day}, follow_redirects=False).status_code == 403,
      "a non-manager cannot copy last week")

# 11. Time off vs a shift on the same day: a soft conflict (409) the manager can
#     override with force=1 ("proceed anyway" — the plan may have changed).
from app.models.oltp import TimeOffRequest as _TOR, TimeOffStatus as _TOS
cf_mon = mon + timedelta(days=28)
cf_day = cf_mon + timedelta(days=2)
cf0 = datetime(cf_day.year, cf_day.month, cf_day.day)
cf_data = {"staff_id": waiter.id, "date": cf_day.isoformat(), "start": "10:00", "end": "14:00"}

def _cf_clean():
    d = SessionLocal()
    for x in d.query(Shift).filter(Shift.staff_id == waiter.id, Shift.starts_at >= cf0, Shift.starts_at < cf0 + timedelta(days=1)).all():
        d.delete(x)
    d.query(_TOR).filter(_TOR.staff_id == waiter.id, _TOR.reason == "conflict-qa").delete()
    d.commit(); d.close()

_cf_clean()
# approved time off that day → scheduling warns (409); force proceeds
d = SessionLocal()
d.add(_TOR(staff_id=waiter.id, starts_at=cf0, ends_at=cf0 + timedelta(hours=23, minutes=59),
           status=_TOS.APPROVED, reason="conflict-qa"))
d.commit(); d.close()
check(owner_c.post("/schedule/shifts", follow_redirects=False, data=cf_data).status_code == 409,
      "scheduling onto approved time off warns (409)")
check(owner_c.post("/schedule/shifts", follow_redirects=False, data={**cf_data, "force": 1}).status_code == 303,
      "force schedules the shift anyway")
_cf_clean()

# a shift exists → approving time off over it warns (409); force proceeds
check(owner_c.post("/schedule/shifts", follow_redirects=False, data=cf_data).status_code == 303,
      "shift schedules fine when there's no time off")
d = SessionLocal()
tor = _TOR(staff_id=waiter.id, starts_at=cf0, ends_at=cf0 + timedelta(hours=23, minutes=59),
           status=_TOS.PENDING, reason="conflict-qa")
d.add(tor); d.commit(); tor_id = tor.id; d.close()
check(owner_c.post(f"/schedule/timeoff/{tor_id}/approve", follow_redirects=False).status_code == 409,
      "approving time off over a shift warns (409)")
d = SessionLocal(); check(d.get(_TOR, tor_id).status == "pending", "it stays pending until confirmed"); d.close()
check(owner_c.post(f"/schedule/timeoff/{tor_id}/approve", follow_redirects=False, data={"force": 1}).status_code == 303,
      "force approves the time off anyway")
d = SessionLocal(); check(d.get(_TOR, tor_id).status == "approved", "forced approval goes through"); d.close()
_cf_clean()

# ---- cleanup --------------------------------------------------------------
db = SessionLocal()
for sid in p5_created:
    o = db.get(Shift, sid)
    if o:
        db.delete(o)
db.commit(); db.close()
db = SessionLocal()
for fc in db.query(SalesForecast).filter(SalesForecast.date.in_([mon, mon + timedelta(days=1)])).all():
    db.delete(fc)
db.commit(); db.close()
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
