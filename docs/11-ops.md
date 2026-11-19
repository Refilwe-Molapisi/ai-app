# Phase 8 — Internal Ops — Implementation Notes

**Document ID:** CAIOS-OPS-001
**Reference:** CAIOS-MASTER-001 §30 (roadmap phase 8), §11 (division agents & desks), §13 (governance trail), §7.4/TV-012 (isolation); builds on CAIOS-DEL-001 / CAIOS-QA-001
**Date:** 19 November 2026
**Status:** Implemented; unit + real-Postgres integration suites ALL PASS; frontend verified live in headless Chromium

---

## 1. What this phase adds

The previous phases built the machine; Phase 8 gives the ops desk the
**human roster and the honest numbers** to run it. One new module
(`app/ops/`), two capabilities:

1. **Team roster** — the people behind the §11 desks, per division, with a
   `member.added` / `member.updated` trail (who changed what, by which
   authenticated principal). Roster edits are governance-relevant, so they
   are trailed like every other state change.
2. **Ops overview** — one honest cross-module rollup: roster per division,
   open workload per division (assignments in `running`/`awaiting_approval`,
   allocated via the workflow→division map), queue health, deliverable
   build/QA-gate states and the 30-day AI ledger. It **counts rows in the
   other modules' own stores** — it invents nothing.

## 2. The four desks

The master spec's §11 names three delivery divisions served by the
specialist agents. The roster uses exactly those strings (shared with the
agent registry, asserted by the unit suite) plus the desk that runs
everything without an agent step:

| Division | Served by | Workflows allocated |
|---|---|---|
| BI & Analytics | `bi-analytics` agent (§11.1) | `company.bi-diagnostic` |
| Commercial Intelligence | `commercial-intelligence` agent (§11.2) | `client.competitor-scan` |
| Risk & Controls | `risk-controls` agent (§11.3) | `client.controls-review` |
| Governance & Operations | the MD / company-secretary desk | `company.knowledge-brief`, `client.onboarding-check`, all human gates |

The allocation lives in one file (`ops/workflows_map.py`). The unit suite
asserts it **exactly covers** the coordination workflow registry and that
agent-executed workflows map to their executing agent's division — the two
registries cannot drift apart silently, and an unknown workflow falls back
to Governance & Operations rather than being misattributed.

## 3. API surface

```
POST /ops/members                 add a member (division validated, unique name per org)
GET  /ops/members                 roster (active by default; ?include_inactive=true)
PATCH /ops/members/{id}           role / division / active — trailed in the same transaction
GET  /ops/members/{id}/events     one member's roster trail
GET  /ops/overview                the cross-module rollup described above
```

Guards: unknown division → 422; duplicate name in org → 422; cross-org →
404 (TV-012); the actor recorded on the trail is the verified principal.

## 4. Frontend

A new **Internal Ops** view: four division cards (active members, open
assignments, allocated workflows), a **machine-health** panel (queue
pending/failed, deliverables awaiting QA vs approved, AI calls/spend/errors
over 30 days), the team table with deactivate/reactivate, and the roster
trail feed. Demo mode ships a full synthetic roster and guards every action.
There is deliberately **no "tick the queue" button** — the worker secret
belongs to the worker, not the browser; the health panel shows the queue
state instead.

## 5. Verification evidence

| Check | Result |
|---|---|
| Unit (`tests/test_phase8.py`): ops map covers the registry exactly (no stale entries), agent workflows map to their agent's division, G&O fallback, schema objects | ALL PASS |
| Integration (`tests/integration_phase8.py`, real PG, six schemas): roster CRUD + duplicate/division 422s; PATCH trailed with principal provenance; deactivation hides from active roster; member events endpoint; overview counts seeded rows correctly (team per division, open workload per division, deliverable build/QA counts, queue + AI sections); cross-org empties and 404s | 19/19 PASS |
| Full battery after wiring (all fourteen suites incl. phases 2–7 + smoke) | ALL PASS |
| Frontend live in Chromium: Ops view in LIVE mode (division cards + health from real rows), add-member modal → trail update, demo mode | verified (commit message §) |

## 6. Deliberately not in this phase

- **Individual-level assignment of work** — assignments belong to
  engagements and workflows; tying a person to a step is a Phase 9+
  concern (deployment first, per §30). The division rollup is the honest
  unit of workload today.
- **Leave/calendar management, payroll** — out of scope for CAIOS; the
  roster carries exactly what governance needs (who is on which desk, and
  since when).
- **Fine-grained RBAC on the roster** — the Supabase role model (§16)
  already gates who reaches the API; per-endpoint roles arrive with the
  production deployment wiring.
