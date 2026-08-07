"""Authentication hardening — signed sessions, hashed PINs, the is_active
recheck, and discount-approval enforcement.

Runs in-process against the ASGI app; any state it changes it restores.
    python tests/test_security.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models.oltp import Role, Staff
import os

from app.routers.pay import _discount_approver_id
from app.security import (
    cookie_secure, hash_pin, is_legacy_pin, sign_session, verify_pin, verify_session,
)

fails = 0
def check(cond, msg, extra=""):
    global fails
    print(("PASS  " if cond else "FAIL  ") + msg + (f"  -> {extra}" if extra else ""))
    if not cond:
        fails += 1


# ---- session signing -------------------------------------------------------
signed = sign_session(7)
check(verify_session(signed) == 7, "a signed cookie verifies back to its id")
check(signed.split(".")[0] == "7", "the id travels in the clear (only the sig is secret)")
check(verify_session("7") is None, "an unsigned id is rejected")
check(verify_session("7.deadbeef") is None, "a bogus signature is rejected")
tampered = signed[:-1] + ("0" if signed[-1] != "0" else "1")
check(verify_session(tampered) is None, "a tampered signature is rejected")
# The core escalation: keep a valid signature but swap the id under it.
check(verify_session("1." + signed.split(".")[1]) is None,
      "a different id under a stolen signature is rejected")
check(verify_session(None) is None and verify_session("") is None, "a missing cookie is rejected")

# ---- PIN hashing -----------------------------------------------------------
h = hash_pin("4321")
check(h != "4321" and h.startswith("pbkdf2_sha256$"), "hash_pin never stores plaintext")
check(verify_pin(h, "4321") and not verify_pin(h, "0000"), "verify_pin checks against the hash")
check(verify_pin("4321", "4321") and is_legacy_pin("4321"),
      "a legacy plaintext PIN still verifies (so it can be upgraded on login)")
check(not is_legacy_pin(h), "a hashed PIN is not treated as legacy")
# Regression: the pin_code column must be wide enough for a hash. SQLite ignores
# VARCHAR length, but Postgres enforces it — a too-narrow column overflows on the
# login that upgrades a legacy PIN, which took the live site down once.
_pin_len = Staff.__table__.c.pin_code.type.length
check(_pin_len is None or _pin_len >= len(hash_pin("1234")),
      "the pin_code column fits a full hash", _pin_len)

# ---- Secure cookie flag (HTTPS-only in prod) -------------------------------
_prev = os.environ.get("COOKIE_SECURE")
for val, want in (("", False), ("1", True), ("true", True), ("0", False), ("off", False)):
    if val:
        os.environ["COOKIE_SECURE"] = val
    else:
        os.environ.pop("COOKIE_SECURE", None)
    check(cookie_secure() is want, f"COOKIE_SECURE={val!r} -> secure={want}")
if _prev is None:
    os.environ.pop("COOKIE_SECURE", None)
else:
    os.environ["COOKIE_SECURE"] = _prev

# ---- integration: forged and deactivated sessions --------------------------
db = SessionLocal()
owner = db.query(Staff).filter(Staff.role == Role.OWNER, Staff.is_active.is_(True)).first()
waiter = db.query(Staff).filter(Staff.role == Role.WAITER, Staff.is_active.is_(True)).first()

forged = TestClient(app)
forged.cookies.set("staff_id", str(owner.id))          # unsigned, as an attacker would craft
r = forged.get("/", follow_redirects=False)
check(r.status_code == 303 and r.headers.get("location") == "/login",
      "a forged (unsigned) cookie cannot impersonate the owner")

good = TestClient(app)
good.cookies.set("staff_id", sign_session(owner.id))
check(good.get("/", follow_redirects=False).status_code == 200, "a properly signed cookie reaches the app")

# A deactivated member's still-valid signed cookie must stop working at once.
spare = db.query(Staff).filter(
    Staff.is_active.is_(True), Staff.id.notin_([owner.id, waiter.id])
).first()
spare.is_active = False
db.commit()
c = TestClient(app); c.cookies.set("staff_id", sign_session(spare.id))
check(c.get("/", follow_redirects=False).status_code == 303,
      "a deactivated staff's session is rejected on the next request")
spare.is_active = True
db.commit()

# ---- discount approver enforcement (the 4.2.6 bypass) ----------------------
def raises403(fn):
    try:
        fn()
        return False
    except HTTPException as e:
        return e.status_code == 403

check(_discount_approver_id(db, waiter, None, False) is None,
      "no discount means no approver is needed")
check(raises403(lambda: _discount_approver_id(db, waiter, None, True)),
      "a waiter cannot discount with no approver")
check(raises403(lambda: _discount_approver_id(db, waiter, waiter.id, True)),
      "a waiter cannot self-approve by naming themselves")
check(raises403(lambda: _discount_approver_id(db, waiter, 999999, True)),
      "a bogus approver id is rejected")
check(_discount_approver_id(db, waiter, owner.id, True) == owner.id,
      "a real manager id authorizes the discount")
check(_discount_approver_id(db, owner, None, True) == owner.id,
      "a manager self-approves their own discount")
db.close()

print()
print("RESULT:", "auth hardening holds" if fails == 0 else f"{fails} FAILURES")
sys.exit(1 if fails else 0)
