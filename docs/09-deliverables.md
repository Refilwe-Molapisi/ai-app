# Phase 6 — Deliverables & Document Generation — Implementation Notes

**Document ID:** CAIOS-DEL-001
**Reference:** CAIOS-MASTER-001 §30 (roadmap phase 6), §7.3/§7.4 (storage & isolation), §13 (governance trail), §20 (usage ledger); CAIOS-INFRA-001 (hosting layers); CAIOS-COORD-001 (events trail pattern)
**Date:** 18 November 2026
**Status:** Implemented; unit + real-Postgres integration suites ALL PASS; frontend verified live in headless Chromium

---

## 1. What this phase adds

The final step of the §1 value chain: analyses become **client-ready
artefacts**. A deliverable is a structured spec rendered deterministically
into one of four formats and stored as a governed object:

| Kind | Artefact | Builder |
|---|---|---|
| `md` | Markdown brief | dependency-free |
| `docx` | Word report | python-docx (already in the image since Phase 2 extraction) |
| `xlsx` | Excel workbook (Summary + one sheet per table) | openpyxl |
| `pdf` | PDF report (platypus, paginated, page footers) | reportlab |

Pipeline: `POST /deliverables` → row (`queued`) + `report.generate` job →
runner renders the bytes → storage adapter `put()` → row (`ready`) with
storage key, size and SHA-256 → event trail records every transition.
The runner registry's own `TODO(Phase 6)` pointer from Phase 4 is now
fulfilled (`report.generate` registers in `default_registry`).

## 2. The spec (version 1)

```json
{
  "meta":     {"prepared_for": "Example (Pty) Ltd", "period": "Q3 2026", "author": "CAIOS"},
  "sections": [{"heading": "Findings", "body": "…", "bullets": ["…"]}],
  "tables":   [{"title": "Revenue by line", "columns": ["Line","Q2","Q3"],
                "rows": [["Product", 1200, 1350]]}]
}
```

Limits (route schema *and* builder, belt and braces): ≤ 50 sections, ≤ 50
bullets each, ≤ 10 tables, ≤ 30 columns, ≤ 5 000 rows, 20 000 chars per body.
Empty specs and ragged rows are honest 422s naming the offending row.

## 3. Governance properties

- **Deterministic** — no model calls inside generation; a spec renders
  identically every time. AI-sourced content enters *through the spec* (e.g.
  the AI console's one-click "Export as deliverable"), and that generation
  was already ledgered at the AI layer (§20). No ledger rows are written here
  because no model is called here.
- **Provenance footer** — every artefact ends with CAIOS, the UTC timestamp
  and "verify figures before external use"; the PDF also numbers pages.
- **Append-only trail (§13)** — `deliverable_events` records
  `created → generating → ready | failed` with actor and detail
  (including the checksum on ready).
- **Isolation (§7.4, TV-012)** — `org_id` on every row and read; cross-org
  reads are 404s (existence never leaked); client-scope artefacts store under
  `clients/{org}/{engagement}/deliverables/…`, company artefacts under
  `company-knowledge/deliverables/{org}/…`.
- **Private storage** — the database stores keys/metadata only (§7.3);
  downloads **stream through the authorised API** (`GET
  /deliverables/{id}/download`) — no public or presigned URLs.
- **Injection guard** — spreadsheet cells starting `= + - @` are stored as
  literal text (apostrophe prefix), so a hostile spec can never plant a
  formula that executes on open.
- **Render safety** — PDF text is NFKD-transliterated to latin-1 (combining
  marks stripped): standard fonts can never crash or emit tofu.
- **Honest failure** — generation/storage errors mark the row `failed` with
  the real reason AND fail the job, so TV-016 retry applies (verified: a
  simulated storage outage fails once, then completes after backoff).

## 4. Storage adapter extension (dev convenience, production unchanged)

`STORAGE_LOCAL_DIR` opts into `LocalFileStorage` — a plain-directory
implementation of the same interface with path-traversal refusal and **no
presigned URLs** (downloads must stream through the API). Selection order in
`get_storage_adapter()`: local dir → S3-compatible → loud RuntimeError.
Layer 1 (local, zero cloud services) can now run the *entire* pipeline
including artefacts; production keeps the private-bucket posture
(§7.4) and never sets the variable. The knowledge pipeline inherits the same
option automatically — document upload stops requiring S3 in dev.

## 5. API surface

```
POST /deliverables                  202 {id, status, job_id}   (spec validated)
GET  /deliverables?org_id&limit     {count, deliverables[]}    (metadata, no specs)
GET  /deliverables/{id}?org_id      {deliverable, events[]}    (spec + trail)
GET  /deliverables/{id}/download    bytes                      (409 until ready)
```

## 6. Frontend

New **Deliverables** view (status pills, size, download buttons), a
**builder modal** (sections editor + optional table, format/scope select),
authorised downloads via `fetch` → blob (the Bearer header rides along,
unlike a plain `<a href>`), and the AI console's **Export as deliverable**
button, which packages the last grounded answer + citations as a Markdown
brief spec. Demo mode carries clearly-badged rows and blocks all actions.

## 7. Verification evidence

| Check | Result |
|---|---|
| Unit suite (`tests/test_phase6.py`): spec validation errors; all four builders reopened and structurally verified (docx headings/tables, xlsx sheets/freeze/formula-guard/numeric cells, pdf text extraction + unicode transliteration, md structure); LocalFileStorage put/get/delete/traversal; storage-key prefixes | 26/26 PASS |
| Integration (`tests/integration_phase6.py`, real PG): 202 → job → ready with checksum → download streams exact bytes → trail created→generating→ready; all four kinds download with correct mime + magic bytes; cross-org 404s; client-scope key prefix; storage-outage → failed row + failed job + retry completes after backoff; 422 validation class | 24/24 PASS |
| Full battery after wiring (all nine suites incl. phases 2–5 + smoke) | ALL PASS |
| Frontend in headless Chromium against a real API: list, build modal, queue → ready → download; export-from-AI-answer; demo-mode guard; no console errors | verified (§9 below) |

## 8. Deliberately not in this phase

- **Spec versioning/regenerate** — `spec_version` is reserved; re-render
  arrives with revision workflows (Phase 7 QA governance).
- **PPTX kind** — decks need a layout engine; the `kind` CHECK constraint
  admits it without a migration when justified.
- **Template library** — house-branded templates (fonts, cover pages) come
  with TV-024 branding work; the docx/pdf builders already isolate
  style decisions in one place each.
