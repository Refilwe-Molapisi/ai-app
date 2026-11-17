# Phase 3 — AI Integration: Implementation Notes

**Document ID:** CAIOS-AI-001
**Reference:** CAIOS-MASTER-001 §8 (AI integration), §9 (knowledge/RAG), §20 (AI usage management), §32.1 (ADR format); ADR-006, ADR-012
**Date:** 17 November 2026
**Status:** Implemented and verified end-to-end on a real PostgreSQL 16 + pgvector database

---

## 1. Scope delivered

Phase 3 closes the loop that Phase 2 opened: the knowledge base can now be
*used*, not just searched — under governance that accounts for every model
call.

```
POST /ai/answer {question, org_id}
  → embed question (ADR-012 adapter, embedder-pinned)
  → knowledge.search (org-scoped, TV-012)
  → render active prompt template (§8.3, versioned)
  → model.complete (ADR-006 adapter, DeepSeek first)
  → ai_usage ledger row (§20 — success AND failure)
  → {answer, grounded, citations[], usage{}}
```

Plus versioned prompt/instruction management (`/prompts`) and the usage
summary (`/ai/usage`). All model calls remain behind `ModelAdapter`; no
endpoint ever talks to a provider directly.

## 2. The answer loop

- **Retrieval reuses the Phase 2 store untouched** — same org filter, same
  embedder pinning. A query embedded by a different embedder than the index
  returns zero hits, and the response says so in `retrieval.hint` rather
  than pretending to be ungrounded for a mysterious reason.
- **`grounded` is explicit.** `grounded=true` means retrieval contributed
  context and citations list the exact documents/chunks used (title, chunk
  seq, cosine distance). `grounded=false` means the model answered from
  general knowledge only — the default template *requires* the model to mark
  that distinction in its prose too.
- **One question, one answer, one ledger row.** The endpoint is single-turn:
  no conversation memory, no streaming (both are Phase 4+/6 concerns tied to
  the assignment engine and UI). It stays comfortably inside the HTTP budget.

## 3. Prompt & instruction management (§8.3)

- Templates live in `prompts` + append-only `prompt_versions`; exactly one
  version is active per prompt. Behaviour changes are deliberate events
  (`POST /prompts/{id}/versions`), never silent edits — the same philosophy
  as TV-019 controlled re-indexing.
- **Validation at write time, not answer time:** a template must be non-empty
  and contain exactly the `{{context}}` placeholder (no `{{question}}`, no
  unknown names, case-sensitive). A stored template that somehow fails
  rendering surfaces as a 500 naming the template problem — an operator
  error, loudly.
- The question travels as the user message; only retrieved context goes into
  the system prompt. Orgs can override the default by creating their own
  `rag.answer` prompt; otherwise `DEFAULT_SYSTEM_PROMPT` (grounding rules,
  citation format, "never invent figures") is used without being persisted.
- Templates are org-scoped like everything else; the company default lives
  under `org_id='company'`.

## 4. The usage ledger (§20)

Every model call writes one `ai_usage` row: org, principal, purpose,
provider, model, input/output/cached tokens, duration, estimated cost,
status, and a short detail note (question snippet or error class).

- **Failures are ledger events too** — a model outage is exactly when cost
  and governance visibility matters (verified: simulated provider failure →
  502 to the caller + `status='error'` row).
- **Cost policy:** DeepSeek peak pricing ($0.27/$1.10 per 1M tokens in/out)
  until the off-peak scheduler exists — conservative by design; TV-007 must
  verify against current provider pricing before production.
- **No content in the ledger.** Answers and prompts are not stored — the
  ledger is about cost and governance, not a second copy of conversations.
- Org-scoped throughout; `/ai/usage` sums tokens/cost/errors and breaks them
  down per provider+model.
- **Economics (peak pricing):** a typical answer (~1,200 in + 400 out tokens)
  costs ≈ **$0.000764** — about **1,300 answers per dollar**. Even a
  thousand grounded answers per month is under a dollar; the §20 ledger exists
  for accountability, not because DeepSeek costs are a budget risk.

## 5. API surface

| Endpoint | Guard | Purpose |
|---|---|---|
| `POST /ai/answer` | bearer | grounded Q&A with citations + usage (503 no embedder/model, 502 model failure — ledger row either way) |
| `GET /ai/usage` | bearer | §20 summary: totals + per provider/model, org-scoped, `days` window |
| `POST /prompts` | bearer | create (v1 active); 409 duplicate name, 422 invalid template |
| `GET /prompts` | bearer | list for org (active version + body preview) |
| `GET /prompts/{id}` | bearer | detail incl. all versions; 404 cross-org |
| `POST /prompts/{id}/versions` | bearer | append + activate; 422 invalid template |

A verified Client User (`app_metadata.client_org_id`) is restricted to their
own org on both `/ai/answer` and `/prompts` — same rule as uploads (§16.4).

## 6. Verification evidence

| Suite | What it proves | Result |
|---|---|---|
| `tests/test_phase3.py` (no DB) | template validation/rejection matrix, rendering + residual-placeholder guard, default template grounding rules, context numbering, DeepSeek cost math, API auth fail-closed | 23/23 |
| `tests/integration_phase3.py` (real PG16 + pgvector) | prompt lifecycle (create → 409 → version → activate), grounded answer with citations + prompt version used, adapter usage numbers flowing into the ledger, org-scoped ledgers, ungrounded empty-org path with hint, simulated model failure → 502 + error row | 24/24 |
| `tests/test_phase2.py` + `tests/integration_phase2.py` (regression) | Phase 2 pipeline unaffected by Phase 3 wiring | ALL PASS |
| smoke (no DB) | all 17 routes in OpenAPI, auth fail-closed on `/ai/answer`, `/prompts`, `/ai/usage` | 13/13 |

## 7. Register updates

| Item | Change |
|---|---|
| ADR-006 | DeepSeek remains the first provider, behind `ModelAdapter`; `/ai/answer` composes it without provider knowledge. |
| §8.3 prompts | Implemented: versioned, validated, org-scoped, single-active-version. |
| §20 usage management | Implemented: complete ledger incl. failure rows, cost accounting, org summaries. |
| TV-007 | Cost constants remain INDICATIVE — still must be verified against provider pricing before production. |
| TV-006 | Dev embedder round-trip through the full answer loop verified; production embedder validation still pending. |

## 8. What this phase deliberately did NOT do

- No multi-turn conversation memory (Phase 4 assignment engine will own
  thread state per workflow step).
- No streaming responses (needs the real UI — Phase 4 wires endpoints).
- No off-peak bulk generation (Phase 6 deliverables; the job queue and
  `llm.offpeak-batch` maintenance kind already exist for it).
- No automatic prompt seeding per org — the default is used implicitly until
  an operator creates an override, so behaviour is auditable from day one.
