# Publish for testers — Koyeb (NO credit card)

Puts the app on a stable public HTTPS URL (e.g. `https://rms-yourname.koyeb.app`)
with a free Postgres database — **no credit card required**. Testers open the
link and sign in; your laptop can be off.

Koyeb's free tier gives you **one web service (your Docker container) + one
Postgres database**, which is exactly what this needs.

> Same honest caveats as any public test: it's **demo data with a simple PIN
> login** (anyone with the URL can sign in as owner), so use a non-obvious
> `OWNER_PIN` and no real data. Photos *uploaded through the app* won't survive a
> restart (ephemeral disk); the 80 already-linked photos are baked into the image
> and show fine. A free service may sleep after inactivity and cold-start on the
> next visit — normal for testing.

---

## 0. Prerequisite: code on GitHub (free, no card)

Koyeb builds straight from a GitHub repo. Your project is a local git repo with
uncommitted work, so:

1. Ask Claude to **commit** the current work (it will, on your say-so).
2. Create an **empty private repo** on https://github.com/new (no card needed).
3. Push to it (Claude will give you the exact two commands with your repo URL).

## 1. Sign up

Create a free account at **https://www.koyeb.com** — sign up **with GitHub** so it
can see your repo. No card asked.

## 2. Create the database

Koyeb dashboard → **Databases → Create Database Service** → Free plan → create.
Open it and copy the **connection string**. Change the scheme from
`postgresql://` to **`postgresql+psycopg://`** (the only edit) — keep everything
else, including `?sslmode=require` if present.

## 3. Create the web service

Koyeb dashboard → **Create Service → GitHub** → pick your repo. Then:

- **Builder:** Dockerfile (Koyeb auto-detects it).
- **Instance:** Free (Nano).
- **Port:** `8000`.
- **Environment variables:**
  | Name | Value |
  |------|-------|
  | `DATABASE_URL` | the `postgresql+psycopg://…` string from step 2 |
  | `OWNER_PIN` | a 4–8 digit PIN you'll remember |

Click **Deploy**. Koyeb builds the image from the Dockerfile and starts it. On
first boot the container **auto-seeds the database** — menu, tables, floors, the
owner account, and the 80 linked photos.

## 4. Share it

Koyeb shows a **public URL** (`https://<service>-<org>.koyeb.app`). Send it to
testers → they sign in as **Owner** with your PIN → done. Laptop can be off.

---

## Redeploy / manage
- **Redeploy after changes:** push to GitHub — Koyeb auto-builds the new commit
  (or hit *Redeploy* in the dashboard).
- **Logs:** the service's **Logs** tab in the dashboard.
- **Stop serving:** pause or delete the service in the dashboard.

## When testing is done → hardening before real use
Login rate-limiting + hashed PINs + session expiry, and move uploaded photos to
object storage. This is a **test** deployment, not the live restaurant floor —
for that, the on-prem setup (DEPLOY.md) keeps working even if the internet drops.
