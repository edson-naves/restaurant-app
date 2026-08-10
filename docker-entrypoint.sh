#!/usr/bin/env sh
# Container entrypoint for both the local compose stack and Cloud Run.
set -e

# Initialise a fresh database: schema + reference data (menu, tables, floors,
# channels, payment instruments) + a single owner account. Idempotent — a no-op
# once staff exist — so it is safe to run on every cold start.
#
# Fail closed: if bootstrap errors (e.g. OWNER_PIN unset on a brand-new DB, or a
# migration failure) the container exits non-zero instead of starting the web
# server against a partially initialised or mismatched schema. `set -e` above
# turns the failure below into an abort.
python -m app.bootstrap

# Cloud Run injects PORT (usually 8080) and expects the app to listen on it.
# Locally PORT is unset, so we default to 8000 — the port the compose Caddy
# proxy forwards to (app:8000).
exec gunicorn app.main:app \
  -k uvicorn.workers.UvicornWorker \
  -w "${WEB_CONCURRENCY:-2}" \
  -b "0.0.0.0:${PORT:-8000}" \
  --forwarded-allow-ips '*' \
  --access-logfile -
