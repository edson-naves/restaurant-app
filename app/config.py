"""Environment/config helpers and startup validation.

Kept dependency-free (stdlib only) and importable by ``security.py`` without a
cycle: this module never imports app code. ``validate_startup_config`` is called
once from ``main.py`` so a misconfigured production process fails fast and loud
rather than silently running with an insecure session key or half-configured
payment provider.
"""
from __future__ import annotations

import os

# Environments treated as non-production (the public dev session key is allowed).
_DEV_ENVS = {"development", "dev", "local", "test", "testing", "ci"}


def app_env() -> str:
    """The deployment environment, from ``APP_ENV``. Defaults to development so
    the app still runs zero-config locally and in the test suite."""
    return os.environ.get("APP_ENV", "development").strip().lower()


def is_production() -> bool:
    """True for any environment that is not explicitly a dev/test one. Used to
    decide when to fail closed on missing secrets/config."""
    return app_env() not in _DEV_ENVS


def venue_currency() -> str:
    """The venue's operating currency (ISO 4217). The app is single-currency;
    this is the authoritative fallback for a legacy Payment that has no per-row
    currency, e.g. when validating a refund currency. VENUE_CURRENCY wins, then
    SQUARE_CURRENCY, else CAD."""
    return (os.environ.get("VENUE_CURRENCY")
            or os.environ.get("SQUARE_CURRENCY") or "CAD").strip().upper()


class ConfigError(RuntimeError):
    """Raised at startup when required production configuration is missing."""


# Square is optional (a cash-only venue needs none), but a *partial* Square
# config is a deployment mistake — it silently disables card payments. Require
# all or nothing. Note: SQUARE_APPLICATION_ID is deliberately NOT here — the
# server-side Terminal flow (app/services/square.py) never reads it; it is only
# a client-side SDK value. So the required server set is exactly these three.
_SQUARE_REQUIRED = (
    "SQUARE_ACCESS_TOKEN",
    "SQUARE_LOCATION_ID",
    "SQUARE_DEVICE_ID",
)


def validate_startup_config() -> list[str]:
    """Check production configuration. Raises ``ConfigError`` on a fatal problem;
    returns a list of non-fatal warnings. A no-op outside production."""
    warnings: list[str] = []
    if not is_production():
        return warnings

    if not (os.environ.get("SECRET_KEY") or "").strip():
        raise ConfigError(
            "SECRET_KEY is required in production (APP_ENV=%s). Set it to a long "
            "random value, e.g. python -c \"import secrets; "
            "print(secrets.token_urlsafe(48))\"." % app_env()
        )

    if (os.environ.get("COOKIE_SECURE", "").strip() not in ("1", "true", "True")):
        warnings.append(
            "COOKIE_SECURE is not set — session cookies will be sent over plain "
            "HTTP. Set COOKIE_SECURE=1 in production (behind HTTPS)."
        )

    square_set = [v for v in _SQUARE_REQUIRED if (os.environ.get(v) or "").strip()]
    if square_set and len(square_set) != len(_SQUARE_REQUIRED):
        missing = [v for v in _SQUARE_REQUIRED if v not in square_set]
        raise ConfigError(
            "Square is partially configured — card payments would silently fail. "
            "Missing: " + ", ".join(missing) + ". Set all Square variables or none."
        )
    if not square_set:
        warnings.append(
            "Square is not configured (no SQUARE_ACCESS_TOKEN) — card/terminal "
            "payments are unavailable; only cash/other instruments will work."
        )
    return warnings
