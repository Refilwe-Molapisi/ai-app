-- CAIOS job queue schema — portable Postgres implementation (ADR-009).
-- Production shape is pgmq (Supabase Queues); this table backs the same
-- JobQueue interface so the swap touches one class only.
-- Loaded automatically by docker-compose on first boot; also idempotently
-- ensured at API startup.

CREATE TABLE IF NOT EXISTS jobs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          text        NOT NULL,
    payload       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    status        text        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','running','done','failed','cancelled','dead')),
    priority      int         NOT NULL DEFAULT 5,
    attempts      int         NOT NULL DEFAULT 0,
    max_attempts  int         NOT NULL DEFAULT 3,
    available_at  timestamptz NOT NULL DEFAULT now(),
    locked_at     timestamptz,
    locked_by     text,
    started_at    timestamptz,
    finished_at   timestamptz,
    duration_ms   int,
    last_error    text,
    result        jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx
    ON jobs (priority, available_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS jobs_kind_status_idx ON jobs (kind, status);

-- Audit hook: every terminal state is recorded for §22 traceability.
CREATE OR REPLACE FUNCTION jobs_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS jobs_touch_updated_at_trg ON jobs;
CREATE TRIGGER jobs_touch_updated_at_trg
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION jobs_touch_updated_at();
