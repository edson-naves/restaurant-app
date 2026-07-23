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
    # Auto-gratuity for large parties (4.2.6). Party size of 0 disables it.
    "auto_gratuity_party": "0",
    "auto_gratuity_rate": "18",
    # Mandatory house service charge on every bill (4.2.6). 0 disables it.
    "service_charge_rate": "0",
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


@dataclass(frozen=True)
class GratuityConfig:
    """Auto-gratuity policy for large parties (4.2.6)."""
    party_threshold: int   # minimum guests to trigger; 0 = disabled
    rate: float            # percent added as gratuity

    def applies(self, guest_count: int) -> bool:
        return self.party_threshold > 0 and guest_count >= self.party_threshold


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


def gratuity_config(db: Session) -> GratuityConfig:
    s = _all(db)
    try:
        party = max(0, int(float(s["auto_gratuity_party"])))
    except (TypeError, ValueError):
        party = 0
    return GratuityConfig(party_threshold=party, rate=_rate(s["auto_gratuity_rate"]))


def service_charge_rate(db: Session) -> float:
    """The mandatory house service-charge percent (0 = off)."""
    return _rate(_all(db)["service_charge_rate"])


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
