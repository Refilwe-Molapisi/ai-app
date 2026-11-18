# CAIOS — Company AI Operations System

Working implementation of the CAIOS master specification (CAIOS-MASTER-001 v1.0),
starting from the $0 hosting strategy (CAIOS-INFRA-001) and the approved brand
identity. This repository covers the **Phases 0–4 foundations**, **Phase 2
(knowledge & document ingestion)**, **Phase 3 (AI integration)**, **Phase 4
(central coordination)**, **Phase 5 (specialist agents)** and the **production
operator frontend (Vercel)**: documents upload → extraction → chunking →
embeddings (pgvector) → org-scoped retrieval → grounded answers with citations,
with versioned prompt templates, a full AI usage ledger, an assignment/workflow
engine with human approval gates, an append-only governance trail, the three
§11 division agents executing governed workflow steps, and a brand-true single-file
console deployed as a static site.

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
│   ├── 05-phase3-ai.md              ← Phase 3 implementation notes (CAIOS-AI-001)
│   ├── 06-phase4-coordination.md    ← Phase 4 implementation notes (CAIOS-COORD-001)
│   ├── 07-phase5-agents.md          ← Phase 5 implementation notes (CAIOS-AGNT-001)
│   └── 08-frontend.md               ← production frontend + Vercel deployment notes (CAIOS-FE-001)
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
│   ├── app/ai/                      ← Phase 3: prompts (versioned), answer loop (service), usage ledger
│   ├── app/coordination/            ← Phase 4: workflow registry, assignments, engine, gates, events
│   ├── app/agents/                  ← Phase 5: the three §11 division agents (registry + governed runs)
│   ├── app/api/routes/              ← /health, /jobs/run, /maintenance/tick, /documents, /knowledge, /ai, /prompts, /assignments, /workflows, /agents
│   ├── tests/                       ← unit tests + real-Postgres integration harness
│   └── Dockerfile                   ← same image for local, Render and the future VPS
├── frontend/                        ← production operator console (Vercel Root Directory; no build step)
│   ├── index.html                   ← single-file app: overview, assignments+gates, knowledge, agents, AI console, usage, connection
│   └── vercel.json                  ← static preset, security headers, cleanUrls
└── prototype/
    └── caios-ui.html                ← single-file UI prototype; live-API layer (LIVE/DEMO badges)
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

### Phase 4: start a governed assignment (dev mode)

```bash
# what can be started, and what each workflow needs
curl localhost:8000/workflows -H 'Authorization: Bearer dev'

# start one: steps are materialised concretely, first step enqueued as a queue job
curl -X POST localhost:8000/assignments -H 'Authorization: Bearer dev' \
     -H 'Content-Type: application/json' \
     -d '{"org_id":"org-a","workflow":"company.knowledge-brief","inputs":{"topic":"VAT registration"}}'
# → 201 {"id":"…", "status":"running", "steps":[{seq:1, status:"queued", …},{seq:2, is_gate:true}]}

curl -X POST localhost:8000/jobs/run -H 'X-Job-Secret: change-me-local'   # tick: brief step executes
# assignment → awaiting_approval; approve consumes the gate and completes the workflow
curl -X POST localhost:8000/assignments/<id>/approve -H 'Authorization: Bearer dev' \
     -H 'Content-Type: application/json' -d '{"org_id":"org-a","note":"looks right"}'
curl 'localhost:8000/assignments/<id>?org_id=org-a' -H 'Authorization: Bearer dev'  # steps + event trail
```

### Phase 5: specialist agents on workflows (dev mode)

```bash
# the three §11 division agents: duties, permitted tools, outputs, constraints
curl localhost:8000/agents -H 'Authorization: Bearer dev'

# start an agent workflow — the agent runs as a governed step, grounded in
# this org's knowledge base, with a §20 ledger row purpose='agent'
curl -X POST localhost:8000/assignments -H 'Authorization: Bearer dev' \
     -H 'Content-Type: application/json' \
     -d '{"org_id":"org-a","workflow":"client.competitor-scan","inputs":{"subject":"Maize milling in Limpopo"}}'
curl -X POST localhost:8000/jobs/run -H 'X-Job-Secret: change-me-local'   # agent step executes
# → step output carries {"agent":{"slug":"commercial-intelligence",...}, "answer":..., "citations":[...]}

# reword an agent's duties per org — versioned, and always grounded:
curl -X POST localhost:8000/prompts -H 'Authorization: Bearer dev' \
     -H 'Content-Type: application/json' \
     -d '{"org_id":"org-a","name":"agent.commercial-intelligence","body":"House style research brief.\n{{context}}"}'
```

Open `prototype/caios-ui.html?api=http://localhost:8000&org=org-a&token=dev`
in a browser: the prototype renders live assignments with a **LIVE** badge
(plus the online agent count), real approve buttons and a working *New
assignment* flow; without an API it stays on clearly-badged synthetic demo data.

## Layer 2 deployment (demo/staging, cost R0)

Follow `docs/01-hosting-blueprint.md` §8 — Render blueprint deploy, two
cron-job.org jobs (`/health` @10 min keep-warm, `/jobs/run` @3 min with
`X-Job-Secret`), and the two GitHub Actions workflows with repository secrets.
The first successful backup + restore test closes TV-020.

## Frontend deployment (Vercel — monorepo)

The operator console deploys from this same repo as a **static site**:

1. Import `Refilwe-Molapisi/ai-app` into Vercel; set the project's
   **Root Directory** to `frontend` (Framework: Other — `vercel.json` pins
   static output, no build step).
2. Deploy; copy the production + preview URLs.
3. On the API (Render), set `CORS_ORIGINS` to those URLs (comma-separated)
   and redeploy once. Never a wildcard.

The console then shows a **LIVE** badge with real assignments, gates, agents
and the §20 spend; without an API it stays on clearly-badged demo data.
Full notes: `docs/08-frontend.md` (CAIOS-FE-001).

## What is implemented vs. the roadmap

| Phase (master spec §30) | State in this repo |
|---|---|
| 1 — Core foundation | Backend skeleton, config, auth verification, health, Docker — **done** |
| 2 — Knowledge & documents | Upload → extraction (pdf/docx/html/text) → chunking → embeddings (pgvector) → org-scoped semantic search; `knowledge.*` handlers in the runner; TV-019 re-index flow — **done** (see docs/04) |
| 3 — AI integration | Grounded answer loop (`/ai/answer`: retrieval → template → model → citations); versioned prompt management (`/prompts`); §20 usage ledger incl. failure rows (`/ai/usage`) — **done** (see docs/05) |
| 4 — Central coordination | Declarative workflow registry; assignments with ordered steps (queue-executed + human gates); `/assignments` + `/workflows` + approve/cancel; append-only event trail; UI prototype live-API layer — **done** (see docs/06) |
| 5 — Specialist agents | The three §11 division agents as registry definitions (responsibilities, permitted tools, required outputs, quality standards, §11 constraints); `agent.task` step kind; three agent workflows; `/agents`; org-versioned instruction overrides with mandatory grounding; §20 ledger `agent` purpose — **done** (see docs/07) |
| Production frontend | Single-file operator console (`frontend/`, monorepo + Vercel Root Directory): overview, assignments with gate approve/cancel, knowledge upload/search, agent registry with one-click workflows, AI console, usage & spend, connection manager; LIVE/DEMO honesty; `CORS_ORIGINS` allow-list on the API — **done** (see docs/08) |
| 6–10 | Not started (deliverables & document generation, QA governance, internal ops, deployment, continuous dev) |

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
- **Phase 4** — unit `tests/test_phase4.py` **25/25** (registry self-validation
  and rejection matrix, pure planning incl. template-input mismatches,
  executor/registry agreement, DB-free 422s); integration
  `tests/integration_phase4.py` **28/28** (full knowledge-brief lifecycle over
  the public API with a real queue job and §20 ledger row, gate approve →
  completed, cancel path, honest fail-fast on an empty org, cross-org 404,
  status filters).
- **Phase 5** — unit `tests/test_phase5.py` **29/29** (agent registry
  self-validation and rejection matrix, §11 constraints per agent, grounding
  block mandatory, `agent.task` kind/executor agreement, planning-time agent
  validation, `/agents` contract, no ad-hoc run endpoint); integration
  `tests/integration_phase5.py` **19/19** (full competitor-scan lifecycle with
  the agent identity block, grounding verified at the model boundary, §20
  `agent` ledger row, bi-diagnostic intake-first path, second-org controls
  review, cross-org 404).
- **API smoke** (`scripts/smoke_backend.py`, no DB): all 23 routes in OpenAPI,
  auth fail-closed on upload/search/answer/prompts/usage/assignments/agents —
  **18/18**.
- Phase 2 + 3 + 4 suites re-run after Phase 5 wiring: ALL PASS (no regressions).
- UI prototype verified in headless Chromium in both modes: demo fallback
  (DEMO DATA badge) and live-API mode against a real API + Postgres (LIVE
  badge with online agent count, real rows, live AI-spend stat); no console
  errors.
- **Production frontend** verified in headless Chromium against a real API +
  PG16: LIVE connect with `?api=&org=&token=`, gate **approve through the UI**
  (prompt-dialog note → COMPLETED, 7 trail events), **start assignment from an
  agent card** (dynamic required-input modal → RUNNING/QUEUED), demo-mode
  read-only guarding, and 375 px mobile layout with zero horizontal overflow.

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
- Workflow steps are planned concretely at creation (bad inputs are 422s,
  never runtime surprises) and fail fast: a step error fails the assignment
  with the error preserved, and retried jobs exit as no-op skips (§11).
- Every assignment transition is an append-only event row with actor and
  detail — the §13 trail is the audit, not a log file.
- Specialist agents run only as governed workflow steps — there is no ad-hoc
  run endpoint; the §11 constraints ride in every agent's system prompt, and
  an instruction override without the grounding block is refused, never run
  ungrounded (§11.2/11.3, §8.3).
- Everything is containerised: Render → Contabo is a data migration, not a rewrite.
