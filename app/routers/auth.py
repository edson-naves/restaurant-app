"""Sign-in / sign-out — section 3, section 5 (security).

A PIN-pad login: pick your name, enter your PIN. On success the staff id is
stored in the session cookie every other route reads via deps.current_staff.
These routes deliberately do not depend on current_staff, so the login page is
reachable when signed out (no redirect loop).

Authentication itself was out of scope for v1's first pass; this closes that
gap. PINs are compared as stored — hashing them is the obvious next hardening
step, noted but not done here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import templates
from app.models.oltp import Role, Staff

router = APIRouter()

COOKIE = "staff_id"
MAX_AGE = 60 * 60 * 12          # a 12-hour shift (the default)
REMEMBER_AGE = 60 * 60 * 24 * 30  # "Remember me on this device" — 30 days


def _active_staff(db: Session) -> list[Staff]:
    return db.execute(
        select(Staff).where(Staff.is_active.is_(True)).order_by(Staff.role, Staff.name)
    ).scalars().all()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = "", db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "login.html", {
        "staff": _active_staff(db), "error": error, "title": "Sign in",
    })


@router.post("/login")
def do_login(
    staff_id: int = Form(...),
    pin: str = Form(...),
    remember: str = Form(""),
    db: Session = Depends(get_db),
):
    person = db.get(Staff, staff_id)
    ok = person is not None and person.is_active and person.pin_code == pin.strip()
    if not ok:
        return RedirectResponse("/login?error=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    # "Remember me on this device" keeps the session for 30 days; otherwise it
    # lasts a single 12-hour shift.
    max_age = REMEMBER_AGE if remember else MAX_AGE
    resp.set_cookie(COOKIE, str(person.id), max_age=max_age, httponly=True, samesite="lax")
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
