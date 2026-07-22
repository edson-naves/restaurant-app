"""System configuration the owner sets up (section 3: settings, owner-only).

Reads the flat Setting key/value store and returns typed, defaulted values, so
callers never deal with missing rows or string parsing. The defaults reproduce
the behaviour that was previously hard-coded in money.py, so an un-configured
system keeps working: GST 5%, no PST, placeholder identity.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.oltp import Setting

DEFAULTS: dict[str, str] = {
    "gst_rate": "5",
    "gst_number": "R000000000 RT0001",
    "pst_rate": "0",
    "pst_number": "",
    "biz_name": "THE RESTAURANT",
    "biz_address": "53 Water St. · Anytown",
    "biz_postal": "",
    "biz_phone": "000-000-0000",
}

# What the settings form is allowed to write. Anything else is ignored, so a
# crafted POST cannot set arbitrary keys.
EDITABLE = tuple(DEFAULTS.keys())


@dataclass(frozen=True)
class TaxConfig:
    gst_rate: float
    gst_number: str
    pst_rate: float
    pst_number: str

    @property
    def total_rate(self) -> float:
        return self.gst_rate + self.pst_rate


def _all(db: Session) -> dict[str, str]:
    stored = {s.key: s.value for s in db.execute(select(Setting)).scalars().all()}
    return {**DEFAULTS, **stored}


def get(db: Session, key: str) -> str:
    return _all(db).get(key, "")


def all_settings(db: Session) -> dict[str, str]:
    return _all(db)


def _rate(raw: str) -> float:
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def tax_config(db: Session) -> TaxConfig:
    s = _all(db)
    return TaxConfig(
        gst_rate=_rate(s["gst_rate"]),
        gst_number=s["gst_number"].strip(),
        pst_rate=_rate(s["pst_rate"]),
        pst_number=s["pst_number"].strip(),
    )


def save(db: Session, values: dict[str, str]) -> None:
    """Persist the editable settings. Unknown keys are dropped."""
    existing = {s.key: s for s in db.execute(select(Setting)).scalars().all()}
    for key in EDITABLE:
        if key not in values:
            continue
        val = (values[key] or "").strip()
        if key in existing:
            existing[key].value = val
        else:
            db.add(Setting(key=key, value=val))
    db.commit()
