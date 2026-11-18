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
