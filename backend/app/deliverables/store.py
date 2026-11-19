"""Deliverables store — Postgres data access for generated artefacts.

Same architecture posture as the knowledge store (ADR-009): a thin class over
psycopg, connection-pooled, DB failures translated to `QueueUnavailable` so
the API surfaces clean 503s. Isolation (§7.4, TV-012): org_id is part of
every table and every read — a read that passes an org filter cannot see
another org's deliverables, and ids are never enough to cross the boundary.
"""

from __future__ import annotations

import functools
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from app.core.config import Settings, get_settings
from app.jobs.queue import QueueUnavailable, _DB_ERRORS

_TRANSLATABLE = _DB_ERRORS + (psycopg.errors.UndefinedTable,)


def translate_db_errors(fn):
    """Same contract as the queue/knowledge decorator."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except _TRANSLATABLE as exc:
            raise QueueUnavailable(f"deliverables store unavailable: {type(exc).__name__}") from exc
    return wrapper


# Columns a caller may update through update() — never the id/org columns.
_MUTABLE = ("status", "storage_key", "mime_type", "size_bytes", "checksum_sha256",
            "last_error", "updated_at")


class DeliverableStore:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=2, open=True,
            timeout=10,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

    def close(self) -> None:
        self._pool.close()

    # -- writes ----------------------------------------------------------------
    @translate_db_errors
    def create(self, *, org_id: str, scope: str, engagement_id: str | None,
               assignment_id: uuid.UUID | None, title: str, kind: str,
               spec: dict, created_by: str) -> dict:
        with self._pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO deliverables
                     (org_id, scope, engagement_id, assignment_id, title, kind, spec, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (org_id, scope, engagement_id, assignment_id, title, kind,
                 Json(spec), created_by),
            ).fetchone()
        assert row is not None
        self.append_event(row["id"], "deliverable.created", actor=created_by,
                          detail=f"{kind} · {len(spec.get('sections', []))} sections, "
                                 f"{len(spec.get('tables', []))} tables")
        return row

    @translate_db_errors
    def update(self, did: uuid.UUID, **fields) -> dict | None:
        cols = {k: v for k, v in fields.items() if k in _MUTABLE and v is not None or k == "last_error"}
        if not cols:
            return self.get(did, org_id=None)
        cols["updated_at"] = "now()"  # sentinel handled below
        sets, params = [], []
        for k, v in cols.items():
            if k == "updated_at":
                sets.append("updated_at = now()")
            else:
                sets.append(f"{k} = %s")
                params.append(v)
        with self._pool.connection() as conn:
            row = conn.execute(
                f"UPDATE deliverables SET {', '.join(sets)} WHERE id = %s RETURNING *",
                (*params, did),
            ).fetchone()
        return row

    @translate_db_errors
    def append_event(self, did: uuid.UUID, event: str, *, actor: str = "system",
                     detail: str | None = None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO deliverable_events (deliverable_id, event, actor, detail)
                   VALUES (%s,%s,%s,%s)""",
                (did, event, actor, detail),
            )

    # -- QA governance (Phase 7, CAIOS-QA-001) ---------------------------------
    @translate_db_errors
    def set_qa(self, did: uuid.UUID, *, qa_state: str, reviewer: str,
               comment: str | None = None, checks: dict | None = None) -> dict | None:
        """One transaction per QA decision — reviewer, comment and checklist land
        atomically with the state change (a decision without its provenance is
        not a decision), and the current version's record carries the same
        qa_state so "which artefact version is approved" stays queryable."""
        with self._pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """UPDATE deliverables SET
                         qa_state = %s, qa_reviewer = %s, qa_comment = %s,
                         qa_checks = %s, qa_decided_at = now(), updated_at = now()
                       WHERE id = %s RETURNING *""",
                    (qa_state, reviewer, comment,
                     Json(checks) if checks else None, did),
                ).fetchone()
                if row is not None:
                    conn.execute(
                        """UPDATE deliverable_versions SET qa_state = %s, qa_checks = %s
                           WHERE deliverable_id = %s AND version = %s""",
                        (qa_state, Json(checks) if checks else None,
                         did, row["spec_version"]),
                    )
        return row

    @translate_db_errors
    def revise(self, did: uuid.UUID, *, new_spec: dict, comment: str | None) -> dict | None:
        """New spec version: bumps spec_version, queues regeneration and resets
        the QA gate — a revised artefact is a new artefact and must earn
        approval again. Old versions stay recorded in deliverable_versions."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """UPDATE deliverables SET
                     spec = %s, spec_version = spec_version + 1, status = 'queued',
                     qa_state = 'draft', qa_reviewer = NULL, qa_comment = NULL,
                     qa_checks = NULL, qa_decided_at = NULL, last_error = NULL,
                     updated_at = now()
                   WHERE id = %s RETURNING *""",
                (Json(new_spec), did),
            ).fetchone()
        assert row is not None
        self.append_event(did, "deliverable.revised", actor="reviewer",
                          detail=f"v{row['spec_version']} queued"
                                 + (f" · {comment}" if comment else ""))
        return row

    @translate_db_errors
    def record_version(self, did: uuid.UUID, *, version: int, spec: dict,
                       storage_key: str, mime_type: str, size_bytes: int,
                       checksum_sha256: str, qa_state: str,
                       qa_checks: dict | None) -> None:
        """Immutable per-version artefact record (upsert: a regenerate of the
        same version overwrites that version's row — its QA trail lives in
        deliverable_events)."""
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO deliverable_versions
                     (deliverable_id, version, spec, storage_key, mime_type,
                      size_bytes, checksum_sha256, qa_state, qa_checks)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (deliverable_id, version) DO UPDATE SET
                     spec = EXCLUDED.spec, storage_key = EXCLUDED.storage_key,
                     mime_type = EXCLUDED.mime_type, size_bytes = EXCLUDED.size_bytes,
                     checksum_sha256 = EXCLUDED.checksum_sha256,
                     qa_state = EXCLUDED.qa_state, qa_checks = EXCLUDED.qa_checks,
                     generated_at = now()""",
                (did, version, Json(spec), storage_key, mime_type, size_bytes,
                 checksum_sha256, qa_state, Json(qa_checks) if qa_checks else None),
            )

    @translate_db_errors
    def get_versions(self, did: uuid.UUID) -> list[dict]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT version, spec, storage_key, mime_type, size_bytes,
                          checksum_sha256, qa_state, qa_checks, generated_at
                   FROM deliverable_versions WHERE deliverable_id = %s
                   ORDER BY version DESC""",
                (did,),
            ).fetchall()
        return list(rows)

    # -- reads -----------------------------------------------------------------
    @translate_db_errors
    def get(self, did: uuid.UUID, org_id: str | None) -> dict | None:
        """org_id=None is for internal worker use (jobs already carry org); every
        API path passes the org filter — enforced by the routes, never the UI."""
        with self._pool.connection() as conn:
            if org_id is None:
                row = conn.execute(
                    "SELECT * FROM deliverables WHERE id = %s", (did,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM deliverables WHERE id = %s AND org_id = %s",
                    (did, org_id)).fetchone()
        return row

    @translate_db_errors
    def status_counts(self, org_id: str) -> dict[str, int]:
        """Deliverable build-status counts for the ops overview (CAIOS-OPS-001)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT status, count(*) AS n FROM deliverables WHERE org_id = %s "
                "GROUP BY status", (org_id,)).fetchall()
        return {r["status"]: r["n"] for r in rows}

    @translate_db_errors
    def qa_counts(self, org_id: str) -> dict[str, int]:
        """QA-gate counts for the ops overview — how much work is waiting at
        the approval gate, per CAIOS-QA-001."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT qa_state, count(*) AS n FROM deliverables WHERE org_id = %s "
                "GROUP BY qa_state", (org_id,)).fetchall()
        return {r["qa_state"]: r["n"] for r in rows}

    @translate_db_errors
    def list(self, org_id: str, limit: int = 50) -> list[dict]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT id, org_id, scope, engagement_id, assignment_id, title, kind,
                          status, mime_type, size_bytes, checksum_sha256, last_error,
                          spec_version, qa_state, qa_reviewer, qa_decided_at,
                          created_by, created_at, updated_at
                   FROM deliverables WHERE org_id = %s
                   ORDER BY created_at DESC LIMIT %s""",
                (org_id, max(1, min(limit, 100))),
            ).fetchall()
        return list(rows)

    @translate_db_errors
    def events(self, did: uuid.UUID) -> list[dict]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT event, actor, detail, created_at
                   FROM deliverable_events WHERE deliverable_id = %s ORDER BY id""",
                (did,),
            ).fetchall()
        return list(rows)
