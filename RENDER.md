# Publish for testers — Render (NO credit card)

Deploys straight from your GitHub repo. Stable public HTTPS URL, free Postgres,
**no credit card**. (Koyeb was the earlier plan but it's shutting down its dev
platform after joining Mistral — Render is the drop-in replacement.)

The app needs **no changes** — it already binds Render's `$PORT` and auto-seeds
the database on first boot.

> Free notes: a free web service **sleeps after ~15 min idle** and cold-starts
> (~1 min) on the next visit — fine for testing. Same demo/auth caveats as any
> public test: simple PIN login, use a non-obvious `OWNER_PIN`, demo data only.
> Photos uploaded *through the app* don't persist (ephemeral disk); the 80
> already-linked photos are baked into the image and show fine.

---

## 1. Sign up
Go to **https://render.com** → **Get Started** → **Sign in with GitHub**
(authorize it to see your repos). No card asked.

## 2. Create the database
Dashboard → **New +** → **Postgres**.
- Name: `rms-db`, Plan: **Free** → **Create Database**.
- Wait ~1 min, then on the database page copy the **Internal Database URL**
  (looks like `postgresql://user:pass@dpg-xxxx/dbname`).
- **Change the scheme** `postgresql://` → **`postgresql+psycopg://`** (only edit).

## 3. Create the web service
Dashboard → **New +** → **Web Service** → connect the **`restaurant-app`** repo.
- **Language / Runtime:** Docker (Render auto-detects the `Dockerfile`).
- **Instance Type:** **Free**.
- **Environment variables** (Advanced → Add):
  | Key | Value |
  |-----|-------|
  | `DATABASE_URL` | the `postgresql+psycopg://…` string from step 2 |
  | `OWNER_PIN` | a 4–8 digit PIN you'll remember |
- (No port setting needed — Render injects `PORT` and the app binds it.)
- **Create Web Service.**

Render builds the image from the Dockerfile and starts it. On first boot the
container **auto-seeds** the database — menu, tables, floors, owner account, and
the 80 linked photos.

## 4. Share it
Render gives a public URL like **`https://restaurant-app.onrender.com`**. Send it
to testers → sign in as **Owner** with your PIN → done. Your laptop can be off.

---

## Manage
- **Auto-deploys** on every `git push` to `main`.
- **Logs / manual deploy / env vars:** the service dashboard.
- Free Postgres has a storage cap and may expire after a period — if you need it
  longer, swap `DATABASE_URL` for a free **Neon** database (neon.tech, no card).
