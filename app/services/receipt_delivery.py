"""Send a receipt by email or SMS (section 4.2.7).

There is no mail or SMS provider wired in. Rather than couple the app to an
external service that needs credentials, delivery goes to a local **outbox**: an
emailed receipt is written as a file under ``outbox/email/`` and a texted one
under ``outbox/sms/``. This is fully functional offline and is the seam where a
real transport drops in — replace ``_dispatch`` with SMTP / Twilio and nothing
else changes.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.oltp import Receipt, ReceiptDelivery, Staff
from app.services import settings as settings_svc

OUTBOX = Path(__file__).resolve().parent.parent.parent / "outbox"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DeliveryError(Exception):
    pass


def _validate(method: str, destination: str) -> str:
    method = (method or "").strip().lower()
    destination = (destination or "").strip()
    if method not in ("email", "sms"):
        raise DeliveryError("Choose email or SMS.")
    if not destination:
        raise DeliveryError("A destination is required.")
    if method == "email" and not EMAIL_RE.match(destination):
        raise DeliveryError(f"'{destination}' is not a valid email address.")
    if method == "sms":
        digits = re.sub(r"[\s()+-]", "", destination)
        if not (digits.isdigit() and 7 <= len(digits) <= 15):
            raise DeliveryError(f"'{destination}' is not a valid phone number.")
    return destination


def render_text(db: Session, receipt: Receipt) -> str:
    """Plain-text version of the receipt — what gets emailed or texted."""
    d = json.loads(receipt.payload_json or "{}")
    cfg = settings_svc.all_settings(db)
    addr = cfg["biz_address"] + (f" · {cfg['biz_postal']}" if cfg.get("biz_postal") else "")
    lines = [cfg["biz_name"], addr, cfg["biz_phone"], "-" * 32]
    if d.get("table"):
        lines.append(f"Table {d['table']}")
    lines.append(f"Order {d.get('order_code', '')}   {d.get('issued_at', '').replace('T', ' ')}")
    lines.append(f"Server: {d.get('served_by', '-')}")
    lines.append("-" * 32)
    for line in d.get("lines", []):
        if line.get("combo"):
            lines.append(f"{line['item']:<24}{line['amount']:>8}")
            for part in line.get("parts", []):
                lines.append(f"  · {part}")
            continue
        name = line["item"] + (" (shared)" if line.get("shared") else "")
        lines.append(f"{line['qty']}x {name:<22}{line['amount']:>8}")
    lines.append("-" * 32)
    lines.append(f"{'Subtotal':<24}{d.get('subtotal', ''):>8}")
    if d.get("discount") and d["discount"] != "0.00":
        lines.append(f"{'Discount':<24}{'-' + d['discount']:>8}")
    if "gst" in d:
        lines.append(f"{'GST (' + str(d.get('gst_rate', '')) + '%)':<24}{d['gst']:>8}")
        if d.get("has_pst"):
            lines.append(f"{'PST (' + str(d.get('pst_rate', '')) + '%)':<24}{d['pst']:>8}")
    elif "tax" in d:
        lines.append(f"{'Tax':<24}{d['tax']:>8}")
    if d.get("tip") and d["tip"] != "0.00":
        lines.append(f"{'Tip':<24}{d['tip']:>8}")
    lines.append(f"{'TOTAL':<24}{d.get('total', ''):>8}")
    lines.append("-" * 32)
    lines.append("Thank you!")
    return "\n".join(lines)


def _dispatch(method: str, destination: str, body: str) -> str:
    """Deliver the message. Returns a detail string (outbox path).

    Swap this for a real SMTP / SMS client to go live. The signature is all a
    transport needs.
    """
    folder = OUTBOX / method
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe = re.sub(r"[^A-Za-z0-9._@-]", "_", destination)
    path = folder / f"{stamp}_{safe}.txt"
    header = (
        f"To: {destination}\n"
        f"Subject: Your receipt\n\n" if method == "email" else f"To: {destination}\n\n"
    )
    path.write_text(header + body, encoding="utf-8")
    return str(path)


def send(db: Session, receipt: Receipt, method: str, destination: str,
         staff: Staff | None) -> ReceiptDelivery:
    """Validate, dispatch, and record a receipt send. Commits."""
    destination = _validate(method, destination)
    body = render_text(db, receipt)
    try:
        detail = _dispatch(method.lower(), destination, body)
        status = "sent"
    except Exception as e:                       # a real transport can fail
        detail, status = str(e)[:300], "failed"

    delivery = ReceiptDelivery(
        receipt_id=receipt.id,
        method=method.lower(),
        destination=destination,
        status=status,
        detail=detail,
        sent_by_id=staff.id if staff else None,
    )
    # Reflect the most recent send on the receipt itself (the model's fields).
    receipt.delivery_method = method.lower()
    receipt.destination = destination
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    if status == "failed":
        raise DeliveryError(detail)
    return delivery
