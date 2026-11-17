"""AI store — prompts, versions and the usage ledger (§8.3, §20).

Same posture as the knowledge store: thin psycopg class, dict_row pool,
cold-start failures translated to QueueUnavailable (clean 503s). Nothing here
knows about providers — adapter output arrives as plain numbers.
"""

import functools
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
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
            raise QueueUnavailable(f"ai store unavailable: {type(exc).__name__}") from exc
    return wrapper


class AIStore:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=2, open=True,
            timeout=10,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

    # -- prompts ---------------------------------------------------------------
    @translate_db_errors
    def create_prompt(self, *, org_id: str, name: str, body: str,
                      description: str = "", created_by: str | None = None) -> dict:
        """Create a prompt with version 1 active. Duplicate names conflict (409
        at the route layer)."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                """INSERT INTO prompts (org_id, name, description) VALUES (%s,%s,%s)
                   RETURNING id, org_id, name, description, created_at""",
                (org_id, name, description),
            ).fetchone()
            conn.execute(
                """INSERT INTO prompt_versions (prompt_id, version, body, is_active, created_by)
                   VALUES (%s, 1, %s, true, %s)""",
                (row["id"], body, created_by),
            )
        return _prompt_dict(row, active_version=1)

    @translate_db_errors
    def list_prompts(self, *, org_id: str, limit: int = 50) -> list[dict]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT p.id, p.org_id, p.name, p.description, p.created_at, p.updated_at,
                          v.version AS active_version, left(v.body, 120) AS body_preview
                   FROM prompts p
                   JOIN prompt_versions v ON v.prompt_id = p.id AND v.is_active
                   WHERE p.org_id=%s
                   ORDER BY p.name LIMIT %s""",
                (org_id, max(1, min(limit, 200))),
            ).fetchall()
        return [_prompt_dict(r) for r in rows]

    @translate_db_errors
    def get_prompt(self, prompt_id: uuid.UUID, *, org_id: str) -> dict | None:
        with self._pool.connection() as conn:
            prompt = conn.execute(
                "SELECT id, org_id, name, description, created_at, updated_at"
                " FROM prompts WHERE id=%s AND org_id=%s",
                (prompt_id, org_id),
            ).fetchone()
            if prompt is None:
                return None
            versions = conn.execute(
                """SELECT version, body, is_active, created_by, created_at
                   FROM prompt_versions WHERE prompt_id=%s ORDER BY version DESC""",
                (prompt_id,),
            ).fetchall()
        return _prompt_dict(prompt, versions=versions)

    @translate_db_errors
    def add_version(self, prompt_id: uuid.UUID, *, org_id: str, body: str,
                    created_by: str | None = None) -> dict:
        """Append a version and make it active atomically — behaviour changes
        are deliberate events, not silent edits."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT id FROM prompts WHERE id=%s AND org_id=%s FOR UPDATE",
                (prompt_id, org_id),
            ).fetchone()
            if row is None:
                return None
            next_version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS v FROM prompt_versions WHERE prompt_id=%s",
                (prompt_id,),
            ).fetchone()["v"]
            conn.execute(
                "UPDATE prompt_versions SET is_active=false WHERE prompt_id=%s AND is_active",
                (prompt_id,),
            )
            conn.execute(
                """INSERT INTO prompt_versions (prompt_id, version, body, is_active, created_by)
                   VALUES (%s,%s,%s,true,%s)""",
                (prompt_id, next_version, body, created_by),
            )
        return {"id": str(prompt_id), "version": next_version, "active": True}

    @translate_db_errors
    def get_active_prompt(self, *, org_id: str, name: str) -> dict | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """SELECT p.name, v.version, v.body
                   FROM prompts p JOIN prompt_versions v
                     ON v.prompt_id = p.id AND v.is_active
                   WHERE p.org_id=%s AND p.name=%s""",
                (org_id, name),
            ).fetchone()
        return dict(row) if row else None

    # -- usage ledger ----------------------------------------------------------
    @translate_db_errors
    def record_usage(self, *, org_id: str, principal: str, purpose: str,
                     provider: str, model: str, input_tokens: int = 0,
                     output_tokens: int = 0, cached_tokens: int = 0,
                     duration_ms: int = 0, estimated_cost_usd: float = 0.0,
                     status: str = "ok", detail: str | None = None) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO ai_usage (org_id, principal, purpose, provider, model,
                         input_tokens, output_tokens, cached_tokens, duration_ms,
                         estimated_cost_usd, status, detail)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (org_id, principal, purpose, provider, model,
                 input_tokens, output_tokens, cached_tokens, duration_ms,
                 round(estimated_cost_usd, 6), status,
                 (detail or "")[:300] or None),
            ).fetchone()
        return row["id"]

    @translate_db_errors
    def usage_summary(self, *, org_id: str, days: int = 30) -> dict:
        since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))
        with self._pool.connection() as conn:
            totals = conn.execute(
                """SELECT count(*) AS calls,
                          COALESCE(sum(input_tokens),0)  AS input_tokens,
                          COALESCE(sum(output_tokens),0) AS output_tokens,
                          COALESCE(sum(cached_tokens),0) AS cached_tokens,
                          COALESCE(sum(estimated_cost_usd),0) AS estimated_cost_usd,
                          COALESCE(sum(duration_ms),0)   AS duration_ms,
                          COALESCE(sum(CASE WHEN status='error' THEN 1 ELSE 0 END),0) AS errors
                   FROM ai_usage WHERE org_id=%s AND created_at >= %s""",
                (org_id, since),
            ).fetchone()
            by_model = conn.execute(
                """SELECT provider, model, count(*) AS calls,
                          COALESCE(sum(input_tokens),0) AS input_tokens,
                          COALESCE(sum(output_tokens),0) AS output_tokens,
                          COALESCE(sum(estimated_cost_usd),0) AS estimated_cost_usd
                   FROM ai_usage WHERE org_id=%s AND created_at >= %s
                   GROUP BY provider, model ORDER BY estimated_cost_usd DESC""",
                (org_id, since),
            ).fetchall()
        return {
            "org_id": org_id,
            "days": days,
            "since": since.isoformat(timespec="seconds"),
            "totals": {
                "calls": totals["calls"],
                "input_tokens": totals["input_tokens"],
                "output_tokens": totals["output_tokens"],
                "cached_tokens": totals["cached_tokens"],
                "estimated_cost_usd": float(totals["estimated_cost_usd"]),
                "duration_ms": totals["duration_ms"],
                "errors": totals["errors"],
            },
            "by_model": [
                {"provider": r["provider"], "model": r["model"], "calls": r["calls"],
                 "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                 "estimated_cost_usd": float(r["estimated_cost_usd"])}
                for r in by_model
            ],
        }

    def close(self) -> None:
        self._pool.close()


def _prompt_dict(row, active_version: int | None = None, versions=None) -> dict:
    out = dict(row)
    if "id" in out and isinstance(out["id"], uuid.UUID):
        out["id"] = str(out["id"])
    for key in ("created_at", "updated_at"):
        if key in out and isinstance(out[key], datetime):
            out[key] = out[key].astimezone(timezone.utc).isoformat(timespec="seconds")
    if active_version is not None:
        out.setdefault("active_version", active_version)
    if versions is not None:
        out["versions"] = [
            {**dict(v), "created_at": v["created_at"].astimezone(timezone.utc).isoformat(timespec="seconds")}
            for v in versions
        ]
    return out
