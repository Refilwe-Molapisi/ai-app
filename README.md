# CAIOS — Company AI Operations System

Working implementation of the CAIOS master specification (CAIOS-MASTER-001 v1.0),
starting from the $0 hosting strategy (CAIOS-INFRA-001) and the approved brand
identity. This repository covers the **Phases 0–4 foundations**, **Phase 2
(knowledge & document ingestion)** and **Phase 3 (AI integration)**: documents
upload → extraction → chunking → embeddings (pgvector) → org-scoped retrieval →
grounded answers with citations, with versioned prompt templates and a full
AI usage ledger.

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
│   ├── 03-adr-and-validation-updates.md ← ADR-001 closed, ADR-009/012/014, TV register changes
│   ├── 04-phase2-knowledge.md       ← Phase 2 implementation notes (CAIOS-KNOW-001)
│   └── 05-phase3-ai.md              ← Phase 3 implementation notes (CAIOS-AI-001)
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
│   ├── app/knowledge/               ← Phase 2: schema, extraction, chunking, handlers, routes
│   ├── app/ai/                      ← Phase 3: prompts (versioned), answer loop, usage ledger
│   ├── app/api/routes/              ← /health, /jobs/run, /maintenance/tick, /documents, /knowledge, /ai, /prompts
│   ├── tests/                       ← unit tests + real-Postgres integration harness
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

### Phase 2: ingest a document and search it (dev mode)

```bash
# upload (dev: any Bearer header works while SUPABASE_URL is unset; storage must be configured)
curl -X POST localhost:8000/documents/upload \
     -H 'Authorization: Bearer dev' \
     -F 'file=@statement.pdf;type=application/pdf' \
     -F 'org_id=org-a' -F 'scope=client' -F 'engagement_id=eng-2026'
# → 202 {"id": "...", "extract_job_id": "..."}    (extract chains embed automatically)

curl -X POST localhost:8000/jobs/run -H 'X-Job-Secret: change-me-local'   # tick: extraction
curl -X POST localhost:8000/jobs/run -H 'X-Job-Secret: change-me-local'   # tick: embedding

# semantic search (org-scoped; results exclude chunks from a different embedder)
curl -X POST localhost:8000/knowledge/search -H 'Authorization: Bearer dev' \
     -H 'Content-Type: application/json' \
     -d '{"query":"VAT registration threshold","org_id":"org-a"}'
```

Without storage or an embedding endpoint configured, uploads fail loudly (503)
and the pipeline runs on the deterministic dev embedder — intentional, so every
environment degrades explicitly instead of pretending.

### Phase 3: ask the knowledge base (dev mode)

```bash
# grounded answer: retrieval → versioned prompt → model → usage ledger
# requires DEEPSEEK_API_KEY (or any adapter); retrieval works regardless
curl -X POST localhost:8000/ai/answer -H 'Authorization: Bearer dev' \
     -H 'Content-Type: application/json' \
     -d '{"question":"When is VAT registration mandatory?","org_id":"org-a"}'
# → {"answer": "...", "grounded": true, "citations": [{title, seq, distance}], "usage": {...}}

# versioned prompt templates (the org override for the default rag.answer)
curl -X POST localhost:8000/prompts -H 'Authorization: Bearer dev' \
     -H 'Content-Type: application/json' \
     -d '{"org_id":"org-a","name":"rag.answer","body":"Answer ONLY from:\n{{context}}\nCite as [n]."}'
curl -X POST localhost:8000/prompts/<id>/versions ...   # new version becomes active

# §20 usage ledger: tokens, estimated cost, errors per org
curl 'localhost:8000/ai/usage?org_id=org-a&days=30' -H 'Authorization: Bearer dev'
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
| 2 — Knowledge & documents | Upload → extraction (pdf/docx/html/text) → chunking → embeddings (pgvector) → org-scoped semantic search; `knowledge.*` handlers in the runner; TV-019 re-index flow — **done** (see docs/04) |
| 3 — AI integration | Grounded answer loop (`/ai/answer`: retrieval → template → model → citations); versioned prompt management (`/prompts`); §20 usage ledger incl. failure rows (`/ai/usage`) — **done** (see docs/05) |
| 4 — Central coordination | Queue + runner + workflow states defined; assignment engine — **skeleton only** |
| 5–10 | Not started (agents, deliverables, QA governance, internal ops, deployment, continuous dev) |

## Verification already performed

All suites are reproducible; the database suites run against a real
PostgreSQL 16 + pgvector (user-space via `pgserver`, or any `DATABASE_URL`):

- **Phase 2** — unit `tests/test_phase2.py` **29/29** (extraction matrix incl.
  encrypted PDFs, chunking determinism/bounds/coverage, embedder contract);
  integration `tests/integration_phase2.py` **47/47** (full pipeline, org
  isolation, TV-019 embedder switch, failure paths, dead-letter semantics).
- **Phase 3** — unit `tests/test_phase3.py` **23/23** (template validation and
  rejection matrix, rendering guard, grounding rules, DeepSeek cost math);
  integration `tests/integration_phase3.py` **24/24** (prompt lifecycle with
  409/422 paths, grounded answer with citations + prompt version, ledger rows
  incl. simulated model failure, org-scoped ledgers, ungrounded hint path).
- **API smoke** (`scripts/smoke_backend.py`, no DB): all 17 routes in OpenAPI,
  auth fail-closed on upload/search/answer/prompts/usage — **13/13**.
- Phase 2 suites re-run after Phase 3 wiring: ALL PASS (no regressions).
- UI prototype rendered and screenshot-verified in headless Chromium: all three
  views, fonts, charts, pills and filters behave; no console errors.

The real-database integration harness caught and fixed a latent Phase 0–4 bug:
psycopg3 returns tuple rows unless `row_factory=dict_row` is pinned on the
pool — invisible to every 503-path smoke test, fatal on a live queue.

## Rules this repo enforces by construction

- Credentials only via environment (§19); the frontend prototype contains none.
- Machine endpoints (`/jobs/run`, `/maintenance/tick`) are secret-guarded.
- Job claiming uses `SELECT … FOR UPDATE SKIP LOCKED` — no double execution.
- Storage keys are client-prefixed (`clients/<org>/<engagement>/…`) — §7.4.
- Knowledge rows carry `org_id` end to end; every search/list filters on it and
  cross-org reads return 404/empty — TV-012 server-side isolation.
- Search never compares vectors from different embedders — provider, model and
  dimensions are pinned per chunk (TV-019).
- Every model call writes a §20 ledger row — including failures; cost is
  estimated at conservative peak pricing until TV-007 verifies provider rates.
- Answers report `grounded` honestly, cite by document+chunk, and refuse to
  mix vector spaces — an ungrounded answer says so, twice (flag + prose rule).
- Prompt behaviour changes are versioned events (`/prompts/{id}/versions`),
  never silent edits (§8.3).
- Everything is containerised: Render → Contabo is a data migration, not a rewrite.
