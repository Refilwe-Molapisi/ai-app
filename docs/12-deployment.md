# Phase 9 — Deployment — Runbook

**Document ID:** CAIOS-DEPLOY-001
**Reference:** CAIOS-MASTER-001 §30 (roadmap phase 9), §16.1 (Supabase auth required in production), §19 (credentials server-side only); CAIOS-INFRA-001 ($0 hosting strategy)
**Date:** 19 November 2026
**Status:** Operator console **live** on GitHub Pages; backend deploy-ready via the Render blueprint (one connect away); production posture verified against a real server process

---

## 1. What is live right now

**https://refilwe-molapisi.github.io/ai-app/** serves the operator console
(the single-file app from `frontend/index.html`, deployed from the repo's
`gh-pages` branch; GitHub auto-built the Pages site on push). Verified on
19 Nov 2026: HTTP 200, content matches the phase-8 build (Internal Ops view
present), zero console errors in Chromium, no overflow at 375 px. Without an
API configured it runs in **demo mode** by design — the Connection view is
where the operator points it at the real API.

Updating the live console after a change to `frontend/index.html`:

```bash
git worktree add /tmp/pages gh-pages
cp frontend/index.html /tmp/pages/index.html && touch /tmp/pages/.nojekyll
git -C /tmp/pages commit -am "deploy: console update" && git -C /tmp/pages push origin gh-pages
```

## 2. Backend deployment (Render, one connect away)

Everything the deploy needs is in the repo; the only step that requires the
owner's Render account is connecting it.

1. **Database — Supabase.** Create a project (free tier), enable the
   `vector` extension (Database → Extensions), then create a database
   password. Take the **pooler** connection string (port 6543,
   `…pooler.supabase.com:6543/postgres`) — it survives serverless/free-tier
   connection churn.
2. **Migrate.** From `backend/`:
   `python scripts/apply_schemas.py --url "<pooler-url>"` — applies all six
   module schemas in dependency order, idempotently (safe to re-run; future
   column changes ship as new migration files per master spec §6.4).
3. **Services — Render Blueprint.** Render → New → Blueprint → connect this
   repo. `infra/render.yaml` defines both services:
   - **caios-api** (web, Docker, `backend/Dockerfile`, health check
     `/health`, Frankfurt region — closest free region to ZA);
   - **caios-worker** (worker, same image, `python -m app.jobs.loop_worker`
     — the queue loop that drains jobs; no public port).
4. **Fill the `sync: false` secrets** in both services' dashboards:
   `DATABASE_URL` (identical in both), `SUPABASE_URL` + `SUPABASE_JWKS_URL`,
   `CORS_ORIGINS` = `https://refilwe-molapisi.github.io` (add a Vercel domain
   later if used), `DEEPSEEK_API_KEY`, the storage keys.
   `JOB_RUN_SECRET` is generated for the API — **copy the same value into
   the worker** and into the cron-job.org header (step 6).
5. **Flip the env.** The blueprint ships `CAIOS_ENV=staging`. After the smoke
   checklist below, set it to `production` — the app then **refuses to boot**
   without Supabase auth configured (lifespan guard, §16.1; verified in
   `tests/integration_phase9.py`) and the dev-bearer shortcut is gone.
6. **Keep-alive + queue tick** (free tier spins down after 15 min idle):
   cron-job.org → every 10 min → `GET <render-url>/health` (wakes the API),
   and every minute → `POST <render-url>/jobs/run` with
   `X-Job-Secret: <JOB_RUN_SECRET>` (the external tick; the in-container
   worker loop already drains the queue continuously — the cron tick is the
   belt-and-braces path for spindowns).

## 3. Why GitHub Pages *and* Vercel are both in the blueprint

The master spec pins Vercel for the production console (Root Directory =
`frontend/`, `vercel.json` security headers). That still stands. GitHub Pages
is the live deployment that exists **today** without a second account: same
single-file app, same Connection-view configuration, auto-built from the
`gh-pages` branch. When the Vercel project is connected, set its Root
Directory to `frontend/` and add the new domain to `CORS_ORIGINS`; Pages
then becomes the permanent fallback/demo deployment. One honest difference:
`vercel.json` security headers do not apply on GitHub Pages — the console
sends no secrets anywhere except the API URL the operator enters, and all
credentials live server-side (§19), so the Pages deployment does not weaken
the credential posture.

## 4. Verification evidence (this phase)

| Check | Result |
|---|---|
| `tests/integration_phase9.py` — migrations: `apply_schemas.py` applies all six schemas against a fresh Postgres, twice (idempotent) | PASS |
| Production boot **refused** without an auth provider (lifespan RuntimeError, §16.1) | PASS |
| Production boot with mock JWKS: `/health` 200 `env=production`; unauthenticated call → 401; `Bearer dev` (garbage token) → 401 — dev shortcut gone | PASS |
| CORS: allowed origin echoed on GET and PATCH preflight (methods listed); unknown origin gets no ACAO header on either | PASS |
| `infra/render.yaml` + `docker-compose.yml` parse; compose mounts all six schemas; worker command matches the blueprint | PASS |
| Live Pages URL: HTTP 200, current content (phase-8 build), Chromium: demo mode, Ops view renders, zero console errors, no overflow at 375 px | PASS |
| Full battery (all fifteen suites, phases 2–8) re-run after deployment artifacts | ALL PASS |

## 5. Post-deploy smoke checklist (run once against the live Render URL)

1. `GET /health` → `{"status":"ok","env":"production"}`.
2. Console → Connection → paste the Render URL → save → header shows LIVE.
3. Upload one PDF → Knowledge view shows it extracted + indexed.
4. AI Console → ask one grounded question → answer with citations; Usage
   view records the call.
5. Start `client.onboarding-check` → approve the gate in Assignments.
6. Deliverables → generate → QA-approve → download; checksum visible.
7. Ops view: roster entry visible; open workload lands on the right desk.

## 6. Rollback

Render keeps previous deploys — "Rollback" on the Events tab redeploys the
last good image in one click. The console is a single static file: revert
`gh-pages` one commit and push. Database schemas are additive (IF NOT
EXISTS only); rolling back code never requires rolling back data.
