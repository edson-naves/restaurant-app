"""Restaurant Management System — application entry point.

Run:  uvicorn app.main:app --reload
"""
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# Local dev reads secrets from .env; on Render the real env vars are already set
# and win (load_dotenv does not override existing values), so this is a no-op
# there. Must run before anything reads os.environ.
load_dotenv()

# Run every date/time calculation in the restaurant's local timezone. Render's
# servers are UTC, so without this "today" flips to tomorrow in the evening for a
# Pacific venue — the schedule's Today button and default day land a day ahead.
# Overridable via the TZ environment variable; tzset applies it on Unix (guarded
# for Windows dev, which has no tzset).
import os as _os
import time as _time
_os.environ.setdefault("TZ", "America/Vancouver")
if hasattr(_time, "tzset"):
    _time.tzset()

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import migrate
from app.config import is_production, validate_startup_config
from app.database import Base, SessionLocal, engine
from app.deps import WEB_DIR, templates
from app.routers import admin, analytics, auth, pay, reservations, sales, schedule

app = FastAPI(title="Restaurant Management System", version="1.0")

# Fail closed on missing production config (SECRET_KEY, partial Square) before
# doing anything else; log non-fatal warnings.
for _warning in validate_startup_config():
    print(f"[config] WARNING: {_warning}", flush=True)

Base.metadata.create_all(engine)
# create_all adds tables, never columns — see migrate.py. Log what changed so a
# schema migration on deploy (esp. on Postgres/Render) is visible and verifiable
# in the service logs rather than silent. In production a real migration failure
# is fatal (strict) rather than silently skipped.
_migrated = migrate.run(engine, strict=is_production())
if _migrated:
    print(f"[migrate] applied: {', '.join(_migrated)}", flush=True)
else:
    print("[migrate] schema up to date", flush=True)

# Seed the default schedule positions once (idempotent) so colour-coding works
# out of the box on a fresh or existing database.
from app.services import schedule as _schedule_svc  # noqa: E402
with SessionLocal() as _db:
    _schedule_svc.ensure_default_positions(_db)

app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
app.include_router(auth.router)
app.include_router(sales.router)
app.include_router(reservations.router)
app.include_router(pay.router)
app.include_router(analytics.router)
app.include_router(admin.router)
app.include_router(schedule.router)


@app.exception_handler(StarletteHTTPException)
def http_error(request: Request, exc: StarletteHTTPException):
    """Show errors in the UI rather than as raw JSON — a waiter mid-service
    needs to read why a payment was refused."""
    # Not signed in → send them to the login page (deps.current_staff raises 401).
    if exc.status_code == 401 and not request.headers.get("accept", "").startswith("application/json"):
        return RedirectResponse("/login", status_code=303)
    if request.headers.get("accept", "").startswith("application/json"):
        raise exc
    # Go back to wherever the action came from (the order screen, usually) rather
    # than always the floor plan — a rejected add/pay should return to the order.
    ref = urlparse(request.headers.get("referer", "")).path
    back = ref if ref.startswith("/") and ref != request.url.path else "/"
    back_label = "Back to order" if back.startswith("/orders/") else "Back to floor plan"
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "status": exc.status_code,
            "detail": exc.detail,
            "back": back,
            "back_label": back_label,
            "title": f"Error {exc.status_code}",
            "can": lambda p: False,
            "all_staff": [],
        },
        status_code=exc.status_code,
    )


@app.get("/healthz", response_class=HTMLResponse)
def healthz():
    """Liveness: the process is up and serving. Does not touch the database, so
    an orchestrator restarts a wedged process without being fooled by a slow DB."""
    return "ok"


@app.get("/readyz", response_class=HTMLResponse)
def readyz():
    """Readiness: prove the app can actually serve traffic — the database is
    reachable and the core schema exists. Returns 503 until it can, so a load
    balancer/orchestrator does not route to an instance that cannot transact.

    The response body is deliberately generic; the underlying error (SQL, driver,
    host, schema) is logged server-side only, never returned to the caller."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            # A core table proves migrations/bootstrap ran, not just that a DB
            # socket answered.
            conn.execute(text('SELECT 1 FROM staff LIMIT 1'))
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger("readyz").exception("readiness check failed")
        return HTMLResponse("not ready", status_code=503)
    return "ready"
