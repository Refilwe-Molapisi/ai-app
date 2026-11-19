"""Internal-ops store — Postgres data access for the team roster (CAIOS-OPS-001).

Same posture as the other stores (ADR-009): thin class over psycopg,
connection-pooled, DB failures translated to `QueueUnavailable` so the API
surfaces clean 503s. Isolation (§7.4, TV-012): org_id on every row and every
read; ids alone never cross the boundary.
"""

from __future__ import annotations

import functools
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json  # noqa: F401 — kept for parity with other stores
from psycopg_pool import ConnectionPool

from app.core.config import Settings, get_settings
from app.jobs.queue import QueueUnavailable, _DB_ERRORS

_TRANSLATABLE = _DB_ERRORS + (psycopg.errors.UndefinedTable,)


def translate_db_errors(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except _TRANSLATABLE as exc:
            raise QueueUnavailable(f"ops store unavailable: {type(exc).__name__}") from exc
    return wrapper


class OpsStore:
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
    def add_member(self, *, org_id: str, name: str, email: str | None,
                   division: str, role: str | None, actor: str) -> dict:
        with self._pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO team_members (org_id, name, email, division, role)
                   VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                (org_id, name, email, division, role),
            ).fetchone()
            conn.execute(
                """INSERT INTO ops_events (member_id, org_id, event, actor, detail)
                   VALUES (%s,%s,'member.added',%s,%s)""",
                (row["id"], org_id, actor, f"{name} → {division}"
                 + (f" · {role}" if role else "")),
            )
        assert row is not None
        return row

    @translate_db_errors
    def update_member(self, member_id: uuid.UUID, *, org_id: str,
                      changes: dict, actor: str) -> dict | None:
        """Whitelisted patch; every applied change is trailed in the same
        transaction (a roster edit without provenance is not governable)."""
        allowed = {"name", "email", "division", "role", "active"}
        cols = {k: v for k, v in changes.items() if k in allowed and v is not None}
        if not cols:
            return self.get_member(member_id, org_id=org_id)
        with self._pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    f"""UPDATE team_members SET {', '.join(f'{c} = %s' for c in cols)},
                            updated_at = now()
                        WHERE id = %s AND org_id = %s RETURNING *""",
                    (*cols.values(), member_id, org_id),
                ).fetchone()
                if row is not None:
                    conn.execute(
                        """INSERT INTO ops_events (member_id, org_id, event, actor, detail)
                           VALUES (%s,%s,'member.updated',%s,%s)""",
                        (member_id, org_id, actor,
                         ", ".join(f"{c}={'active' if cols[c] else 'inactive'}"
                                   if c == "active" else f"{c}={cols[c]}"
                                   for c in cols)),
                    )
        return row

    def append_event(self, org_id: str, event: str, *, actor: str = "api",
                     detail: str | None = None) -> None:
        self._append_event(org_id, event, actor=actor, detail=detail)

    @translate_db_errors
    def _append_event(self, org_id: str, event: str, *, actor: str,
                      detail: str | None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO ops_events (org_id, event, actor, detail)
                   VALUES (%s,%s,%s,%s)""",
                (org_id, event, actor, detail),
            )

    # -- reads -----------------------------------------------------------------
    @translate_db_errors
    def get_member(self, member_id: uuid.UUID, *, org_id: str) -> dict | None:
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT * FROM team_members WHERE id = %s AND org_id = %s",
                (member_id, org_id),
            ).fetchone()

    @translate_db_errors
    def list_members(self, org_id: str, *, include_inactive: bool = False,
                     limit: int = 200) -> list[dict]:
        with self._pool.connection() as conn:
            if include_inactive:
                rows = conn.execute(
                    """SELECT * FROM team_members WHERE org_id = %s
                       ORDER BY division, name LIMIT %s""",
                    (org_id, max(1, min(limit, 500))),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM team_members WHERE org_id = %s AND active
                       ORDER BY division, name LIMIT %s""",
                    (org_id, max(1, min(limit, 500))),
                ).fetchall()
        return list(rows)

    @translate_db_errors
    def events(self, org_id: str, limit: int = 50) -> list[dict]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT member_id, event, actor, detail, created_at
                   FROM ops_events WHERE org_id = %s ORDER BY id DESC LIMIT %s""",
                (org_id, max(1, min(limit, 200))),
            ).fetchall()
        return list(rows)
