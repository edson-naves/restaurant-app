# Migrate the restaurant DB from Render (free, expiring) → Neon

**Why now:** Render emailed that the free Postgres `restaurant` is **suspended on 2026-08-28**
and becomes inaccessible. Neon's free Postgres does **not** expire — and it becomes the shared
database for Nu-Brite later (one Neon, two apps, US$0).

Do this **before Aug 28**, while the old Render DB is still alive to copy from. Nothing is
deleted from Render by these steps — you only copy out and switch the app over.

---

## Prerequisites
- Docker Desktop running (the script uses the `postgres` image — no local psql install needed).
- Access to the **Render** dashboard and a **Neon** account.

## Step 1 — Create the Neon database (free, no card)
1. Sign up at **neon.tech** (free tier).
2. Create a project — call the database **`restaurant`** (region close to you / your users).
3. Copy the **connection string**. It looks like:
   `postgresql://USER:PASSWORD@ep-xxxx.REGION.aws.neon.tech/restaurant?sslmode=require`
   Keep the `?sslmode=require`.

## Step 2 — Get the Render source URL
Render dashboard → your Postgres **`restaurant`** → **Connect** → copy the **External Database URL**
(the external one, since we connect from your machine). Looks like `postgres://...render.com/restaurant`.

## Step 3 — Copy the data (Render → Neon)
From the repo root (`Restaurant/restaurant_app`), in **Git Bash**:

```bash
SRC='postgres://...render.com/restaurant' \
DST='postgresql://...neon.tech/restaurant?sslmode=require' \
  bash scripts/migrate_to_neon.sh
```

It dumps from Render to a local file, restores into Neon, and prints the table list + a staff
count so you can eyestop-check it copied. (If it complains about a Postgres **version**, re-run with
`PGIMAGE=postgres:16-alpine` prepended — match Neon's major version.)

> PowerShell instead of Git Bash? Same idea, just set the vars first:
> ```powershell
> $env:SRC='postgres://...render.com/restaurant'
> $env:DST='postgresql://...neon.tech/restaurant?sslmode=require'
> bash scripts/migrate_to_neon.sh
> ```

## Step 4 — Point the app at Neon (the actual switch)
Render dashboard → **restaurant-app** web service → **Environment** → edit **`DATABASE_URL`**.

**Important — the app needs the SQLAlchemy driver prefix, not the plain URL.** Take the Neon
string and change the scheme to `postgresql+psycopg://`:

```
postgresql+psycopg://USER:PASSWORD@ep-xxxx.REGION.aws.neon.tech/restaurant?sslmode=require
```

Save → Render redeploys automatically. (The app runs its own migrations on boot, so the schema
is ready; the data you copied is already there.)

## Step 5 — Verify, THEN stop worrying about Aug 28
- Open `https://restaurant-app-mbp4.onrender.com/`, sign in, confirm your data (staff, menu,
  history) is there.
- Update `KEEPALIVE_URL` only if the app URL changed (it won't).
- Once verified, delete the local `restaurant_*.dump`. You can leave the old Render DB to expire
  on its own on Aug 28 — the app no longer uses it.

---

## Later — Nu-Brite shares the same Neon (no new DB, no new cost)
When you deploy Nu-Brite:
1. In Neon, add a second database **`wheels`** (same project: SQL `CREATE DATABASE wheels;`, or the
   Neon UI). Free tier holds both.
2. Nu-Brite's `DATABASE_URL` → the same Neon host, database `wheels`
   (`postgres://...neon.tech/wheels?sslmode=require`; Nu-Brite uses the `postgres` node driver, so
   **no** `+psycopg` prefix there).
3. Deploy Nu-Brite as a second Render web service → free `nubrite.onrender.com`.

Result: one Neon (two databases) + two Render services = consolidated, US$0.

## Rollback
If anything looks wrong after Step 4, set Render's `DATABASE_URL` back to the old Render URL and
redeploy — the old DB is untouched until Aug 28.
