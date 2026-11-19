-- CAIOS deliverables (Phase 6 — CAIOS-DEL-001, master spec §30 phase 6).
-- Generated client artefacts: reports (.docx/.pdf), workbooks (.xlsx), briefs (.md).
-- The database stores keys/metadata only (§7.3): bytes live in object storage
-- under governance-recognised prefixes, never in the table.
-- Isolation (§7.4, TV-012): org_id on every row and every read.

CREATE TABLE IF NOT EXISTS deliverables (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           text        NOT NULL,
    scope            text        NOT NULL DEFAULT 'company'
                     CHECK (scope IN ('client','company')),
    engagement_id    text,                            -- client scope only
    assignment_id    uuid,       -- traceability to the governing workflow run
                     -- (plain uuid, no FK: modules bootstrap independently)
    title            text        NOT NULL,
    kind             text        NOT NULL CHECK (kind IN ('docx','xlsx','pdf','md')),
    status           text        NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','generating','ready','failed')),
    spec             jsonb       NOT NULL,            -- structured content (spec v1)
    spec_version     int         NOT NULL DEFAULT 1,
    storage_key      text,                            -- set when ready
    mime_type        text,
    size_bytes       int,
    checksum_sha256  text,
    last_error       text,                            -- honest failure reason
    created_by       text        NOT NULL DEFAULT 'ui',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS deliverables_org_idx ON deliverables (org_id, created_at DESC);

-- Append-only governance trail (§13) — same posture as assignment_events:
-- every transition is recorded, never updated or deleted.
CREATE TABLE IF NOT EXISTS deliverable_events (
    id               bigserial PRIMARY KEY,
    deliverable_id   uuid        NOT NULL REFERENCES deliverables(id) ON DELETE CASCADE,
    event            text        NOT NULL,            -- created|generating|ready|failed
    actor            text        NOT NULL DEFAULT 'system',
    detail           text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS deliverable_events_del_idx ON deliverable_events (deliverable_id, id);

-- ── Phase 7 (CAIOS-QA-001): QA governance ────────────────────────────────────
-- A deliverable is releasable only when qa_state = 'approved' (§13 gate).
-- Revisions create a new spec_version; every generated version is recorded
-- immutably in deliverable_versions (spec + artefact metadata per version).
-- ALTERs are idempotent: the file is ensured on every boot.

ALTER TABLE deliverables ADD COLUMN IF NOT EXISTS qa_state       text NOT NULL DEFAULT 'draft';
ALTER TABLE deliverables ADD COLUMN IF NOT EXISTS qa_reviewer    text;
ALTER TABLE deliverables ADD COLUMN IF NOT EXISTS qa_comment     text;
ALTER TABLE deliverables ADD COLUMN IF NOT EXISTS qa_checks      jsonb;
ALTER TABLE deliverables ADD COLUMN IF NOT EXISTS qa_decided_at  timestamptz;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'deliverables_qa_state_check') THEN
        ALTER TABLE deliverables ADD CONSTRAINT deliverables_qa_state_check
            CHECK (qa_state IN ('draft','in_review','approved','changes_requested'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS deliverable_versions (
    id               bigserial PRIMARY KEY,
    deliverable_id   uuid        NOT NULL REFERENCES deliverables(id) ON DELETE CASCADE,
    version          int         NOT NULL,             -- == spec_version of that build
    spec             jsonb       NOT NULL,             -- exact spec that produced the bytes
    storage_key      text,                            -- versioned key: {id}.v{n}.{ext}
    mime_type        text,
    size_bytes       int,
    checksum_sha256  text,
    qa_state         text        NOT NULL DEFAULT 'draft',
    qa_checks        jsonb,
    generated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (deliverable_id, version)
);

CREATE INDEX IF NOT EXISTS deliverable_versions_del_idx
    ON deliverable_versions (deliverable_id, version DESC);
