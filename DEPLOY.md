# Deploying for real — on-prem (LAN)

This runs the app + PostgreSQL on a single always-on machine in the restaurant
(a mini-PC, or your laptop on the same wi-fi to start). Tablets connect over the
local network, so **the floor keeps working even if the internet is down** —
the right architecture for a point-of-sale.

Everything below is the same three files whether the box is your laptop, a
mini-PC, or a rented server: `docker-compose.yml`, `Dockerfile`, `Caddyfile`.

---

## 1. Prerequisites on the box

- **Docker Desktop** (Windows/Mac) or **Docker Engine + compose plugin** (Linux).
  This is the only thing you install by hand — everything else runs in
  containers.
- The box should have a **static/reserved IP** on the restaurant network (set it
  in the router's DHCP reservations) so the tablets' bookmark never breaks.

## 2. Configure

```sh
cp .env.example .env
```

Edit `.env` and set at least:

```
POSTGRES_PASSWORD=<a long random password>
```

Keep `.env` out of version control (it holds the DB password).

## 3. Start it

```sh
docker compose up -d --build
```

This builds the app image and starts three services: `db` (Postgres, data on a
named volume), `app` (Gunicorn + Uvicorn workers), and `caddy` (HTTPS reverse
proxy). The app creates its schema automatically on first boot.

## 4. Seed the restaurant (one time)

```sh
OWNER_PIN=1234 docker compose run --rm -e OWNER_PIN app python -m app.bootstrap
```

This loads the menu, tables, floor & zones, sales channels and payment
instruments, and creates a single **Owner** account with the PIN you chose.
There are **no default accounts** — nothing to forget to change later. It is
idempotent: run it again and it does nothing.

Then sign in as **Owner** and, under **Manage**, add your real staff (each with
their own PIN) and adjust the floor/tables to match the room.

## 5. Point the tablets at it

Find the box's IP (e.g. `192.168.1.50`) and browse to:

```
https://192.168.1.50/
```

Add it to each tablet's home screen for a full-screen, app-like shortcut.

### Trust the certificate (removes the browser warning)

Caddy issues a certificate from its own local authority. Install that root
certificate once per tablet and the warning disappears:

```sh
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > rms-root.crt
```

Email/AirDrop `rms-root.crt` to each tablet and install it (iPad: Settings →
Profile → Install, then General → About → Certificate Trust Settings →
enable it; Android: Settings → Security → Install a certificate → CA).

> On a fully trusted, isolated LAN some operators skip TLS and serve plain HTTP.
> If you take card details anywhere near this device, keep HTTPS on.

## 6. Backups (do not skip)

A backup on the same box is not a backup. Schedule `scripts/backup.sh` nightly
and **finish its offsite TODO** (copy the dump to another machine or cloud):

```sh
# Linux cron, 03:00 nightly:
0 3 * * * cd /path/to/restaurant_app && sh scripts/backup.sh >> backups/backup.log 2>&1
```

**Restore** from a dump:

```sh
gunzip -c backups/restaurant-YYYYMMDD-HHMMSS.sql.gz \
  | docker compose exec -T db psql -U rms -d restaurant
```

Test a restore onto a spare box *before* you need it.

## 7. Updating the app

```sh
git pull
docker compose up -d --build
```

Schema changes apply automatically on boot (additive only — no data loss). Take
a backup first anyway.

---

## Operating notes

- **Logs:** `docker compose logs -f app`
- **Stop / start:** `docker compose down` / `docker compose up -d`
- **A crash restarts itself** (`restart: unless-stopped`); the box should also be
  set to power on after a power cut (BIOS "restore on AC").
- **Health check:** `https://<box-ip>/healthz` returns `ok`.

## What's still worth doing next (post-launch)

- **Live updates** on the kitchen & floor screens (polling/SSE) so tickets
  appear without a refresh.
- **Auth hardening:** session expiry and hashed PINs (currently compared as
  stored) — worth doing before the system is widely used.
- **A spare box** flashed and ready, so a hardware failure is a 10-minute swap
  and a restore, not a lost night.
