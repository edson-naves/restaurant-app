"""Stage 1 regression tests — production config fails closed.

Scope: ``app.config.validate_startup_config()`` and the APP_ENV policy. This
file does NOT re-test the session-key guard: ``app.security`` owns that, and
``tests/test_secret_key.py`` proves it across 17 cases, each in a subprocess
with a cleaned environment. The two guards are independent and are proved
independently — the earlier version of this file asserted both in one test and
encoded a policy that no longer holds.

One sentinel below is deliberately cross-cutting: it asserts that the deployed
guard still refuses the public development key now that Stage 1 sits beside it.
That is a regression check on the integration, not a re-test of the guard.

Run: python tests/test_config.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _env  # noqa: F401  — declares the test opt-out before app imports

from app import config, security  # noqa: E402

_failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL {label}")


def _clear(*names):
    for n in names:
        os.environ.pop(n, None)


def test_absent_app_env_is_production():
    """Absence is the strict case. A deployment that never declares itself must
    not get the lenient path — that is how a misconfiguration stays silent."""
    _clear("APP_ENV")
    check(config.app_env() == "production", "absent APP_ENV reads as production")
    check(config.is_production(), "absent APP_ENV is production")
    os.environ["APP_ENV"] = "   "
    check(config.app_env() == "production", "blank APP_ENV reads as production")
    check(config.is_production(), "blank APP_ENV is production")
    # app_env() and is_production() must never disagree: the error messages
    # interpolate app_env(), so a split policy prints a self-contradicting
    # message during exactly the outage it is meant to explain.
    os.environ["APP_ENV"] = "development"
    check(not config.is_production() and config.app_env() == "development",
          "explicit development agrees across both functions")


def test_validate_startup_config_requires_secret_in_production():
    """Stage 1's central behaviour, asserted on the config guard alone.

    Note ALLOW_INSECURE_DEV_SECRET is set (by _env) and is deliberately NOT
    honoured here: the opt-out belongs to the signing key, not to deployment
    configuration validation."""
    _clear("SECRET_KEY")
    os.environ["APP_ENV"] = "production"
    raised = False
    try:
        config.validate_startup_config()
    except config.ConfigError:
        raised = True
    check(raised, "validate_startup_config raises without SECRET_KEY in production")

    # The same call is a no-op outside production, which is what lets the suite run.
    os.environ["APP_ENV"] = "test"
    try:
        config.validate_startup_config()
        check(True, "validate_startup_config is a no-op outside production")
    except config.ConfigError:
        check(False, "validate_startup_config is a no-op outside production")


def test_production_with_secret_ok():
    os.environ["APP_ENV"] = "production"
    os.environ["SECRET_KEY"] = "x" * 48
    _clear("SQUARE_ACCESS_TOKEN", "SQUARE_LOCATION_ID", "SQUARE_DEVICE_ID", "COOKIE_SECURE")
    warnings = config.validate_startup_config()
    # Cash-only + no cookie-secure produce warnings, not errors.
    check(any("Square" in w for w in warnings), "warns when Square unconfigured")
    check(any("COOKIE_SECURE" in w for w in warnings), "warns when COOKIE_SECURE unset")


def test_partial_square_fails_closed():
    os.environ["APP_ENV"] = "production"
    os.environ["SECRET_KEY"] = "x" * 48
    os.environ["SQUARE_ACCESS_TOKEN"] = "tok"
    _clear("SQUARE_LOCATION_ID", "SQUARE_DEVICE_ID")
    raised = False
    try:
        config.validate_startup_config()
    except config.ConfigError as exc:
        raised = "Square is partially configured" in str(exc)
    check(raised, "partial Square config aborts startup")


def test_deployed_secret_guard_survived_stage1():
    """Integration sentinel for the SECRET_KEY hardening.

    Stage 1 shipped its own, weaker _secret(). It was deliberately not taken —
    app/security.py is preserved byte-for-byte from the pre-integration main.
    If a future merge ever restores Stage 1's version, this fails: that version
    accepts the public development key outside production, and accepts a key
    below the length floor anywhere."""
    os.environ["SECRET_KEY"] = security._DEV_SECRET
    raised = False
    try:
        security._secret()
    except security.InsecureSecretKey:
        raised = True
    check(raised, "the deployed guard still refuses the public development key")

    os.environ["SECRET_KEY"] = "short"
    raised = False
    try:
        security._secret()
    except security.InsecureSecretKey:
        raised = True
    check(raised, "the deployed guard still enforces the length floor")
    _clear("SECRET_KEY")


if __name__ == "__main__":
    try:
        test_absent_app_env_is_production()
        test_validate_startup_config_requires_secret_in_production()
        test_production_with_secret_ok()
        test_partial_square_fails_closed()
        test_deployed_secret_guard_survived_stage1()
    finally:
        # Leave the environment clean for any test run after this one.
        _clear("APP_ENV", "SECRET_KEY", "SQUARE_ACCESS_TOKEN", "SQUARE_LOCATION_ID",
               "SQUARE_DEVICE_ID", "COOKIE_SECURE")
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall config tests passed")
