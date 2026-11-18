# Phase: Production Frontend (Vercel) — Implementation Notes

**Document ID:** CAIOS-FE-001
**Reference:** CAIOS-MASTER-001 §10 (operator interface), §16 (auth model), CAIOS-DESIGN-001 (brand tokens), CAIOS-INFRA-001 (hosting layers), CAIOS-COORD-001/CAIOS-AGNT-001 (API contract)
**Date:** 18 November 2026
**Status:** Implemented, verified in headless Chromium in both DEMO and LIVE modes; deployable to Vercel as a static site

---

## 1. Repository structure decision: monorepo

The frontend lives **inside this repository** at `frontend/` — not in a separate
repo. Vercel fully supports monorepos: the project's **Root Directory** is set
to `frontend/`, and Vercel builds/deployes only that path (static preset, no
build step). Reasons recorded for the decision log:

- **Atomic contract changes** — the API's response shapes and the UI that
  renders them land in the same commit; a cross-repo deployment could
  temporarily pair a new API with an old UI (or vice versa).
- **One governance surface** — brand tokens (docs/02), the API contract and
  the ADRs live here; the frontend references them directly.
- **Ops cost at $0** — one clone, one push, one bundle backup
  (`caios-git-backup.bundle`), one CI context. A second repo doubles the
  secrets and the push/pull bookkeeping for zero benefit at this scale.
- The backend stays on Render (Layer 2 blueprint); the frontend gets its own
  Vercel project — no coupling in deployment, only in git.

## 2. What was built

`frontend/index.html` — a single-file, no-build-step operator console
(vanilla JS + CSS, brand tokens from docs/02: Libre Baskerville display,
Montserrat UI, navy/green palette). It is the production evolution of
`prototype/caios-ui.html`, wired to the real API surface:

| View | API | Notes |
|---|---|---|
| Overview | `/assignments`, `/agents`, `/knowledge/stats`, `/ai/usage` | 4 live stat tiles, recent work, registry status |
| Assignments + detail | `/assignments[+ approve / cancel]` | Filter chips; detail renders steps, agent identity block, answer, citations, §13 event trail; gate approve/cancel with recorded notes |
| Knowledge | `/documents`, `/documents/upload`, `/knowledge/search` | Metadata table, multipart upload (company/client scope), semantic search with distances |
| Specialist Agents | `/agents` | The three §11 agents: duties, tools, outputs, standards, constraints; one-click **Start workflow** per agent |
| AI Console | `/ai/answer` | Grounded flag, citations, prompt version, usage, honest ungrounded hint |
| Usage & Spend | `/ai/usage` | §20 totals: calls, errors, tokens, estimated cost, by-model table |
| Workflows | `/workflows` | Registry cards with step flow + required inputs → start modal |
| Connection | — | API base/org/token form (localStorage; URL params `?api=&org=&token=` override), health test, deployment notes |

Design invariants carried from the prototype: **LIVE/DEMO honesty** — without
an API the app shows clearly-badged synthetic demo data and every mutating
action is blocked with a toast; with an API it shows the real ledger numbers.
No credentials are embedded; the token lives in the operator's browser only.

## 3. Backend change that makes Vercel work: `CORS_ORIGINS`

The API's CORS allow-list previously hardcoded localhost (and allowed nothing
in production). It is now driven by a new setting:

- `CORS_ORIGINS` (comma-separated, field maps 1:1 in `Settings` — this
  codebase uses **no env prefix**, so the variable name is exactly
  `CORS_ORIGINS`, matching `DATABASE_URL`/`JOB_RUN_SECRET` convention).
- Localhost origins and `null` (file:// prototype) are still auto-added
  outside production; production allows **only** configured origins —
  never a wildcard.

Set it on Render to the deployed frontend, e.g.:

```
CORS_ORIGINS=https://caios.vercel.app,https://caios-git-main-refilwe-molapisi.vercel.app
```

## 4. Deploying the frontend on Vercel

1. Import the `Refilwe-Molapisi/ai-app` repo into Vercel.
2. **Root Directory:** `frontend`
3. **Framework Preset:** Other (static) — `frontend/vercel.json` pins
   `framework: null`, no install/build command, output `.`.
4. Deploy — every push to `main` touching `frontend/` redeploys.
5. Copy the production URL (and the git-branch preview URL pattern) into the
   API's `CORS_ORIGINS` on Render and redeploy the API once.

Security headers (nosniff, DENY framing, referrer policy) ship in
`frontend/vercel.json`; `cleanUrls` is enabled.

## 5. Verification evidence

| Check | Result |
|---|---|
| JS syntax (`node --check` over extracted scripts) | OK |
| DEMO mode: badge, 4 stat tiles, 4 seeded assignments, filters, detail (steps + answer + citations + events), 3 agent cards, 5 workflow cards | verified in Chromium, 0 page errors |
| LIVE mode (real API + real PG16): LIVE badge · "3 agents online" footer, real tiles, **gate approve through the prompt-dialog → COMPLETED with 7 events**, **New assignment from an agent card (dynamic inputs) → RUNNING/QUEUED**, agents/usage/knowledge views on real data | verified |
| `CORS_ORIGINS` preflight: OPTIONS from the UI origin → 200 + `access-control-allow-origin` | verified (and 400/`Disallowed CORS origin` before, proving the list is enforced) |
| 375 px mobile: no horizontal overflow, sidebar collapses to row nav, tiles stack | verified + screenshots |
| Full backend battery re-run after the CORS change | ALL PASS (all 9 suites) |

## 6. Deliberately not in this phase

- **Supabase login UI** — auth arrives when TV-011/013 wiring lands; the
  Connection view already accepts a real Supabase JWT as the token today.
- **Prompt management UI** — `/prompts` stays a governed CLI/API surface for
  now; admin UI can follow with role-scoped views.
- **Deliverables viewer** — Phase 6 (documents/reports) brings download views.
