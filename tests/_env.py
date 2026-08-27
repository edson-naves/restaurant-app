"""Test-suite environment — import this BEFORE any ``app`` import.

Two production defaults have to be opted out of before the app can be imported
by a test.

``app.security`` refuses to import without a usable ``SECRET_KEY`` (see the
fail-closed rationale there). Separately, ``app.config.app_env()`` treats an
absent ``APP_ENV`` as production, so ``app.main`` would call
``validate_startup_config()`` and demand a real ``SECRET_KEY`` — and that guard
deliberately does NOT honour ``ALLOW_INSECURE_DEV_SECRET``, because the two
guards protect different things (a signing key, and a deployment's
configuration). Declaring ``APP_ENV=test`` is what lets the nine entrypoints
that import ``app.main`` load at all. The suites are run directly as scripts
(``python tests/test_x.py``), so there is no pytest ``conftest.py`` to hook and no
package ``__init__`` to execute — this module is the single place that declares
the opt-out for local test runs.

``setdefault`` rather than assignment: a caller who exports a real ``SECRET_KEY``
keeps it, so the same suites can be pointed at a production-shaped environment
without editing them.

Only the twelve entrypoints that actually reach ``app.security`` import this. The
others are deliberately left alone: if one of them grows an import that pulls
security in, the failure should be visible and say so, rather than being
pre-silenced here.

``tests/test_secret_key.py`` must NOT import this — it owns the very variables
under test, and a global opt-out would make the "no opt-out" cases untestable.
"""
import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ALLOW_INSECURE_DEV_SECRET", "1")
