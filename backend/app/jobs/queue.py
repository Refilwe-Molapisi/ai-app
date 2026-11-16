"""Job queue — Postgres-backed, provider-independent (ADR-009, §14.2).

Interface contract (the only thing the rest of the app knows about):

    enqueue(kind, payload, priority=, run_at=) -> job_id
    claim_batch(worker_id, limit)              -> claimed jobs (status=running)
    complete(job_id, result=) / fail(...) / cancel(job_id) / get(job_id) / counts()

Durability: jobs live in Postgres, so any process (Render cold start, cron
trigger, local worker, future VPS worker) can claim and execute them — nothing
is ever lost to a sleep. Claiming uses SELECT … FOR UPDATE SKIP LOCKED so
concurrent triggers cannot double-execute a job (TV-016).
"""

import json
import functools
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from psycopg_pool import PoolTimeout

from app.core.config import Settings, get_settings


class QueueUnavailable(RuntimeError):
    """Database/queue unreachable (cold start, network). Callers translate this
    to a clean 503 — jobs stay pending and the next trigger drains them."""


_DB_ERRORS = (PoolTimeout, psycopg.OperationalError, psycopg.errors.ConnectionFailure)


def translate_db_errors(fn):
    """Decorator: convert raw connection failures into QueueUnavailable."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except _DB_ERRORS as exc:
            raise QueueUnavailable(f"database unreachable: {type(exc).__name__}") from exc
    return wrapper


@dataclass
class Job:
    id: uuid.UUID
    kind: str
    payload: dict
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None = None
    result: dict | None = None

    @classmethod
    def from_row(cls, row) -> "Job":
        return cls(
            id=row["id"], kind=row["kind"], payload=row["payload"],
            status=row["status"], attempts=row["attempts"],
            max_attempts=row["max_attempts"], last_error=row["last_error"],
            result=row["result"],
        )


class PostgresJobQueue:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=4, open=True,
            timeout=10,  # fail fast on cold starts; callers surface 503, never hang
            # dict_row: handlers and endpoints read rows by column name
            # (Job.from_row, counts(), …). Latent tuple-rows bug fixed in Phase 2.
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

    # -- write side -----------------------------------------------------------
    @translate_db_errors
    def enqueue(self, kind: str, payload: dict | None = None, *,
                priority: int = 5, run_at: datetime | None = None,
                max_attempts: int | None = None) -> uuid.UUID:
        with self._pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO jobs (kind, payload, priority, available_at, max_attempts)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (kind, Jsonb(payload or {}), priority,
                 run_at or datetime.now(timezone.utc),
                 max_attempts or self._settings.job_max_attempts),
            ).fetchone()
        return row["id"]

    @translate_db_errors
    def cancel(self, job_id: uuid.UUID) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='cancelled', finished_at=now() "
                "WHERE id=%s AND status='pending' RETURNING id", (job_id,))
        return cur.fetchone() is not None

    # -- claim / execute ------------------------------------------------------
    @translate_db_errors
    def claim_batch(self, worker_id: str, limit: int) -> list[Job]:
        """Atomically claim up to `limit` runnable jobs (SKIP LOCKED)."""
        with self._pool.connection() as conn:
            with conn.transaction():
                rows = conn.execute(
                    """SELECT id FROM jobs
                       WHERE status='pending' AND available_at <= now()
                       ORDER BY priority, available_at
                       LIMIT %s
                       FOR UPDATE SKIP LOCKED""",
                    (limit,),
                ).fetchall()
                if not rows:
                    return []
                ids = [r["id"] for r in rows]
                out = conn.execute(
                    """UPDATE jobs
                       SET status='running', locked_by=%s, locked_at=now(),
                           started_at=now(), attempts=attempts+1
                       WHERE id = ANY(%s)
                       RETURNING id, kind, payload, status, attempts,
                                 max_attempts, last_error, result""",
                    (worker_id, ids),
                ).fetchall()
        return [Job.from_row(r) for r in out]

    @translate_db_errors
    def complete(self, job_id: uuid.UUID, result: dict | None = None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """UPDATE jobs SET status='done', result=%s, finished_at=now(),
                          duration_ms=(EXTRACT(EPOCH FROM (now()-started_at))*1000)::int
                   WHERE id=%s""",
                (Jsonb(result or {}), job_id),
            )

    @translate_db_errors
    def fail(self, job_id: uuid.UUID, error: str, *, backoff_seconds: int = 60) -> None:
        """Retry with backoff until max_attempts, then dead-letter (TV-016)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE id=%s", (job_id,)
            ).fetchone()
            if row is None:
                return
            if row["attempts"] >= row["max_attempts"]:
                conn.execute(
                    "UPDATE jobs SET status='dead', last_error=%s, finished_at=now(),"
                    " duration_ms=(EXTRACT(EPOCH FROM (now()-started_at))*1000)::int"
                    " WHERE id=%s", (error[:2000], job_id))
            else:
                conn.execute(
                    """UPDATE jobs SET status='pending', last_error=%s,
                              available_at = now() + %s * interval '1 second',
                              locked_by=NULL, locked_at=NULL
                       WHERE id=%s""",
                    (error[:2000], backoff_seconds * row["attempts"], job_id),
                )

    # -- read side ------------------------------------------------------------
    @translate_db_errors
    def get(self, job_id: uuid.UUID) -> Job | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id, kind, payload, status, attempts, max_attempts,"
                " last_error, result FROM jobs WHERE id=%s", (job_id,)
            ).fetchone()
        return Job.from_row(row) if row else None

    @translate_db_errors
    def counts(self) -> dict[str, int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT status, count(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    @translate_db_errors
    def ensure_schema(self, schema_sql: str) -> None:
        """Idempotent bootstrap (dev convenience; migrations are versioned in
        production per CAIOS-MASTER-001 §6.4)."""
        with self._pool.connection() as conn:
            conn.execute(schema_sql)

    def close(self) -> None:
        self._pool.close()


def json_dumps(obj) -> str:
    return json.dumps(obj, default=str)
