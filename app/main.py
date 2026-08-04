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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import migrate
from app.database import Base, engine
from app.deps import WEB_DIR, templates
from app.routers import admin, analytics, auth, pay, reservations, sales, schedule

app = FastAPI(title="Restaurant Management System", version="1.0")

Base.metadata.create_all(engine)
# create_all adds tables, never columns — see migrate.py. Log what changed so a
# schema migration on deploy (esp. on Postgres/Render) is visible and verifiable
# in the service logs rather than silent.
_migrated = migrate.run(engine)
if _migrated:
    print(f"[migrate] applied: {', '.join(_migrated)}", flush=True)
else:
    print("[migrate] schema up to date", flush=True)

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
    return "ok"
