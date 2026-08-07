# Working notes for Claude

Guidance to follow in this repo, so it doesn't have to be repeated each session.

## Working style (cost & speed)

- **Match verification to the change:**
  - **Cosmetic only** (CSS, copy, markup, template layout — no Python/logic change):
    compile templates (`.venv/Scripts/python tests/test_templates.py`) and commit.
    Do **not** write throwaway smoke scripts or run the full suite for these.
  - **Logic change** (routers, services, models, JS behaviour): run the relevant
    suite(s); reseed + build the star schema before `test_e2e`/`test_reconciliation`.
- **Batch related edits.** When several tweaks arrive together (or in quick
  succession), do them in **one** edit → verify → commit cycle, not one per tweak.
- Don't over-explain or re-derive things already established; act when the path is clear.
- Prefer editing over rewriting; keep to the surrounding code's style.

## Project facts (so they aren't rediscovered)

- **Stack:** FastAPI + SQLAlchemy 2.0 + Jinja. SQLite for dev/tests, Postgres in prod (Render).
- **Run:** `uvicorn app.main:app`. **Tests:** `tests/test_*.py` run directly with the venv Python.
- **e2e is HTTP against a live server** on `http://127.0.0.1:8079` (`uvicorn app.main:app --port 8079`,
  no `--reload`) — **restart that server after code changes** or it tests stale code.
  Before `test_e2e` / `test_reconciliation`: `python -m app.seed` then
  `run_etl(SessionLocal(), full_refresh=True)` (the seed builds OLTP, not the star schema).
- **Schema:** new **tables** auto-create via `Base.metadata.create_all`; new **columns** on
  existing tables need an entry in `app/migrate.py` (`ADDED_COLUMNS`, Postgres-capable).
  Grown column lengths go in `WIDENED_COLUMNS`.
- **Auth:** signed session cookie + hashed PINs (`app/security.py`); `SECRET_KEY` and
  `COOKIE_SECURE=1` are set as Render env vars in prod. Timezone via `TZ` (default America/Vancouver).
- **Money is integer cents** everywhere; splits go through `services/money.distribute` (exact sums).

## Git / deploy

- Commit only what the change touched — **stage files explicitly**, never `git add -A`.
- **Never stage `.github/`** — the push token lacks `workflow` scope and the push will be rejected.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Push to `main` → Render auto-deploys. LF→CRLF git warnings on Windows are benign.
