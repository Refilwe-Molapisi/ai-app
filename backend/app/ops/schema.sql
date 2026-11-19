-- CAIOS internal operations (Phase 8 — CAIOS-OPS-001, master spec §30 phase 8).
-- The human roster behind the §11 division agents, plus the ops event trail.
-- Divisions mirror the agent registry (agents/definitions.py): the three
-- §11 delivery divisions plus the Governance & Operations desk that owns the
-- non-agent workflows (knowledge briefs, onboarding checks, gates).
-- Isolation (§7.4, TV-012): org_id on every row and every read.

CREATE TABLE IF NOT EXISTS team_members (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      text        NOT NULL,
    name        text        NOT NULL,
    email       text,
    division    text        NOT NULL
                CHECK (division IN ('BI & Analytics','Commercial Intelligence',
                                    'Risk & Controls','Governance & Operations')),
    role        text,
    active      boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

CREATE INDEX IF NOT EXISTS team_members_org_idx ON team_members (org_id, division);

-- Append-only ops trail (§13) — roster changes are governance-relevant:
-- who joined/left which division, who changed whose role.
CREATE TABLE IF NOT EXISTS ops_events (
    id          bigserial   PRIMARY KEY,
    member_id   uuid        REFERENCES team_members(id) ON DELETE CASCADE,
    org_id      text        NOT NULL,
    event       text        NOT NULL,   -- member.added | member.updated
    actor       text        NOT NULL DEFAULT 'api',
    detail      text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ops_events_org_idx ON ops_events (org_id, id);
