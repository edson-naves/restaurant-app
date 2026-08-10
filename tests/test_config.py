"""Stage 1 regression tests — production config fails closed.

Covers audit findings #17 (no public dev SECRET_KEY in production) and #16/#20
supporting behaviour. Run: python tests/test_config.py
"""
import os
import sys

# Import the app package regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def test_dev_allows_fallback_secret():
    _clear("APP_ENV", "SECRET_KEY")
    os.environ["APP_ENV"] = "development"
    check(not config.is_production(), "development is not production")
    # Dev may use the public fallback key without raising.
    check(security._secret() == security._DEV_SECRET.encode(), "dev uses fallback key")


def test_production_without_secret_fails_closed():
    _clear("SECRET_KEY")
    os.environ["APP_ENV"] = "production"
    check(config.is_production(), "production is production")
    # startup validation raises
    raised = False
    try:
        config.validate_startup_config()
    except config.ConfigError:
        raised = True
    check(raised, "validate_startup_config raises without SECRET_KEY in prod")
    # defence in depth: signing also refuses
    raised2 = False
    try:
        security._secret()
    except config.ConfigError:
        raised2 = True
    check(raised2, "_secret() refuses the dev key in production")


def test_production_with_secret_ok():
    os.environ["APP_ENV"] = "production"
    os.environ["SECRET_KEY"] = "x" * 48
    _clear("SQUARE_ACCESS_TOKEN", "SQUARE_LOCATION_ID", "SQUARE_DEVICE_ID", "COOKIE_SECURE")
    warnings = config.validate_startup_config()
    check(security._secret() == (b"x" * 48), "prod uses the real SECRET_KEY")
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


if __name__ == "__main__":
    try:
        test_dev_allows_fallback_secret()
        test_production_without_secret_fails_closed()
        test_production_with_secret_ok()
        test_partial_square_fails_closed()
    finally:
        # Leave the environment clean for any test run after this one.
        _clear("APP_ENV", "SECRET_KEY", "SQUARE_ACCESS_TOKEN", "SQUARE_LOCATION_ID",
               "SQUARE_DEVICE_ID", "COOKIE_SECURE")
    if _failures:
        print(f"\n{len(_failures)} FAILED")
        sys.exit(1)
    print("\nall config tests passed")
