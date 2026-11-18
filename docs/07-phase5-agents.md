# Phase 5 — Specialist Agents: Implementation Notes

**Document ID:** CAIOS-AGNT-001
**Reference:** CAIOS-MASTER-001 §8.3 (versioned prompts), §11 (specialist AI agents), §20 (usage ledger), §30 Phase 5; CAIOS-COORD-001 (Phase 4 engine), CAIOS-AI-001 (Phase 3 service)
**Date:** 18 November 2026
**Status:** Implemented and verified end-to-end on a real PostgreSQL 16 + pgvector database

---

## 1. Scope delivered

The three §11 division agents now exist as governed, registry-defined
specialists that execute only through the Phase 4 coordinator — never as
free-form endpoints. §11.4's target architecture is now literally in place:
**one central coordinator, three specialist agents, shared operational
capabilities** (knowledge retrieval = Phase 2, generation core = Phase 3,
queue/engine = Phases 0–4).

| Slug | § | Division | Purpose |
|---|---|---|---|
| `bi-analytics` | §11.1 | BI & Analytics | Business data diagnostics, revenue/expenditure/sales analysis, KPIs, trends, variances, management reporting |
| `commercial-intelligence` | §11.2 | Commercial Intelligence | Competitor/market/industry research, pricing & supplier comparisons, opportunity assessments |
| `risk-controls` | §11.3 | Risk & Controls | Risk identification, register development, process diagnostics, control gaps, SOP drafting |

Each definition carries the five things §30 Phase 5 requires, as data:

- **Responsibilities** — the §11.1–11.3 duty lists, verbatim in intent.
- **Permitted tools** — `knowledge.retrieval`, `model.reasoning` (the Phase 6
  document/analysis tools join this list when they exist; nothing else is
  permitted by construction because nothing else is registered).
- **Required outputs** — the contract every run's answer must satisfy
  (e.g. commercial research MUST preserve sources and separate verified
  facts / estimates / assumptions / unavailable information).
- **Quality standards** — honesty and traceability rules, baked into the
  system prompt of every call.
- **Constraints** — the §11 must-nots (no altering accounting records, no
  certifying financials, no inventing sources, no legal/audit/regulatory
  opinions), rendered under a "NEVER VIOLATE" heading on every call.

## 2. How an agent runs

```
POST /assignments {workflow: client.competitor-scan, inputs:{subject}}
  → plan_assignment validates the agent slug + renders the question concrete
  → 'assignment.step' job → _exec_agent_task → run_agent()
      retrieval (org-scoped, embedder-pinned)
      → instructions: org's active versioned override ('agent.<slug>' via
        /prompts) or the §11 defaults — an override WITHOUT {{context}} is
        refused (never run ungrounded)
      → model call → §20 ledger row purpose='agent' (failures too)
  → step output carries the agent identity block (who produced what)
  → gate → approve → completed (full §13 event trail)
```

Deliberate choices:

- **No ad-hoc `/agents/{slug}/run` endpoint.** Ungoverned invocation would
  bypass scopes, gates and the assignment trail; the unit suite asserts the
  route does not exist. Direct use is available to tests via `run_agent()`.
- **Instruction overrides are versioned prompts.** Orgs may reword agent
  duties through the existing `/prompts` system (name `agent.<slug>`);
  behaviour changes stay versioned events (§8.3). The grounding block
  (`{{context}}`) is structurally mandatory — for defaults via registry
  validation, for overrides via a run-time refusal.
- **The §20 ledger gained the `agent` purpose** — with an idempotent
  in-place constraint migration in `app/ai/schema.sql` (DROP + ADD CHECK),
  so existing deployments upgrade at next boot. Cost attribution per agent
  works from day one.

## 3. New workflows (registry now has five)

| Workflow | Inputs | Steps |
|---|---|---|
| `company.bi-diagnostic` | `topic` | kb.check (intake) → **bi-analytics** → gate |
| `client.competitor-scan` | `subject` | **commercial-intelligence** → gate |
| `client.controls-review` | `process` | **risk-controls** → gate |

Agent slugs and question templates are validated at assignment creation —
an unregistered agent or a bad placeholder is a fast 422, never a runtime
surprise (the Phase 4 planning rule extended to agents).

## 4. Verification evidence

| Suite | What it proves | Result |
|---|---|---|
| `tests/test_phase5.py` (no DB) | registry self-validation + rejection matrix; §11 constraints present per agent; grounding block mandatory; summary shape; `agent.task` kind/executor agreement; planning-time agent validation (unknown slug → 422); `/agents` API contract; **no ad-hoc run endpoint** | 29/29 |
| `tests/integration_phase5.py` (real PG16 + pgvector) | full competitor-scan lifecycle (agent step grounded with citations, identity block, instructions verified at the model boundary, §20 `agent` ledger row, gate → completed); bi-diagnostic (intake check before agent); controls-review on a second org; cross-org 404; cancel at gate | 19/19 |
| Phase 2/3/4 suites (regression) | service refactor + constraint migration broke nothing | ALL PASS |
| smoke (no DB) | all 23 routes in OpenAPI, auth fail-closed on `/agents` | ALL PASS |

## 5. Register updates

| Item | Change |
|---|---|
| §11.1–11.3 | The three agents implemented as registry definitions with duties, tools, outputs, standards and constraints carried as data. |
| §11.4 | Architecture realised: coordinator + 3 specialists + shared capabilities; no per-service bots. |
| §8.3 | Agent instruction overrides are versioned prompts; grounding block structurally enforced. |
| §20 | `agent` purpose added with in-place migration; per-agent cost attribution available. |
| §30 Phase 5 | Responsibilities / workflows / permitted tools / required outputs / quality standards: all defined and exercised. |

## 6. What this phase deliberately did NOT do

- **Numerical/analysis tools.** `bi-analytics` is permitted `knowledge.retrieval`
  and `model.reasoning` only; real computation (spreadsheet processing,
  statistical analysis) lands with the Phase 6 tools and then joins the
  agent's permitted set. The instructions explicitly forbid unverifiable
  arithmetic until then.
- **External web research.** Commercial intelligence is grounded in the
  client/company knowledge base; web search integration (and source
  preservation for it) is a separate capability to be added and verified.
- **Multi-turn agent memory.** One governed action per step; conversation
  threads arrive with the UI phase.
- **Agent-level output schemas** (JSON-structured findings) — candidates
  with Phase 6 deliverables; prose contracts are enforced today.
