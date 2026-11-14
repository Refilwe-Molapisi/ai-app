# ADR Updates & Validation Register Changes

**Document ID:** CAIOS-GOV-001
**Reference:** CAIOS-MASTER-001 §32–33; CAIOS-INFRA-001 (Hosting Blueprint)
**Date:** 14 November 2026

---

## 1. Architecture Decision Records (§32.1 format)

### ADR-001 — Backend hosting (CLOSED)

- **Context:** CAIOS requires a backend runtime during pre-revenue development.
  Management rejected paid VPS spend and rejected Oracle Always Free after
  provisioning friction.
- **Problem:** §5.3 required evaluating alternative free hosting; no viable free
  always-on Python host remains in 2026.
- **Decision:** Phased hosting — (1) local Docker Compose for development;
  (2) Render free + cron-job.org + GitHub Actions for demo/staging on synthetic
  data; (3) first paid tier (Supabase Pro) at first revenue; (4) Contabo VPS
  long-term. Full rationale in CAIOS-INFRA-001.
- **Alternatives considered:** Oracle Always Free (rejected — provisioning);
  Render always-on (rejected — spin-down, no workers); Cloud Run (rejected —
  card + GCP coupling); Koyeb (rejected — 512 MB).
- **Consequences:** keep-alive pattern required; one near-always-on Render
  service only; production gated behind critical TVs.
- **Status:** **Closed — adopted.** Date: 2026-11-14.

### ADR-002 — Supabase Auth — unchanged, Selected.
### ADR-003 — Z1 storage — unchanged, Provisionally selected (quantify allowances in writing — TV-004).
### ADR-005 — Supabase PostgreSQL — unchanged, Selected (Pro upgrade at first revenue).
### ADR-006 — DeepSeek API — unchanged, Selected initially (behind model adapter).

### ADR-008 — Future infrastructure (CONFIRMED, de-risked)

- **Decision:** Contabo VPS remains the long-term target. Because the stack is
  containerised from day one, migration becomes a data migration + config change,
  not a re-platform. **Status: Confirmed — no longer blocks initial development.**

### ADR-009 — Background processing (RESOLVED — proposed)

- **Decision:** Postgres-backed job queue behind a replaceable `JobQueue`
  interface. Shipped skeleton uses a portable `jobs` table with
  `SELECT … FOR UPDATE SKIP LOCKED`; production shape is pgmq (Supabase Queues).
  Cron (Layer 2) or systemd/worker (Layer 3) drives it — the queue never depends
  on a hosting provider.
- **Consequences:** retries, timeouts, dead-lettering and execution limits are
  runner properties; TV-008/TV-016 validate against the runner.
- **Status:** Proposed → implement in Phase 4.

### ADR-012 — Embedding models (RESOLVED — proposed)

- **Decision:** Embeddings are generated independently of the primary LLM
  (DeepSeek offers no embeddings API). Adapter interface `EmbeddingAdapter` with:
  deterministic hash embedder (tests/dev), HTTP embedder (local Ollama/TEI or any
  endpoint), and a local BGE-M3/multilingual-E5 target for the worker. Record
  provider/model/dimensions per ADR §9.4; `halfvec` considered for the 500 MB
  free-tier budget. **Status:** Proposed → implement in Phase 2.

### ADR-013 — Backup & recovery — unchanged, Proposed (implemented early: daily scheduled backups in Layer 2).

### ADR-014 — POPIA processing gate (NEW)

- **Context:** CAIOS will process client-confidential data; DeepSeek and free-tier
  infrastructure introduce cross-border processor relationships (POPIA ss20–21,
  s72). CAIOS-MASTER-001 §28 defers assessment.
- **Problem:** nothing in the architecture *enforces* the boundary today.
- **Decision:** a hard workflow-engine gate — assignments involving
  Client-Confidential/Restricted data cannot dispatch LLM calls or external
  research tools until the operator-terms assessment is documented and a
  `popia_clearance` flag is set on the engagement. Interim mitigations:
  synthetic data only, de-identification before any external call, deliberate
  Supabase region choice.
- **Status:** Adopted as policy; enforce in Phase 4 (workflow engine).

## 2. Technical Validation Register changes (§33)

| ID | Change |
|---|---|
| TV-001 | **Replaced** by bootstrap checklist §8 of CAIOS-INFRA-001 (hosting decision closed) |
| TV-002 | **Replaced** — FastAPI deployment compatibility is now trivially proven by the same Docker image running locally and on Render |
| TV-004 | Kept — extended: obtain Z1 allowance quantification in writing; test boto3, signed URLs, multipart, CORS, lifecycle |
| TV-006 | Kept — now includes EmbeddingAdapter validation (ADR-012) |
| TV-008 | Kept — reinterpreted: validate the job runner under cron triggers (batch, 25 s budget) |
| TV-011 | **Pulled forward** — daily scheduled backups run from Phase 2, not Phase 9 |
| TV-016 | Kept — reinterpreted: runner retry/timeout/limit tests |
| TV-020 | Kept — first restore test scheduled immediately after the first successful backup (bootstrap step 7) |
| **TV-023 (new)** | Validate POPIA gate enforcement: confidential assignment cannot dispatch external calls without `popia_clearance` — Priority: Critical |
| **TV-024 (new)** | Validate keep-warm + `/jobs/run` catch-up behaviour after a sleep (Render cold start mid-queue) — Priority: High |
| **TV-025 (new)** | Validate GitHub Actions backup restore into scratch Postgres, monthly — Priority: Critical (evidence for TV-020) |

## 3. Acceptance criteria impact (§34)

No criterion is weakened. Criterion 13 ("background jobs processed reliably") is
now proven by the runner + cron architecture rather than an always-on worker;
criterion 19 ("supports future VPS migration") is strengthened by the
container-first approach.
