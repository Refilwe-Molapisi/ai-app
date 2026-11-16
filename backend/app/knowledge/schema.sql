-- CAIOS knowledge & documents schema (Phase 2 — CAIOS-MASTER-001 §7, §9).
-- Idempotent, like the jobs schema; ensured at API startup and loaded by
-- docker-compose on first boot (infra/docker-compose.yml mounts both).
--
-- Storage rule (§7.3): the database stores document METADATA and derived text
-- (chunks needed for retrieval); binary contents live only in object storage.
-- `documents.extracted_text` is DERIVED and reproducible from the stored
-- object — kept so re-chunking (TV-019) never needs re-extraction.
--
-- Client isolation (§7.4, TV-012): every document and chunk carries org_id;
-- every search filters on it. Cross-client access is impossible by
-- construction at the query level, not by UI hiding.

CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector (Supabase: preinstalled; compose: pgvector/pgvector image)

CREATE TABLE IF NOT EXISTS documents (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           text        NOT NULL,            -- 'company' for internal knowledge base
    scope            text        NOT NULL DEFAULT 'client'
                     CHECK (scope IN ('client','company')),
    engagement_id    text,                            -- NULL = org-wide
    title            text        NOT NULL,
    storage_key      text        NOT NULL UNIQUE,     -- object key, never contents (§7.3)
    mime_type        text        NOT NULL DEFAULT 'application/octet-stream',
    size_bytes       int         NOT NULL DEFAULT 0,
    checksum_sha256  text        NOT NULL,
    status           text        NOT NULL DEFAULT 'uploaded'
                     CHECK (status IN ('uploaded','extracting','extracted',
                                       'indexing','indexed','failed')),
    extractor        text,                            -- extraction provenance (§9.4)
    extracted_text   text,                            -- derived; see header note
    text_chars       int,
    chunk_count      int         NOT NULL DEFAULT 0,
    embed_provider   text,                            -- ADR-012: recorded per TV-019 re-indexing
    embed_model      text,
    embed_dimensions int,
    last_error       text,
    uploaded_by      text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS documents_org_idx ON documents (org_id, scope, status);

CREATE TABLE IF NOT EXISTS document_chunks (
    id               bigserial PRIMARY KEY,
    document_id      uuid        NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    org_id           text        NOT NULL,            -- denormalised for isolation-filtered search
    seq              int         NOT NULL,
    content          text        NOT NULL,
    char_start       int         NOT NULL,            -- offsets into the extracted text
    char_end         int         NOT NULL,
    embedding        vector,                          -- UNTYPED: dimensions follow the embedder
    embed_provider   text        NOT NULL,
    embed_model      text        NOT NULL,
    embed_dimensions int         NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, seq)
);

CREATE INDEX IF NOT EXISTS chunks_org_idx     ON document_chunks (org_id);
CREATE INDEX IF NOT EXISTS chunks_doc_idx     ON document_chunks (document_id, seq);

-- NOTE ON INDEXING: pgvector HNSW/IVFFlat indexes require a FIXED vector
-- dimension. The shipped schema keeps `embedding` untyped so the dev hash
-- embedder (256) and the production target (BGE-M3, 1024) coexist during the
-- transition. When the production embedder is finalised, a versioned migration
-- pins the dimension and adds:
--     ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(1024);
--     CREATE INDEX chunks_embedding_hnsw ON document_chunks
--         USING hnsw (embedding vector_cosine_ops);
-- At Phase-2 corpus scale (free-tier budget, blueprint §9) sequential cosine
-- scans are fast enough; this is a documented upgrade path, not a debt.

CREATE OR REPLACE FUNCTION documents_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS documents_touch_updated_at_trg ON documents;
CREATE TRIGGER documents_touch_updated_at_trg
    BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION documents_touch_updated_at();
