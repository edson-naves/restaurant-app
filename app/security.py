"""Session signing and PIN hashing — standard library only, no crypto deps.

Two jobs, both previously left as v1 placeholders:

1. **Signed session cookie.** The login cookie is a staff id *plus an HMAC-SHA256
   signature*. The browser can read the id but cannot forge the signature, so a
   guest can no longer become the owner by editing ``staff_id`` in their cookie.
   ``verify_session`` returns the id only when the signature checks out.

2. **Hashed PINs.** PINs are stored as salted PBKDF2-HMAC-SHA256 hashes, never in
   plaintext. ``verify_pin`` also accepts a legacy plaintext value so existing
   rows keep working; the caller upgrades them to a hash on the next successful
   login (see ``is_legacy_pin``).

The secret comes from ``SECRET_KEY`` in the environment (set it on Render and in
.env). With nothing set it falls back to a fixed development key so the app runs
zero-config locally — that key is public and MUST NOT be used in production. A
real deployment sets ``SECRET_KEY`` to a long random value; rotating it simply
signs everyone out (their next request fails verification and redirects to
/login), which is the safe failure mode.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

# Public, non-secret default so local dev needs no configuration. Production
# overrides it with a real SECRET_KEY; if it is ever left as this in prod, the
# sessions are only as safe as a value printed in the source — hence the name.
_DEV_SECRET = "dev-insecure-secret-set-SECRET_KEY-in-production"


def _secret() -> bytes:
    # Read at call time so a changed env var takes effect without touching import
    # order, mirroring how the Square client reads its config.
    return (os.environ.get("SECRET_KEY") or _DEV_SECRET).encode("utf-8")


# --------------------------------------------------------------------------
# Session cookie signing
# --------------------------------------------------------------------------

def sign_session(staff_id: int) -> str:
    """Signed cookie value for a staff id: ``"<id>.<hex-hmac>"``."""
    msg = str(int(staff_id))
    sig = hmac.new(_secret(), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def verify_session(value: str | None) -> int | None:
    """The staff id from a signed cookie, or None if missing/tampered.

    Constant-time signature comparison, so a forged cookie cannot be probed
    byte-by-byte.
    """
    if not value or "." not in value:
        return None
    msg, _, sig = value.partition(".")
    if not msg.isdigit():
        return None
    expected = hmac.new(_secret(), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return int(msg)


# --------------------------------------------------------------------------
# PIN hashing
# --------------------------------------------------------------------------

_ALGO = "pbkdf2_sha256"
_ROUNDS = 200_000            # deliberately slow, to blunt brute force of a 4-8 digit PIN
_SALT_BYTES = 16


def hash_pin(pin: str) -> str:
    """Salted PBKDF2 hash of a PIN: ``"pbkdf2_sha256$rounds$salt$hash"``."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, _ROUNDS)
    return f"{_ALGO}${_ROUNDS}${salt.hex()}${dk.hex()}"


def is_hashed(stored: str) -> bool:
    return bool(stored) and stored.startswith(_ALGO + "$")


def is_legacy_pin(stored: str) -> bool:
    """A stored PIN still in plaintext (pre-hashing) — the caller should upgrade
    it to a hash after a successful login."""
    return bool(stored) and not is_hashed(stored)


def verify_pin(stored: str, provided: str) -> bool:
    """True when ``provided`` matches the stored PIN (hashed or legacy plaintext),
    compared in constant time."""
    if not stored:
        return False
    if is_hashed(stored):
        try:
            _algo, rounds_s, salt_hex, hash_hex = stored.split("$")
            rounds = int(rounds_s)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
        except (ValueError, TypeError):
            return False
        dk = hashlib.pbkdf2_hmac("sha256", provided.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(dk, expected)
    # Legacy plaintext row from before hashing shipped.
    return hmac.compare_digest(stored.encode("utf-8"), provided.encode("utf-8"))
