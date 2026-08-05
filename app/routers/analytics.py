"""Reports & Analytics routes — section 4.3, plus the end-of-day flow (6.3)."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import reports
from app.database import get_db
from app.deps import render, require
from app.etl import run_etl
from app.models.oltp import AuditEvent, DayClose, MenuCategory, Staff
from app.services import closeout
from app.services.money import money

router = APIRouter(prefix="/reports")


def _range(start: str | None, end: str | None, db: Session) -> tuple[date, date]:
    lo, hi = reports.date_bounds(db)
    try:
        s = datetime.strptime(start, "%Y-%m-%d").date() if start else lo
    except ValueError:
        s = lo
    try:
        e = datetime.strptime(end, "%Y-%m-%d").date() if end else hi
    except ValueError:
        e = hi
    if s > e:
        s, e = e, s
    return s, e


@router.get("")
def hub(
    request: Request,
    start: str = None,
    end: str = None,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    s, e = _range(start, end, db)
    summary_day = reports.busiest_day(db, s, e)
    return render(request, "reports_hub.html", {
        "db": db, "staff": staff, "start": s, "end": e,
        "trend": reports.revenue_trend(db, s, e),
        "channels": reports.channel_comparison(db, s, e),
        "payments": reports.payment_breakdown(db, s, e),
        "top": reports.best_sellers(db, s, e, limit=5),
        "staff_rows": reports.staff_performance(db, s, e),
        "swaps_by_staff": reports.swap_requests_by_staff(db, s, e),
        "missed_by_staff": reports.missed_shifts_by_staff(db, s, e),
        "busiest": summary_day,
        "title": "Reports & analytics",
    })


@router.get("/daily")
def daily(
    request: Request,
    day: str = None,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    """4.3.1 — daily sales summary."""
    lo, hi = reports.date_bounds(db)
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date() if day else hi
    except ValueError:
        d = hi
    s = reports.daily_sales_summary(db, d)
    peak = max((h["orders"] for h in s["by_hour"]), default=0)
    return render(request, "report_daily.html", {
        "db": db, "staff": staff, "s": s, "day": d, "peak": peak,
        "prev": d - timedelta(days=1), "next": d + timedelta(days=1),
        "lo": lo, "hi": hi, "title": "Daily sales summary",
    })


@router.get("/best-sellers")
def best_sellers(
    request: Request,
    start: str = None,
    end: str = None,
    category: str = "",
    channel: str = "",
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    """4.3.2 — filterable by date range, category and channel."""
    s, e = _range(start, end, db)
    rows = reports.best_sellers(
        db, s, e, category=category or None, channel_type=channel or None, limit=30
    )
    cats = db.execute(select(MenuCategory).order_by(MenuCategory.sort_order)).scalars().all()
    top = max((r["quantity"] for r in rows), default=0)
    return render(request, "report_items.html", {
        "db": db, "staff": staff, "rows": rows, "start": s, "end": e,
        "categories": cats, "category": category, "channel": channel, "top": top,
        "title": "Best selling items",
    })


@router.get("/payments")
def payments(
    request: Request,
    start: str = None,
    end: str = None,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    """4.3.3 — payment breakdown with card-level detail."""
    s, e = _range(start, end, db)
    return render(request, "report_payments.html", {
        "db": db, "staff": staff, "pb": reports.payment_breakdown(db, s, e),
        "start": s, "end": e, "title": "Payment breakdown",
    })


@router.get("/staff")
def staff_report(
    request: Request,
    start: str = None,
    end: str = None,
    shift: str = "",
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    """4.3.4 — staff performance, filterable by date and shift."""
    s, e = _range(start, end, db)
    rows = reports.staff_performance(db, s, e, shift=shift or None)
    return render(request, "report_staff.html", {
        "db": db, "staff": staff, "rows": rows, "start": s, "end": e,
        "shift": shift, "shifts": ["Morning", "Afternoon", "Evening", "Late"],
        "top": max((r["revenue_cents"] for r in rows), default=0),
        "title": "Staff performance",
    })


@router.get("/channels")
def channels(
    request: Request,
    start: str = None,
    end: str = None,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    """4.3.5 — delivery vs dine-in, and platform breakdown."""
    s, e = _range(start, end, db)
    cc = reports.channel_comparison(db, s, e)
    return render(request, "report_channels.html", {
        "db": db, "staff": staff, "cc": cc, "start": s, "end": e,
        "top": max((r["revenue_cents"] for r in cc["by_platform"]), default=0),
        "title": "Delivery vs dine-in",
    })


@router.get("/export.csv")
def export_csv(
    start: str = None,
    end: str = None,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    """4.3.3 / 6.3 step 5 — payment history CSV for accounting reconciliation.

    One row per item-to-instrument allocation, which is the grain accounting
    needs: it shows exactly which instrument covered which item.
    """
    s, e = _range(start, end, db)
    rows = reports.payment_history_rows(db, s, e)

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([
        "date", "hour", "order_code", "payment_id", "channel", "item",
        "instrument", "card_brand", "card_last4", "staff", "seat",
        "amount", "discount", "tip", "total", "partial_close",
    ])
    for r in rows:
        w.writerow([
            r[0].isoformat(), r[1], r[2], r[3], r[4], r[5], r[6], r[7] or "",
            r[8] or "", r[9], r[10] if r[10] is not None else "",
            f"{r[11]/100:.2f}", f"{r[12]/100:.2f}", f"{r[13]/100:.2f}",
            f"{r[14]/100:.2f}", "yes" if r[15] else "no",
        ])
    buf.seek(0)
    fname = f"payment_history_{s.isoformat()}_to_{e.isoformat()}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/refresh")
def refresh(
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    """Run the ETL so newly closed orders appear in the reports."""
    run_etl(db, full_refresh=False, verbose=False)
    return RedirectResponse("/reports", status_code=303)


# --------------------------------------------------------------------------
# End-of-day close-out — workflow 6.3 (Z-report)
# --------------------------------------------------------------------------

def _cents(value: str, field: str = "Amount") -> int:
    """Parse a typed currency string into integer cents (no float rounding)."""
    raw = value.strip().replace("$", "").replace(",", "")
    if not raw:
        return 0
    whole, _, frac = raw.partition(".")
    whole = whole or "0"
    if not whole.isdigit() or (frac and not frac.isdigit()) or len(frac) > 2:
        raise HTTPException(400, f"{field} '{value}' is not a valid amount.")
    return int(whole) * 100 + int(frac.ljust(2, "0") or 0)


@router.get("/close")
def close_page(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    pending = closeout.compute_pending(db)
    history = db.execute(
        select(DayClose).order_by(DayClose.closed_at.desc()).limit(20)
    ).scalars().all()
    return render(request, "report_close.html", {
        "db": db, "staff": staff, "pending": pending, "history": history,
        "title": "End-of-day close",
    })


@router.post("/close")
def do_close(
    request: Request,
    opening_float: str = Form("0"),
    counted_cash: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    close = closeout.record_close(
        db, staff,
        opening_float_cents=_cents(opening_float, "Opening float"),
        counted_cash_cents=_cents(counted_cash, "Counted cash"),
        notes=notes,
    )
    return RedirectResponse(f"/reports/close/{close.id}", status_code=303)


@router.get("/close/{close_id}")
def close_view(
    close_id: int,
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    close = db.get(DayClose, close_id)
    if close is None:
        raise HTTPException(404, "Close not found")
    return render(request, "report_close_view.html", {
        "db": db, "staff": staff, "close": close,
        "rows": closeout.by_instrument(close),
        "title": f"Z-report #{close.id}",
    })


@router.get("/activity")
def activity(
    request: Request,
    db: Session = Depends(get_db),
    staff: Staff = Depends(require("reports.view")),
):
    """The table-action audit trail (4.1.1) — moves and merges, newest first."""
    events = db.execute(
        select(AuditEvent).order_by(AuditEvent.at.desc()).limit(200)
    ).scalars().all()
    return render(request, "report_activity.html", {
        "db": db, "staff": staff, "events": events,
        "title": "Activity log",
    })
