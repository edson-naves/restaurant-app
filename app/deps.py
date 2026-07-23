"""Shared dependencies: current user, role gating, templates.

Section 3 defines five roles with distinct permissions. Authentication itself
is out of scope for v1 (the doc assumes staff onboarding and a PIN pad), so the
active user is held in a cookie and switched from the header. The permission
model is real; only the identity check is simplified.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.oltp import Role, Staff
from app.models.oltp import ALLERGEN_OPTIONS, COURSE_LABELS, course_label, seat_color
from app.services.money import duration, money

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["money"] = money
templates.env.filters["duration"] = duration
templates.env.filters["course"] = course_label
templates.env.filters["seat_color"] = seat_color
templates.env.globals["COURSE_LABELS"] = COURSE_LABELS
templates.env.globals["ALLERGEN_OPTIONS"] = ALLERGEN_OPTIONS


def asset_v(name: str = "app.css") -> str:
    """Cache-busting stamp for a static file, from its modification time.

    Browsers cache /static/app.css aggressively and will happily keep serving
    a stale copy after an edit — the page then renders with half its rules
    missing, which looks like a broken layout rather than a caching problem.
    Appending the mtime changes the URL whenever the file changes, so a new
    stylesheet is always fetched and an unchanged one stays cached.
    """
    try:
        return str(int((WEB_DIR / "static" / name).stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["asset_v"] = asset_v

# Section 3 — access matrix.
PERMISSIONS: dict[str, set[str]] = {
    "orders.view":     {Role.OWNER, Role.MANAGER, Role.WAITER, Role.KITCHEN, Role.DELIVERY_COORDINATOR},
    "orders.manage":   {Role.OWNER, Role.MANAGER, Role.WAITER},
    "kitchen.view":    {Role.OWNER, Role.MANAGER, Role.KITCHEN, Role.WAITER},
    "kitchen.update":  {Role.OWNER, Role.MANAGER, Role.KITCHEN},
    "menu.availability": {Role.OWNER, Role.MANAGER, Role.WAITER, Role.KITCHEN},  # 86 an item
    "reservations": {Role.OWNER, Role.MANAGER, Role.WAITER},  # front-of-house 4.1.5
    "delivery.view":   {Role.OWNER, Role.MANAGER, Role.DELIVERY_COORDINATOR, Role.KITCHEN},
    "delivery.update": {Role.OWNER, Role.MANAGER, Role.DELIVERY_COORDINATOR},
    "payments.take":   {Role.OWNER, Role.MANAGER, Role.WAITER},
    "discount.approve": {Role.OWNER, Role.MANAGER},   # 4.2.6
    "reports.view":    {Role.OWNER, Role.MANAGER},
    "staff.manage":    {Role.OWNER},
    "settings":        {Role.OWNER},                  # manager explicitly excluded
}


def current_staff(request: Request, db: Session = Depends(get_db)) -> Staff:
    """The logged-in staff, from the session cookie set at /login.

    No fallback: an unauthenticated request raises 401, which the app turns into
    a redirect to /login (see main.http_error). The cookie is the session — the
    PIN is checked once at login, not on every request — so a member deactivated
    mid-shift keeps their session until it expires or they log out.
    """
    staff_id = request.cookies.get("staff_id")
    staff = None
    if staff_id and staff_id.isdigit():
        staff = db.get(Staff, int(staff_id))
    if staff is None:
        raise HTTPException(401, "Please sign in.")
    return staff


def can(staff: Staff, permission: str) -> bool:
    return staff.role in PERMISSIONS.get(permission, set())


def require(permission: str):
    """Route guard for a named permission."""
    def _guard(staff: Staff = Depends(current_staff)) -> Staff:
        if not can(staff, permission):
            raise HTTPException(
                403,
                f"{staff.name} ({staff.role.replace('_', ' ')}) does not have "
                f"permission for this action.",
            )
        return staff
    return _guard


def render(request: Request, template: str, ctx: dict):
    """Render with the globals every page needs.

    Starlette takes (request, name, context) — the legacy (name, context) form
    silently treats the template name as the request and fails deep inside
    Jinja, so keep the request first.
    """
    db = ctx.get("db")
    staff = ctx.get("staff")
    base = {
        "can": (lambda p: can(staff, p)) if staff else (lambda p: False),
        "Role": Role,
    }
    if db is not None:
        base["all_staff"] = db.execute(
            select(Staff).where(Staff.is_active.is_(True)).order_by(Staff.id)
        ).scalars().all()
    base.update(ctx)
    base.pop("db", None)
    return templates.TemplateResponse(request, template, base)
