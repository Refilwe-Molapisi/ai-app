# CAIOS — Company AI Operations System

Working implementation of the CAIOS master specification (CAIOS-MASTER-001 v1.0),
starting from the $0 hosting strategy (CAIOS-INFRA-001) and the approved brand
identity. This repository currently covers **Phases 0–4 foundations**: the
backend skeleton, the hosting blueprint, the design system and a UI prototype.

**Status: synthetic data only.** No client-confidential information may enter
any component until the critical validations (TV-011/012/013/020/023) pass and
the ADR-014 POPIA gate is cleared.

---

## Repository layout

```
caios/
├── docs/
│   ├── 01-hosting-blueprint.md      ← $0 strategy: layers, limits, runbook (CAIOS-INFRA-001)
│   ├── 02-design-system.md          ← tokens extracted from brand + UI direction (CAIOS-DESIGN-001)
│   └── 03-adr-and-validation-updates.md ← ADR-001 closed, ADR-009/012/014, TV register changes
├── infra/
│   ├── docker-compose.yml           ← Layer 1: local stack (API + worker + Postgres/pgvector)
│   ├── render.yaml                  ← Layer 2: Render free blueprint
│   └── .env.example                 ← every variable, with staging/prod notes
├── .github/workflows/
│   ├── backup-supabase.yml          ← daily encrypted pg_dump → Cloudflare R2 (TV-011/020)
│   └── heavy-jobs.yml               ← scheduled heavy-work trigger (off-peak)
├── backend/
│   ├── app/core/config.py           ← the only place env vars are read
│   ├── app/core/security.py         ← Supabase JWT verification (JWKS)
│   ├── app/adapters/model_adapter.py      ← provider-independent LLM interface (DeepSeek first)
│   ├── app/adapters/embedding_adapter.py  ← LLM-independent embeddings (ADR-012)
│   ├── app/adapters/storage_adapter.py    ← S3-compatible storage (Z1/R2), client-keyed
│   ├── app/jobs/                    ← portable Postgres job queue + bounded runner (ADR-009)
│   ├── app/api/routes/              ← /health, /jobs/run, /jobs, /maintenance/tick
│   └── Dockerfile                   ← same image for local, Render and the future VPS
└── prototype/
    └── caios-ui.html                ← single-file UI prototype (brand palette + typography)
```

## Quick start (Layer 1 — local, cost R0)

```bash
cp infra/.env.example infra/.env          # fill what you have; everything degrades gracefully
docker compose -f infra/docker-compose.yml up --build
curl localhost:8000/health                # → {"status":"ok", ...}
# exercise the queue end-to-end:
curl -X POST localhost:8000/jobs -H 'Content-Type: application/json' \
     -d '{"kind":"demo.echo","payload":{"hello":"world"}}'        # (dev runs unauthenticated)
curl -X POST localhost:8000/jobs/run -H 'X-Job-Secret: change-me-local'
```

Open `prototype/caios-ui.html` in a browser for the interface direction.

## Layer 2 deployment (demo/staging, cost R0)

Follow `docs/01-hosting-blueprint.md` §8 — Render blueprint deploy, two
cron-job.org jobs (`/health` @10 min keep-warm, `/jobs/run` @3 min with
`X-Job-Secret`), and the two GitHub Actions workflows with repository secrets.
The first successful backup + restore test closes TV-020.

## What is implemented vs. the roadmap

| Phase (master spec §30) | State in this repo |
|---|---|
| 1 — Core foundation | Backend skeleton, config, auth verification, health, Docker — **done** |
| 2 — Knowledge & documents | Adapters stubbed (`knowledge.*` job kinds reserved), schema ready — **start here next** |
| 3 — AI integration | Model adapter complete (DeepSeek + cost accounting); prompt/instruction management — **partially** |
| 4 — Central coordination | Queue + runner + workflow states defined; assignment engine — **skeleton only** |
| 5–10 | Not started (agents, deliverables, QA governance, internal ops, deployment, continuous dev) |

## Verification already performed

- `python -m py_compile` clean on all backend modules.
- Smoke test (`scripts/smoke_backend.py`): 7/7 pass — `/health` 200, secret
  guards (401 on bad secret), authenticated enqueue fail-closed without auth,
  OpenAPI exposes all routes, and cold-start DB failures surface as clean 503s.
- UI prototype rendered and screenshot-verified in headless Chromium: all three
  views, fonts, charts, pills and filters behave; no console errors.

## Rules this repo enforces by construction

- Credentials only via environment (§19); the frontend prototype contains none.
- Machine endpoints (`/jobs/run`, `/maintenance/tick`) are secret-guarded.
- Job claiming uses `SELECT … FOR UPDATE SKIP LOCKED` — no double execution.
- Storage keys are client-prefixed (`clients/<org>/<engagement>/…`) — §7.4.
- Everything is containerised: Render → Contabo is a data migration, not a rewrite.
