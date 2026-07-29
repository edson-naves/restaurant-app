# Publish on the web for testers — Google Cloud Run

This puts the app on a **stable public HTTPS URL** (e.g. `https://rms-xxxx.run.app`)
that testers can open anytime — even with your laptop off. No domain needed.
Database is a **free Neon Postgres**. Cost for light testing: ~$0.

> **Heads-up before you start**
> - Google Cloud requires **billing enabled** on the project (a credit card),
>   even though Cloud Run's free tier covers light testing. You won't be charged
>   for a handful of testers, but the card is required to activate.
> - This is a **test deployment of demo data.** The login is a simple PIN with no
>   rate-limiting, and anyone with the URL can sign in as the owner — so set a
>   non-obvious `OWNER_PIN` and don't put real/sensitive data in.
> - Cloud Run's filesystem is **ephemeral**: the 80 menu photos are baked into the
>   image and show fine, but photos uploaded *through the app* won't survive a
>   restart. (Fine for testing; production would store them in Cloud Storage.)

---

## 1. Free Postgres (Neon) — 3 minutes, no card

1. Sign up at **https://neon.tech** (free).
2. Create a project → it gives you a **connection string** like:
   ```
   postgresql://user:PASSWORD@ep-cool-name-123.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```
3. **Change the scheme** from `postgresql://` to `postgresql+psycopg://` (that's the
   only edit). Keep `?sslmode=require`. You'll paste this in step 3.

## 2. Google Cloud CLI — one-time setup

1. Install the **gcloud CLI**: https://cloud.google.com/sdk/docs/install
   (restart your terminal afterwards so `gcloud` is on PATH).
2. Log in and pick/create a project:
   ```sh
   gcloud auth login
   gcloud projects create rms-test-123 --name="RMS Test"   # or use an existing one
   gcloud config set project rms-test-123
   ```
3. **Enable billing** for the project in the console
   (https://console.cloud.google.com/billing), then enable the services:
   ```sh
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
   ```

## 3. Configure secrets

In the project folder, create **`env.yaml`** (already git/docker-ignored):
```yaml
DATABASE_URL: "postgresql+psycopg://user:PASSWORD@ep-cool-name-123.us-east-2.aws.neon.tech/neondb?sslmode=require"
OWNER_PIN: "4729"
```
Use your Neon string from step 1 and any 4–8 digit PIN you'll remember.

## 4. Deploy

From the project folder (the one with the `Dockerfile`):
```sh
gcloud run deploy rms \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 512Mi \
  --env-vars-file env.yaml
```
- First run: say **yes** to any prompts (create the Artifact Registry repo, enable APIs).
- Cloud Build builds the image from the `Dockerfile`, deploys it, and the
  container **auto-seeds the Neon database on first boot** (menu, tables, floors,
  the owner account, and the linked photos).
- When it finishes it prints a **Service URL** — that's your public link.

## 5. Share it

Give testers the **Service URL**. They open it, sign in as **Owner** with the PIN
you set in `env.yaml`, and start testing. Nothing to install; the laptop can be off.

---

## Everyday commands
```sh
# Redeploy after code changes:
gcloud run deploy rms --source . --region us-central1 --allow-unauthenticated --env-vars-file env.yaml

# Live logs:
gcloud run services logs tail rms --region us-central1

# The current URL:
gcloud run services describe rms --region us-central1 --format='value(status.url)'

# Take it down (stop serving / avoid any charges):
gcloud run services delete rms --region us-central1
```

## When testing is done → hardening before real use
Same list as DEPLOY.md: login rate-limiting + hashed PINs + session expiry,
move uploaded photos to Cloud Storage, and (if it becomes the real POS) reconsider
on-prem for offline resilience. This Cloud Run setup is for **testing**, not the
live restaurant floor.
