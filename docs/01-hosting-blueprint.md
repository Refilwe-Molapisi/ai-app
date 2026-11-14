# CAIOS Hosting Blueprint — The $0 Strategy

**Document ID:** CAIOS-INFRA-001
**Reference:** CAIOS-MASTER-001 (v1.0), ADR-001, ADR-008, ADR-009
**Date:** 14 November 2026
**Status:** Adopted baseline — supersedes the "evaluate alternative free backend hosting" position in §5.3

---

## 1. Decision summary

The original specification left backend hosting open (ADR-001) and asked for an
evaluation of free providers (§5.3, TV-001/TV-002). That evaluation is now closed
with the following outcome:

| Option | Verdict | Reason |
|---|---|---|
| Oracle Cloud Always Free | **Rejected** | Genuinely free always-on VPS, but provisioning is unreliable (capacity/sign-up friction). Rejected by management decision. |
| Paid VPS now (Contabo / Hetzner, ~$6/mo) | **Rejected** | No budget available in the current pre-revenue stage. |
| Render / Koyeb / Cloud Run as always-on host | **Rejected** | Render spins down after 15 min idle and has no free background workers; Koyeb free is 0.1 vCPU / 512 MB; Cloud Run requires a card and adds GCP trigger machinery. |
| **Phased $0 strategy (this document)** | **Adopted** | Local Docker for development; free tiers composed carefully for demo/staging; production deferred behind the existing critical-validation gate. |

**Core finding:** there is no free always-on Python host left in 2026. The strategy
therefore does not pretend to have one — it eliminates the need for one until real
client data exists, at which point the first paid tier is funded by the first revenue.

## 2. The three-layer architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ LAYER 1 — BUILD (Phases 1–3)                            cost: R0 │
│ Local Docker Compose: FastAPI + worker + Postgres (pgvector)     │
│ Cloudflare Tunnel when outside access is needed (free, no card)  │
├──────────────────────────────────────────────────────────────────┤
│ LAYER 2 — DEMO / STAGING (Phases 4–8)                   cost: R0 │
│ Render free (stateless API, kept warm)                           │
│ cron-job.org (keep-alive pings + job-batch triggers)             │
│ GitHub Actions (heavy batch work + scheduled backups)            │
│ Supabase free (DB + auth + pgmq) / Z1 (files) / Resend / DeepSeek│
├──────────────────────────────────────────────────────────────────┤
│ LAYER 3 — PRODUCTION GATE                        cost: first pay │
│ Synthetic data only until critical TVs pass.                     │
│ First revenue funds Supabase Pro ($25/mo) before anything else.  │
│ Long-term target unchanged: Contabo VPS (ADR-008).               │
└──────────────────────────────────────────────────────────────────┘
```

The same container image runs in all three layers. No re-platforming is required at
any transition — only environment variables change.

## 3. Layer 1 — Build environment (Phases 1–3)

- `docker compose up` starts the full stack locally: FastAPI API, job worker,
  PostgreSQL with pgvector. See `infra/docker-compose.yml`.
- Backend on `http://localhost:8000`, frontend prototype served statically.
- When something must be reachable from outside (a client demo, a test from another
  device, a webhook callback from Resend or DeepSeek), expose the local backend with
  **Cloudflare Tunnel** (`cloudflared tunnel --url http://localhost:8000`). Free,
  no card, gives a stable HTTPS hostname.
- Development uses synthetic data only (per CAIOS-MASTER-001 §25).

## 4. Layer 2 — Free-tier staging composition (Phases 4–8)

### 4.1 Principles

1. **No always-on worker process.** The backend is fully stateless; all durable
   state lives in Supabase (Postgres) and Z1 (objects). Any single Render service
   can therefore sleep and wake without data loss.
2. **Cron replaces daemons.** cron-job.org fires HTTP calls on a schedule; the
   backend does a bounded amount of work per call and returns inside the ~30 s
   HTTP timeout.
3. **GitHub Actions is the muscle, not the uptime.** Memory-heavy or CPU-heavy
   work (bulk PDF extraction, embedding re-indexing, `pg_dump` backups) runs as
   scheduled Actions workflows on 4 vCPU / 16 GB runners — more compute than any
   free app host provides.

### 4.2 Component allocation

| Role | Service | Verified free allowance (Nov 2026) | Notes |
|---|---|---|---|
| Stateless API | Render free | 750 instance-hrs/mo; spins down after 15 min idle | One service only — keep-alive consumes ~744 hrs |
| Keep-alive + job trigger | cron-job.org | Free, unlimited jobs, 1-min granularity, ~30 s HTTP timeout | Two jobs: `/health` every 10 min; `/jobs/run` every 2–5 min |
| Heavy batch compute | GitHub Actions | 2,000 min/mo (private repo), runners 4 vCPU / 16 GB, 6 h cap per job | Backups, bulk extraction, re-indexing |
| Database / auth / queue | Supabase free | 500 MB DB, 1 GB file storage, 5 GB egress, 50 K MAU, pauses after 7 days inactivity, **no backups**, 2 active projects | pgmq queue via Supabase Queues |
| Object storage | Z1 (primary) + Cloudflare R2 (backup) | Z1: SA-hosted S3-compatible — quantify "free uploads / 100% downloads" in writing; R2: 10 GB free, zero egress | R2 exists because §27 requires *independent* backups |
| Email | Resend | 100 emails/day, 3,000/month | Batch notifications through the job queue as digests |
| LLM | DeepSeek API | Pay-as-you-go; off-peak ≈ 50 % of peak pricing | Full BI-01 diagnostic run ≈ $0.03–0.05 off-peak |

### 4.3 The keep-warm pattern (and its honest caveats)

- cron-job.org pings `GET /health` every 10 minutes → Render never reaches the
  15-minute idle threshold → no cold starts during working hours.
- **Caveat 1:** this consumes ~744 of the 750 monthly instance-hours. Render can
  host exactly **one** near-always-on service. Everything else must be stateless
  and sleepy or moved to Actions.
- **Caveat 2:** keep-alive pinging is a widely used pattern but is not a
  contractual entitlement. Confirm Render's current ToS stance, and design every
  endpoint to tolerate a cold start (idempotent, stateless).
- **Caveat 3:** a missed ping (cron outage, Render incident) causes a sleep, not
  data loss — the architecture must never assume the process stays resident.

### 4.4 Background jobs without a worker

- The job queue is **Postgres-backed** (`pgmq` via Supabase Queues in production
  shape; the shipped skeleton uses a portable `jobs` table with
  `SELECT … FOR UPDATE SKIP LOCKED` behind the same `JobQueue` interface —
  swapping implementations touches one class only, per ADR-009).
- cron-job.org calls `POST /jobs/run` (secret-protected) every 2–5 minutes. The
  endpoint claims a **small batch** (e.g. ≤ 5 jobs), executes within a wall-clock
  budget (e.g. 25 s), marks results, returns. Retries, timeouts, dead-letter
  handling and execution limits (TV-008/TV-016) are properties of the runner, not
  of any hosting provider.
- Long jobs that cannot finish in 25 s are **chunked** by the workflow engine, or
  routed to GitHub Actions via the maintenance-tick endpoint (`/maintenance/tick`).

### 4.5 Scheduled backups (TV-011 closes here, not in Phase 9)

Supabase free has **no backups**, yet TV-011 is Critical. Therefore:

- A GitHub Actions workflow (`backup-supabase.yml`) runs `pg_dump` on schedule
  (daily), gzips, encrypts, and uploads to **R2** (independent from Supabase and
  from Z1).
- A restoration test is part of the workflow's runbook — TV-020 requires a
  *tested* restore, not an assumed one.
- Secrets live in GitHub Actions secrets and Render env vars — never in the repo.

## 5. Layer 3 — Production gate

The $0 stack runs **synthetic data only**. Real client information is admitted
when **all** of the following hold:

1. Critical TVs pass: TV-011/012/013/020 (backups, client isolation, secret
   management, tested restoration) — per CAIOS-MASTER-001 §33.1.
2. Supabase Pro ($25/mo) is active — removes pause risk, adds automated backups,
   lifts the 500 MB limit.
3. The POPIA operator/cross-border assessment is documented (new ADR-014):
   no client-confidential content leaves for DeepSeek until operator terms
   (POPIA s20/s21) and cross-border transfer (s72) are assessed.
4. The engagement's revenue covers the first paid tier — the upgrade is funded,
   not donated.

Upgrade order at first revenue: **Supabase Pro → Resend Pro (Phase 9,
client-facing email) → Contabo VPS (long-term, unchanged ADR-008).**

## 6. Migration path (unchanged in direction, reduced in urgency)

ADR-008 (Contabo VPS) remains the long-term target. §26.2 of the master spec
still holds, but the backend row collapses: the same Docker image that runs on
Render runs on the VPS. What remains at migration time:

- Supabase → VPS PostgreSQL data migration (`pg_dump`/restore — already rehearsed
  by the backup pipeline).
- Frontend move (Cloudflare Pages → VPS, optional).
- Swap `JobQueue` cron triggers for a systemd timer or a long-running worker —
  interface unchanged.

## 7. Register updates

| Item | Change |
|---|---|
| ADR-001 | **Closed.** "Phased hosting: local Docker (dev) → Render free + external cron (staging, synthetic data) → first paid tier at first revenue → Contabo VPS (long-term)." Free-tier evaluation abandoned — no viable free always-on Python host remains. |
| ADR-008 | Unchanged (Contabo), confirmed as long-term target; no longer blocks initial development. |
| ADR-009 | **Resolved (proposed).** Postgres-backed queue behind a replaceable `JobQueue` interface; portable table implementation shipped; pgmq/Supabase Queues as production shape. |
| ADR-012 | **Resolved (proposed).** Embeddings generated independently of the LLM provider; local/HTTP embedding endpoint; deterministic embedder for tests. |
| ADR-014 | **New.** POPIA processing gate — hard workflow-engine gate, not a policy paragraph. |
| TV-001, TV-002 | **Replaced** by the bootstrap checklist in §8 (hosting decision closed). |
| TV-008, TV-016 | Reinterpreted: validated against the job runner + cron trigger, not a hosting provider. |
| TV-011, TV-020 | Pulled forward to Layer 2 (daily scheduled backups + restore test), not deferred to Phase 9. |

## 8. Day-one bootstrap checklist (replaces TV-001/TV-002)

1. Clone repo; `cp infra/.env.example infra/.env`; fill Supabase/Z1/DeepSeek keys.
2. `docker compose -f infra/docker-compose.yml up --build` → API + worker + DB up.
3. `curl localhost:8000/health` → OK.
4. Deploy `backend/` to Render (Blueprint via `infra/render.yaml`), set env vars.
5. Create two cron-job.org jobs: `GET /health` @10 min; `POST /jobs/run` @3 min
   with `X-Job-Secret`.
6. Add the two GitHub Actions workflows; store `SUPABASE_DB_URL` + storage creds
   in Actions secrets; run `backup-supabase.yml` once manually.
7. Download the backup, restore into a scratch Postgres, diff row counts
   → TV-020 evidence #1.
8. Run one synthetic end-to-end job: enqueue → `/jobs/run` → status → audit row.

## 9. Risks accepted under this strategy

| Risk | Mitigation |
|---|---|
| Render keep-alive vs ToS | Monitor; keep endpoints cold-start-safe; migration to Contabo is a config change |
| Supabase free pauses (7-day inactivity) | cron traffic keeps DB active; upgrade at first revenue |
| 500 MB DB ceiling | Cap initial knowledge corpus; `halfvec`/smaller embedding dims; monitor usage |
| GH Actions minutes exhaustion | Budget ~200 min/mo for backups + heavy jobs; workflow_dispatch only for big runs |
| cron-job.org outage | Jobs accumulate in the queue; nothing lost; catch-up on next tick |
| Single-region latency (EU/US hosts vs ZA) | Irrelevant for async workflows; client files stay in-country on Z1 |
