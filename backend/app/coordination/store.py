"""Coordination store — assignments, steps, events (§11, §13).

Same posture as the other stores: thin psycopg class, dict_row pool,
autocommit, cold-start failures translated to QueueUnavailable. Multi-row
transitions (create, approve-advance) run inside explicit transactions so a
step and its event are never half-written.
"""

import functools
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from app.core.config import Settings, get_settings
from app.jobs.queue import QueueUnavailable, _DB_ERRORS

_TRANSLATABLE = _DB_ERRORS + (psycopg.errors.UndefinedTable,)


def translate_db_errors(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except _TRANSLATABLE as exc:
            raise QueueUnavailable(f"coordination store unavailable: {type(exc).__name__}") from exc
    return wrapper


class CoordinationStore:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=2, open=True,
            timeout=10,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

    # -- creation --------------------------------------------------------------
    @translate_db_errors
    def create_assignment(self, *, org_id: str, workflow: str, title: str | None,
                          engagement_id: str | None, inputs: dict,
                          created_by: str | None, steps: list[dict]) -> dict:
        """Insert the assignment, its concrete steps and the 'created' event in
        ONE transaction. `steps` are finalised specs: [{seq, name, kind, params,
        is_gate}]."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                """INSERT INTO assignments (org_id, workflow, title, engagement_id,
                                            inputs, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   RETURNING id, org_id, workflow, title, engagement_id, status,
                             inputs, created_by, created_at""",
                (org_id, workflow, title, engagement_id, Jsonb(inputs), created_by),
            ).fetchone()
            for s in steps:
                conn.execute(
                    """INSERT INTO assignment_steps
                           (assignment_id, seq, name, kind, params, is_gate)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (row["id"], s["seq"], s["name"], s["kind"],
                     Jsonb(s.get("params") or {}), s.get("is_gate", False)),
                )
            conn.execute(
                """INSERT INTO assignment_events (assignment_id, event, actor, detail)
                   VALUES (%s,'assignment.created',%s,%s)""",
                (row["id"], created_by or "system",
                 Jsonb({"workflow": workflow, "steps": [s["name"] for s in steps]})),
            )
        return _assignment_dict(row)

    # -- reads -----------------------------------------------------------------
    @translate_db_errors
    def get_assignment(self, assignment_id: uuid.UUID, *,
                       org_id: str | None = None) -> dict | None:
        """org_id given ⇒ org-scoped read (routes); None ⇒ internal loader for
        queue handlers, which know only the assignment id."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """SELECT id, org_id, workflow, title, engagement_id, status, inputs,
                          created_by, created_at, updated_at, completed_at
                   FROM assignments WHERE id=%s AND (%s::text IS NULL OR org_id=%s)""",
                (assignment_id, org_id, org_id),
            ).fetchone()
            if row is None:
                return None
            steps = conn.execute(
                """SELECT id, seq, name, kind, params, is_gate, status, output,
                          job_id, last_error, started_at, finished_at
                   FROM assignment_steps WHERE assignment_id=%s ORDER BY seq""",
                (assignment_id,),
            ).fetchall()
            events = conn.execute(
                """SELECT id, event, actor, detail, created_at
                   FROM assignment_events WHERE assignment_id=%s ORDER BY id""",
                (assignment_id,),
            ).fetchall()
        out = _assignment_dict(row, steps=steps, events=events)
        out["steps"] = [_step_dict(s) for s in steps]
        out["events"] = [_event_dict(e) for e in events]
        return out

    @translate_db_errors
    def list_assignments(self, *, org_id: str, status: str | None = None,
                         limit: int = 50) -> list[dict]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT a.id, a.org_id, a.workflow, a.title, a.engagement_id,
                          a.status, a.created_by, a.created_at, a.completed_at,
                          (SELECT count(*) FROM assignment_steps s
                            WHERE s.assignment_id = a.id) AS step_count,
                          (SELECT count(*) FROM assignment_steps s
                            WHERE s.assignment_id = a.id AND s.status='done') AS steps_done
                   FROM assignments a
                   WHERE a.org_id=%s AND (%s::text IS NULL OR a.status=%s)
                   ORDER BY a.created_at DESC LIMIT %s""",
                (org_id, status, status, max(1, min(limit, 200))),
            ).fetchall()
        return [_assignment_dict(r) for r in rows]

    @translate_db_errors
    def status_counts(self, org_id: str) -> dict:
        """Counts for the ops overview (CAIOS-OPS-001): assignments per status,
        and open work (running/awaiting_approval) per workflow so the ops desk
        can allocate workload to divisions via ops/workflows_map.py."""
        with self._pool.connection() as conn:
            by_status = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) AS n FROM assignments WHERE org_id = %s "
                "GROUP BY status", (org_id,)).fetchall()}
            open_by_workflow = {r["workflow"]: r["n"] for r in conn.execute(
                "SELECT workflow, count(*) AS n FROM assignments WHERE org_id = %s "
                "AND status IN ('running','awaiting_approval') GROUP BY workflow",
                (org_id,)).fetchall()}
        return {"by_status": by_status, "open_by_workflow": open_by_workflow}

    # -- step transitions ------------------------------------------------------
    @translate_db_errors
    def next_actionable_step(self, assignment_id: uuid.UUID) -> dict | None:
        """First step that still needs something: pending (never enqueued) or
        queued/awaiting_approval. None ⇒ nothing left to advance."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """SELECT id, seq, name, kind, params, is_gate, status
                   FROM assignment_steps
                   WHERE assignment_id=%s AND status IN
                         ('pending','queued','awaiting_approval')
                   ORDER BY seq LIMIT 1""",
                (assignment_id,),
            ).fetchone()
        return _step_dict(row) if row else None

    @translate_db_errors
    def get_step(self, assignment_id: uuid.UUID, seq: int) -> dict | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """SELECT id, seq, name, kind, params, is_gate, status, output,
                          job_id, last_error, started_at, finished_at
                   FROM assignment_steps WHERE assignment_id=%s AND seq=%s""",
                (assignment_id, seq),
            ).fetchone()
        return _step_dict(row) if row else None

    @translate_db_errors
    def update_step(self, assignment_id: uuid.UUID, seq: int, *, status: str,
                    output: dict | None = None, job_id: uuid.UUID | None = None,
                    last_error: str | None = None,
                    mark_started: bool = False, mark_finished: bool = False) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """UPDATE assignment_steps SET
                       status=%s,
                       output=COALESCE(%s, output),
                       job_id=COALESCE(%s, job_id),
                       last_error=%s,
                       started_at=CASE WHEN %s THEN now() ELSE started_at END,
                       finished_at=CASE WHEN %s THEN now() ELSE finished_at END
                   WHERE assignment_id=%s AND seq=%s""",
                (status,
                 Jsonb(output) if output is not None else None,
                 job_id, last_error,
                 mark_started, mark_finished, assignment_id, seq),
            )

    # -- assignment transitions ------------------------------------------------
    @translate_db_errors
    def set_status(self, assignment_id: uuid.UUID, *, status: str,
                   completed: bool = False) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """UPDATE assignments SET status=%s,
                          completed_at=CASE WHEN %s THEN now() ELSE completed_at END
                   WHERE id=%s""",
                (status, completed, assignment_id),
            )

    @translate_db_errors
    def append_event(self, assignment_id: uuid.UUID, event: str, *,
                     actor: str = "system", detail: dict | None = None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO assignment_events (assignment_id, event, actor, detail)
                   VALUES (%s,%s,%s,%s)""",
                (assignment_id, event, actor, Jsonb(detail or {})),
            )

    @translate_db_errors
    def cancel_open_steps(self, assignment_id: uuid.UUID) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """UPDATE assignment_steps SET status='cancelled', finished_at=now()
                   WHERE assignment_id=%s
                     AND status IN ('pending','queued','awaiting_approval')""",
                (assignment_id,),
            )

    def close(self) -> None:
        self._pool.close()


# ── serialisation helpers ────────────────────────────────────────────────────

def _iso(dt) -> str | None:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    return dt


def _assignment_dict(row, steps=None, events=None) -> dict:
    out = dict(row)
    out["id"] = str(out["id"])
    out["created_at"] = _iso(out.get("created_at"))
    out["updated_at"] = _iso(out.get("updated_at"))
    out["completed_at"] = _iso(out.get("completed_at"))
    if steps is not None:
        out["steps"] = [_step_dict(s) for s in steps]
    if events is not None:
        out["events"] = [_event_dict(e) for e in events]
    return out


def _step_dict(row) -> dict:
    out = dict(row)
    if out.get("id") is not None:
        out["id"] = str(out["id"])
    out["job_id"] = str(out["job_id"]) if out.get("job_id") else None
    for key in ("started_at", "finished_at"):
        out[key] = _iso(out.get(key))
    return out


def _event_dict(row) -> dict:
    out = dict(row)
    out["id"] = str(out["id"])
    out["created_at"] = _iso(out.get("created_at"))
    return out
