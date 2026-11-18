-- CAIOS AI integration schema (Phase 3 — CAIOS-MASTER-001 §8, §20).
-- Idempotent; ensured at API startup next to the jobs and knowledge schemas.
--
-- prompts / prompt_versions: versioned prompt & instruction templates (§8.3).
--   Templates are per-org (org_id='company' for internal defaults), append-only
--   in versions, with exactly ONE active version per prompt — re-indexing-style
--   controlled change for behaviour, never silent edits.
--   Only the {{context}} placeholder is legal (validated at the API layer);
--   the question travels as the user message.
--
-- ai_usage: the AI Usage Management ledger (§20). Every model call — success
--   or failure — writes one row: provider, model, token counts, estimated cost
--   (USD, peak pricing until the off-peak scheduler exists), duration, status.
--   No prompt bodies or answers are stored here — answers belong to the
--   caller; the ledger is about COST and GOVERNANCE, not content.

CREATE TABLE IF NOT EXISTS prompts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      text        NOT NULL,
    name        text        NOT NULL,
    description text        NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id         bigserial PRIMARY KEY,
    prompt_id  uuid        NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    version    int         NOT NULL,
    body       text        NOT NULL,
    is_active  boolean     NOT NULL DEFAULT false,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (prompt_id, version)
);

CREATE INDEX IF NOT EXISTS prompt_versions_active_idx
    ON prompt_versions (prompt_id) WHERE is_active;

CREATE TABLE IF NOT EXISTS ai_usage (
    id                 bigserial PRIMARY KEY,
    org_id             text        NOT NULL,
    principal          text        NOT NULL DEFAULT '',
    purpose            text        NOT NULL DEFAULT 'answer'
                       CHECK (purpose IN ('answer','agent','generate','extract','maintenance')),
    provider           text        NOT NULL,
    model              text        NOT NULL,
    input_tokens       int         NOT NULL DEFAULT 0,
    output_tokens      int         NOT NULL DEFAULT 0,
    cached_tokens      int         NOT NULL DEFAULT 0,
    duration_ms        int         NOT NULL DEFAULT 0,
    estimated_cost_usd numeric(12,6) NOT NULL DEFAULT 0,
    status             text        NOT NULL DEFAULT 'ok'
                       CHECK (status IN ('ok','error')),
    detail             text,       -- short note: question snippet / error class
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ai_usage_org_idx      ON ai_usage (org_id, created_at);
CREATE INDEX IF NOT EXISTS ai_usage_model_idx    ON ai_usage (provider, model);
CREATE INDEX IF NOT EXISTS ai_usage_status_idx   ON ai_usage (status);

CREATE OR REPLACE FUNCTION prompts_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS prompts_touch_updated_at_trg ON prompts;
CREATE TRIGGER prompts_touch_updated_at_trg
    BEFORE UPDATE ON prompts
    FOR EACH ROW EXECUTE FUNCTION prompts_touch_updated_at();

-- Phase 5 upgrade path: purpose gains 'agent' (§11 specialist agent runs).
-- CREATE TABLE IF NOT EXISTS does not touch existing tables, so recreate the
-- CHECK constraint idempotently — older deployments upgrade in place.
ALTER TABLE ai_usage DROP CONSTRAINT IF EXISTS ai_usage_purpose_check;
ALTER TABLE ai_usage ADD CONSTRAINT ai_usage_purpose_check
    CHECK (purpose IN ('answer','agent','generate','extract','maintenance'));
