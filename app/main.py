"""Restaurant Management System — application entry point.

Run:  uvicorn app.main:app --reload
"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import migrate
from app.database import Base, engine
from app.deps import WEB_DIR, templates
from app.routers import admin, analytics, auth, pay, sales

app = FastAPI(title="Restaurant Management System", version="1.0")

Base.metadata.create_all(engine)
migrate.run(engine)          # create_all adds tables, never columns — see migrate.py

app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
app.include_router(auth.router)
app.include_router(sales.router)
app.include_router(pay.router)
app.include_router(analytics.router)
app.include_router(admin.router)


@app.exception_handler(StarletteHTTPException)
def http_error(request: Request, exc: StarletteHTTPException):
    """Show errors in the UI rather than as raw JSON — a waiter mid-service
    needs to read why a payment was refused."""
    # Not signed in → send them to the login page (deps.current_staff raises 401).
    if exc.status_code == 401 and not request.headers.get("accept", "").startswith("application/json"):
        return RedirectResponse("/login", status_code=303)
    if request.headers.get("accept", "").startswith("application/json"):
        raise exc
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "status": exc.status_code,
            "detail": exc.detail,
            "title": f"Error {exc.status_code}",
            "can": lambda p: False,
            "all_staff": [],
        },
        status_code=exc.status_code,
    )


@app.get("/healthz", response_class=HTMLResponse)
def healthz():
    return "ok"
