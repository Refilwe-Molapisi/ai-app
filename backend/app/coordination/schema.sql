-- CAIOS central coordination schema (Phase 4 — CAIOS-MASTER-001 §11, §12, §13).
-- Idempotent; ensured at API startup after the jobs/knowledge/ai schemas.
--
-- assignments: one unit of coordinated work for one org (§11). Status model:
--   running → awaiting_approval ⇄ running → completed
--   running/awaiting_approval → cancelled (human) | failed (step error)
--
-- assignment_steps: ordered, typed steps materialised from the workflow
--   registry at creation time — params are CONCRETE (input templating is done
--   once, at creation, so a bad placeholder is a 422, never a runtime surprise).
--   Automatic steps execute as 'assignment.step' queue jobs; gate steps wait
--   for POST /assignments/{id}/approve (human in the loop, §12).
--
-- assignment_events: append-only governance trail (§13). Every transition —
--   creation, enqueue, start, completion, approval, failure, cancellation —
--   is one row. No deletes: the trail is the audit.

CREATE TABLE IF NOT EXISTS assignments (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        text        NOT NULL,
    workflow      text        NOT NULL,
    title         text,
    engagement_id text,
    status        text        NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running','awaiting_approval','completed','cancelled','failed')),
    inputs        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_by    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz
);

CREATE INDEX IF NOT EXISTS assignments_org_idx  ON assignments (org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS assignments_stat_idx ON assignments (org_id, status);

CREATE TABLE IF NOT EXISTS assignment_steps (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assignment_id uuid        NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    seq           int         NOT NULL,
    name          text        NOT NULL,
    kind          text        NOT NULL,
    params        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    is_gate       boolean     NOT NULL DEFAULT false,
    status        text        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','queued','running','awaiting_approval',
                                    'done','failed','skipped','cancelled')),
    output        jsonb,
    job_id        uuid,          -- queue job executing this step (informational)
    last_error    text,
    started_at    timestamptz,
    finished_at   timestamptz,
    UNIQUE (assignment_id, seq)
);

CREATE INDEX IF NOT EXISTS assignment_steps_assn_idx ON assignment_steps (assignment_id, seq);

CREATE TABLE IF NOT EXISTS assignment_events (
    id            bigserial PRIMARY KEY,
    assignment_id uuid        NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    event         text        NOT NULL,
    actor         text        NOT NULL DEFAULT 'system',
    detail        jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS assignment_events_assn_idx ON assignment_events (assignment_id, id);

CREATE OR REPLACE FUNCTION assignments_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS assignments_touch_updated_at_trg ON assignments;
CREATE TRIGGER assignments_touch_updated_at_trg
    BEFORE UPDATE ON assignments
    FOR EACH ROW EXECUTE FUNCTION assignments_touch_updated_at();
