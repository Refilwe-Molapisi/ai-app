# Phase 2 — Knowledge & Documents: Implementation Notes

**Document ID:** CAIOS-KNOW-001
**Reference:** CAIOS-MASTER-001 §7 (documents), §9 (knowledge/RAG), §30 (phase plan); ADR-009, ADR-012; CAIOS-INFRA-001 (hosting blueprint)
**Date:** 16 November 2026
**Status:** Implemented and verified end-to-end on a real PostgreSQL 16 + pgvector database

---

## 1. Scope delivered

Phase 2 turns the stubbed `knowledge.*` job kinds of the Phase 0–4 skeleton
into a working ingestion-and-retrieval pipeline:

```
upload (multipart, §7.2)
  → object storage (S3-compatible, client-keyed §7.4)
  → knowledge.extract   (text extraction, provenance §9.4)          [job]
  → knowledge.embed     (chunk → embed → pgvector upsert)           [job, chained]
  → /knowledge/search   (cosine retrieval, org-scoped)
```

Both stages run as ordinary queue jobs inside the `/jobs/run` cron budget —
no orchestrator, no always-on worker. `knowledge.extract` enqueues
`knowledge.embed` as its final step, so one upload drains to a searchable
index across successive cron ticks.

## 2. Schema decisions (`backend/app/knowledge/schema.sql`)

1. **Metadata in Postgres, bytes in storage (§7.3).** `documents` stores
   keys, checksums, sizes and ingest state; the binary never enters the
   database. `documents.extracted_text` is derived, reproducible text kept
   solely so re-chunking (TV-019) never needs re-extraction.
2. **Untyped `vector` column.** pgvector's HNSW/IVFFlat indexes require a
   fixed dimension, but the production embedder is not final (ADR-012 target:
   BGE-M3, 1024 dims; dev hash embedder: 256). Shipping an untyped column
   plus per-chunk `embed_provider/model/dimensions` lets both coexist and
   makes the dimension pin a one-line versioned migration, documented in the
   schema header. At free-tier corpus scale a sequential cosine scan is fast
   enough; the index is a documented upgrade, not silent debt.
3. **Denormalised `org_id` on chunks.** Search filters on the chunk row
   itself, so isolation does not depend on a join behaving (§7.4, TV-012).
4. **`UNIQUE (document_id, seq)` and `UNIQUE (storage_key)`.** Re-indexing
   replaces all chunks atomically (`store.index_chunks` — DELETE + INSERT in
   one transaction), so a re-run can never duplicate or half-write. A second
   document claiming the same storage object is rejected as a 409 conflict —
   one object, one canonical document row.

## 3. Extraction (`extraction.py`)

| Type | Handler | Notes |
|---|---|---|
| TXT / MD / CSV / LOG | UTF-8 decode (latin-1 fallback) | JSON is pretty-printed so keys survive chunking |
| HTML | stdlib parser | script/style stripped, paragraphs separated |
| PDF | pypdf | encrypted PDFs raise `EncryptedDocument` — never guessed |
| DOCX | python-docx | paragraphs **and table cells** (management-accounts tables) |

Dispatch is by extension first, mime second (SARS/bank portals often send
`application/octet-stream` with a real extension). Every result carries an
extractor id (`pypdf@6.x`, `python-docx@1.x`, …) recorded on the document row
— §9.4 provenance, enabling selective re-extraction after an extractor fix.
All handlers are pure Python; the container image stays slim and identical
across Layers 1–3.

## 4. Chunking (`chunking.py`)

Deterministic, paragraph-aware, with word-aligned overlap:

- whole paragraphs packed up to `KNOWLEDGE_CHUNK_CHARS` (default 1200 ≈ 300–400 tokens);
- oversized paragraphs hard-split at word boundaries;
- continuation chunks start with the tail of the previous chunk
  (`KNOWLEDGE_CHUNK_OVERLAP`, default 150), so a fact at a boundary is
  complete in at least one chunk;
- `char_start/char_end` offsets into the extracted text — retrieval results
  can cite back into the source document later;
- re-chunking the same text with the same parameters yields identical chunks
  (asserted by tests).

## 5. Embedding contract (ADR-012)

- The embedder is resolved exactly like in the skeleton: `EMBEDDING_ENDPOINT`
  set → `OpenAICompatEmbedder`; unset → `DeterministicHashEmbedder`
  (dev/tests only — pseudo-embeddings, honest about it in provider name).
- Chunk vectors are written in batches (`KNOWLEDGE_EMBED_BATCH`) with the
  batch's provider/model/dimensions stored per chunk **and** on the document.
- **Cross-model comparison is impossible by construction:** searches pin
  provider+model+dimensions. A query embedded by a different embedder returns
  an empty result plus a re-index hint, not silently meaningless scores.
- Re-index flow (TV-019): `POST /documents/{id}/reindex` re-embeds with the
  *current* embedder; chunks are replaced atomically. Verified in the
  integration suite: 256-dim index → 512-dim re-index → old-model search
  refused → re-index back → searchable again.

## 6. API surface (`/documents`, `/knowledge`)

| Endpoint | Guard | Purpose |
|---|---|---|
| `POST /documents/upload` | bearer | multipart → storage → extract job (202). Size cap 413, unsupported type 422, duplicate key 409 |
| `GET /documents` | bearer | metadata list for one org (no text, no binaries) |
| `GET /documents/{id}` | bearer | ingest status/index metadata; org-scoped (404 cross-org) |
| `POST /documents/{id}/reindex` | bearer | manual TV-019 trigger |
| `POST /knowledge/search` | bearer | embed query → cosine search, org-scoped |
| `GET /knowledge/stats` | bearer | corpus snapshot per org |

**Isolation model (§7.4, TV-012):** `org_id` is explicit on every route and
every query; cross-org reads return 404/empty rather than an error that
confirms existence. A verified Client User (`app_metadata.client_org_id`)
cannot upload outside their own org (403). In Phase 4 the workflow engine
derives `org_id` from verified claims instead of trusting the client — the
single remaining trust boundary is documented in `routes.py`.

## 7. Free-tier budget (blueprint §9)

A 1200-char chunk costs ≈ 1 KB of text + 1 KB of vector at 256 dims
(4 KB at 1024 dims) + row overhead ≈ **3–6 KB per chunk**. A typical
engagement pack (50 documents × 5,000 chars ≈ 170 chunks) indexes at ≈ 1 MB.
Supabase's 500 MB ceiling therefore supports a corpus of tens of thousands of
chunks long before any upgrade is needed; `KNOWLEDGE_MAX_UPLOAD_MB` (20) and
the corpus cap in the blueprint remain the guards.

## 8. Verification evidence

| Suite | What it proves | Result |
|---|---|---|
| `tests/test_phase2.py` (no DB) | extraction matrix incl. encrypted/unsupported; chunking determinism/bounds/coverage/paragraph integrity/overlap; embedder contract; pgvector literal; registry wiring | 29/29 |
| `tests/integration_phase2.py` (real PG16 + pgvector, user-space) | full public-API pipeline upload→extract→embed→search; org isolation; TV-019 re-index cycle incl. embedder switch and closed loop; 409 duplicates; encrypted-PDF failure → doc `failed` + job retry; demo.echo regression | 47/47 |
| smoke (API contract, no DB) | routes exposed, auth fail-closed on upload/search, 422 pre-storage, Phase 0–4 checks unchanged | 10/10 |

The integration suite runs against `DATABASE_URL` if set (e.g. the compose
db) or boots its own user-space Postgres via `pip install pgserver` — no
Docker or root required.

**Real-database verification paid for itself twice:** the Phase 0–4 queue had
a latent psycopg bug (rows read by column name while psycopg3 defaults to
tuples) that no 503-path smoke test could ever catch — fixed by pinning
`row_factory=dict_row` on both pools — and multi-row chunk inserts needed
`executemany`, not `execute`. Both classes of bug are now covered by tests
that talk to an actual server.

## 9. Register updates

| Item | Change |
|---|---|
| ADR-012 | **Implemented.** Embedding adapter independent of LLM; deterministic dev embedder + OpenAI-compatible endpoint shipped; BGE-M3 local target unchanged. |
| TV-006 | Partially validated locally (deterministic dev embedder, real pgvector round-trip). Full validation still requires the production embedder + endpoint. |
| TV-012 | Server-side isolation proven at the query level (org filter on chunks; cross-org 404/empty). Claim-derived scoping lands in Phase 4. |
| TV-019 | Implemented: atomic chunk replacement, model pinning, cross-model refusal, manual re-index endpoint. |
| TV-016 | Retry/backoff observed for a failing extraction job (encrypted PDF); dead-letter after max attempts as per runner. |

## 10. What this phase deliberately did NOT do

- No LLM calls anywhere in the pipeline (Phase 3 consumes retrieval; POPIA
  gate ADR-014 still applies to anything client-confidential reaching an
  external model).
- No OCR for scanned PDFs (pypdf extracts text layers only — scanned packs
  are marked failed with a clear error; OCR joins as another extractor).
- No UI changes — the prototype remains a design artefact until Phase 4 wires
  real endpoints.
