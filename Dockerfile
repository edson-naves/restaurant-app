# Production image. Python 3.13 (stable wheels for psycopg + gunicorn); the app
# itself is dialect-agnostic and talks to Postgres in production via DATABASE_URL.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY requirements.txt requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements-prod.txt

# Application code and templates/static assets (includes web/static/img/menu,
# so the linked menu photos are baked into the image and served on Cloud Run).
COPY app ./app
COPY web ./web
COPY docker-entrypoint.sh ./

# Run as an unprivileged user; it owns /app so the runtime can create its
# working dirs (db/ is unused on Postgres, outbox/ holds queued emails).
RUN chmod +x docker-entrypoint.sh && useradd --create-home appuser && chown -R appuser /app
USER appuser

# Informational only. Locally the app binds 8000 (Caddy proxies to it); on
# Cloud Run it binds $PORT (8080) instead — see docker-entrypoint.sh.
EXPOSE 8000

# The entrypoint seeds a fresh DB, then starts Gunicorn + Uvicorn workers on
# $PORT (default 8000). --forwarded-allow-ips trusts the X-Forwarded-* headers
# from the reverse proxy (Caddy locally, Google's front end on Cloud Run).
CMD ["sh", "docker-entrypoint.sh"]
