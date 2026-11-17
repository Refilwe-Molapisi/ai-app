# Phase 4 — Central Coordination: Implementation Notes

**Document ID:** CAIOS-COORD-001
**Reference:** CAIOS-MASTER-001 §11 (assignment engine), §12 (workflow states), §13 (governance trail), §32.1 (ADR format); ADR-009 (queue), CAIOS-KNOW-001 (Phase 2), CAIOS-AI-001 (Phase 3)
**Date:** 17 November 2026
**Status:** Implemented and verified end-to-end on a real PostgreSQL 16 + pgvector database

---

## 1. Scope delivered

Phase 4 is the layer that turns CAIOS from a set of capabilities into an
*operating system*: work enters as an **assignment**, follows a declarative
**workflow** of ordered steps, executes through the existing job queue, pauses
for humans where the workflow says so, and leaves an append-only **event
trail** an auditor can read top to bottom.

```
POST /assignments {org_id, workflow, inputs}
  → plan_assignment (pure validation: workflow + inputs → CONCRETE steps)
  → assignments + assignment_steps + assignment_events rows (one transaction)
  → first automatic step enqueued as an 'assignment.step' queue job
  → runner executes it inside the /jobs/run budget (ADR-009 / TV-008)
  → engine advances: next step … or gate → awaiting_approval … or completed
POST /assignments/{id}/approve   → consumes the open gate, walks forward
POST /assignments/{id}/cancel    → cancels jobs, steps, assignment (logged)
```

The registry ships two workflows proving the two step modes:

| Workflow | Steps | Mode |
|---|---|---|
| `company.knowledge-brief` | `brief` (ai.brief) → `review` (gate) | automatic LLM step + human gate |
| `client.onboarding-check` | `index-check` (kb.check) → `review` (gate) | automatic readiness check + human gate |

Adding a workflow is one entry in `app/coordination/workflows.py` — the
engine, handlers and routes are workflow-agnostic (ADR-006's
"selection-point" philosophy applied to workflows).

## 2. Design decisions

- **Creation-time planning (`plan_assignment`, pure).** Workflow resolution,
  required-input checking and question-template rendering all happen BEFORE
  any database or pool is touched: an unknown workflow, a missing `{topic}`
  or a template referencing an input nobody provided is a fast 422 to the
  caller — never a runtime surprise five jobs deep. Steps are stored
  CONCRETE (rendered params), so the executor needs no templating logic.
- **Fail-fast steps.** A step error marks the step *and* the assignment
  `failed` (error preserved on both + `assignment.failed` event) and
  re-raises so the queue's bookkeeping still applies. A retried job
  re-enters the handler, sees the terminal state and exits as a no-op skip —
  no double spend, no stuck assignments. Transient resilience comes from
  re-creating the assignment; a `retry` endpoint is a Phase 5+ candidate.
- **Gates pause before consumption.** The gate step sits in
  `awaiting_approval` (assignment: `awaiting_approval`) until approve —
  approval consumes it and enqueues whatever follows. Nothing may follow a
  gate except another step sequence end (registry self-check: at most one
  trailing gate, validated at import AND in unit tests).
- **Events are governance, not logging.** Every transition — created, step
  queued/started/completed, awaiting_approval, gate approved, completed,
  cancelled, failed — is one append-only `assignment_events` row with actor
  and JSON detail. The API returns the full trail on the detail endpoint.
- **Org isolation end to end (TV-012).** Routes take `org_id` explicitly,
  verified Client Users are locked to their own org (403), cross-org reads
  are 404; the queue handler loads by assignment id (internal), and every
  step writes back through org-independent ids it already owns.
- **Workflow model calls go through the shared AI service** (Phase 3's
  `grounded_answer`, extracted to `app/ai/service.py`). The `/ai/answer`
  route is now a thin wrapper over the same function — one governed path,
  one §20 ledger, no drift between API answers and workflow answers.

## 3. API surface (Phase 4 additions — 22 routes total)

| Endpoint | Guard | Purpose |
|---|---|---|
| `GET /workflows` | bearer | registry listing: names, descriptions, required inputs, step plans |
| `POST /assignments` | bearer | start an assignment (201; 422 unknown workflow / missing inputs, fast, DB-free) |
| `GET /assignments` | bearer | org list with status filter and per-assignment step counters |
| `GET /assignments/{id}` | bearer | full detail: steps (status, output, errors) + event trail |
| `POST /assignments/{id}/approve` | bearer | consume the open gate (409 when nothing is open) |
| `POST /assignments/{id}/cancel` | bearer | cancel a non-terminal assignment (409 on terminal) |

`assignment.step` (the queue kind) is registered in the runner's default
registry like every other kind; the runner budget applies unchanged.

## 4. UI prototype: live-API layer

`prototype/caios-ui.html` now has a live-API layer with graceful fallback:

- Configure via URL or localStorage: `caios-ui.html?api=http://localhost:8000&org=company&token=dev`
- **No API configured → DEMO DATA badge** (synthetic data unchanged);
  **API unreachable → API UNREACHABLE badge** (demo data stays);
  **API reachable → LIVE badge**: the assignments table, pipeline donut and
  the AI-spend stat render from `GET /assignments` + `GET /ai/usage`; rows
  awaiting approval expose a working **Approve** button, and *New assignment*
  really starts a workflow via `POST /assignments`.
- Dev CORS now includes the `null` origin (file://) — production origins stay
  configured, never wildcards.

Verified in headless Chromium: both modes render, no console errors; live
mode exercised against a real API + Postgres showing real assignment rows and
the live usage stat.

## 5. Verification evidence

| Suite | What it proves | Result |
|---|---|---|
| `tests/test_phase4.py` (no DB) | registry self-validation + rejection matrix (empty steps, unknown kind, mid-pipeline gate, two gates), pure planning (concrete params, template-input mismatches), executor/registry agreement, API contract incl. DB-free 422s and auth fail-closed | 25/25 |
| `tests/integration_phase4.py` (real PG16 + pgvector) | full knowledge-brief lifecycle (create → real queue job → grounded step output with citations → gate → approve → completed + events), §20 ledger records the workflow model call, onboarding-check ready path, cancel path (job + steps + event), honest fail-fast on an empty org, cross-org 404, status filters, unknown workflow 422 | 28/28 |
| Phase 2 + 3 suites (regression) | service extraction broke nothing; all prior guarantees hold | ALL PASS |
| smoke (no DB) | all 22 routes in OpenAPI, auth fail-closed on the new endpoints | ALL PASS |
| Headless Chromium | demo fallback + live-API mode of the prototype | both render clean |

## 6. Register updates

| Item | Change |
|---|---|
| §11 assignment engine | Implemented: declarative registry, concrete step materialisation, queue-executed steps, gates, fail-fast semantics. |
| §12 workflow states | Implemented per the state machine above; verified through the API. |
| §13 governance trail | Implemented: append-only events with actor + detail on every transition. |
| ADR-006 | The AI service extraction keeps provider selection single-sourced; workflow steps compose it. |
| TV-008 | Step jobs respect the runner budget like all jobs (single LLM call per step fits comfortably). |
| TV-012 | Org isolation extended to assignments, steps and events. |

## 7. What this phase deliberately did NOT do

- No specialist agents yet (Phase 5 registers real agent step kinds in the
  same handler table — the `ai.brief`/`kb.check` executors are the pattern).
- No deliverable generation (Phase 6: `report.generate` behind the same
  registry; the TODO marker already sits in the runner).
- No multi-turn threads / conversation memory per step (single governed
  actions; threads arrive with the UI and agent phases).
- No `retry` endpoint for failed assignments (re-create works today; retry
  with reuse of completed steps is a Phase 5+ candidate — recorded here so
  it is a decision, not an omission).
- No email notifications on gate arrival (Resend lands with Phase 5+; the
  event trail already carries the approval request).
