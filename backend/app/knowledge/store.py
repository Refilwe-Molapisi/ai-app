"""Knowledge store — Postgres data access for documents and chunks.

Same architecture posture as the job queue (ADR-009): a thin class over
psycopg, connection-pooled, cold-start failures translated to
`QueueUnavailable` so the API surfaces clean 503s and cron callers retry.

Isolation (§7.4, TV-012): org_id is part of every table, every read and every
search. Reads that pass an org filter simply cannot see another client's rows.

Embedding metadata lives on both documents (current index state) and chunks
(per-chunk provenance). Searches pin provider+model+dimensions so vectors from
different embedders are never compared (they live in different spaces —
comparing them would silently return nonsense, the exact failure TV-019
re-indexing exists to prevent).
"""

import functools
import json
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app.core.config import Settings, get_settings
from app.jobs.queue import QueueUnavailable, _DB_ERRORS

_TRANSLATABLE = _DB_ERRORS + (psycopg.errors.UndefinedTable, psycopg.errors.FeatureNotSupported)


def translate_db_errors(fn):
    """Same contract as the queue decorator, plus schema-not-ready conditions."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except _TRANSLATABLE as exc:
            raise QueueUnavailable(f"knowledge store unavailable: {type(exc).__name__}") from exc
    return wrapper


class KnowledgeStore:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._pool = ConnectionPool(
            settings.database_url, min_size=1, max_size=2, open=True,
            timeout=10,
            kwargs={"autocommit": True, "row_factory": dict_row},  # rows by column name
        )

    # -- documents ------------------------------------------------------------
    @translate_db_errors
    def register_document(self, *, org_id: str, scope: str, engagement_id: str | None,
                          title: str, storage_key: str, mime_type: str,
                          size_bytes: int, checksum_sha256: str,
                          uploaded_by: str | None = None) -> dict:
        with self._pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO documents
                       (org_id, scope, engagement_id, title, storage_key, mime_type,
                        size_bytes, checksum_sha256, uploaded_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id, org_id, scope, engagement_id, title, storage_key,
                             status, created_at""",
                (org_id, scope, engagement_id or None, title, storage_key, mime_type,
                 size_bytes, checksum_sha256, uploaded_by),
            ).fetchone()
        return _doc_dict(row)

    @translate_db_errors
    def get_document(self, document_id: uuid.UUID, org_id: str | None = None) -> dict | None:
        query = ("SELECT id, org_id, scope, engagement_id, title, storage_key, mime_type,"
                 " size_bytes, checksum_sha256, status, extractor, extracted_text,"
                 " text_chars, chunk_count, embed_provider, embed_model, embed_dimensions,"
                 " last_error, uploaded_by, created_at, updated_at"
                 " FROM documents WHERE id=%s")
        params: list = [document_id]
        if org_id is not None:  # isolation: an org-scoped read cannot cross tenants
            query += " AND org_id=%s"
            params.append(org_id)
        with self._pool.connection() as conn:
            row = conn.execute(query, params).fetchone()
        return _doc_dict(row) if row else None

    @translate_db_errors
    def list_documents(self, *, org_id: str, scope: str | None = None,
                       engagement_id: str | None = None, limit: int = 50) -> list[dict]:
        query = ("SELECT id, org_id, scope, engagement_id, title, storage_key, mime_type,"
                 " size_bytes, status, chunk_count, embed_provider, embed_model,"
                 " embed_dimensions, text_chars, last_error, created_at, updated_at"
                 " FROM documents WHERE org_id=%s")
        params: list = [org_id]
        if scope:
            query += " AND scope=%s"
            params.append(scope)
        if engagement_id:
            query += " AND engagement_id=%s"
            params.append(engagement_id)
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(max(1, min(limit, 200)))
        with self._pool.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_doc_dict(r) for r in rows]

    # Whitelist — handlers only ever set these columns, never raw SQL fragments.
    _MUTABLE = {"status", "extractor", "extracted_text", "text_chars", "chunk_count",
                "embed_provider", "embed_model", "embed_dimensions", "last_error"}

    @translate_db_errors
    def update_document(self, document_id: uuid.UUID, **fields) -> dict | None:
        unknown = set(fields) - self._MUTABLE
        if unknown:
            raise ValueError(f"cannot update document fields: {sorted(unknown)}")
        if not fields:
            return self.get_document(document_id)
        sets = ", ".join(f"{col}=%s" for col in fields)
        params = list(fields.values()) + [document_id]
        with self._pool.connection() as conn:
            row = conn.execute(
                f"UPDATE documents SET {sets} WHERE id=%s"
                " RETURNING id, org_id, scope, engagement_id, title, storage_key,"
                " status, extractor, text_chars, chunk_count, embed_provider,"
                " embed_model, embed_dimensions, last_error", params,
            ).fetchone()
        return _doc_dict(row) if row else None

    # -- indexing -------------------------------------------------------------
    @translate_db_errors
    def index_chunks(self, document_id: uuid.UUID, *, org_id: str,
                     chunks: list[dict], provider: str, model: str, dimensions: int) -> int:
        """Atomically replace all chunks of a document (idempotent re-index,
        TV-019) and flip the document to 'indexed'. Vector values arrive as
        pgvector text literals '[0.1,0.2,…]' (see to_pgvector)."""
        rows = [
            (document_id, org_id, c["seq"], c["content"], c["char_start"], c["char_end"],
             c["vector"], provider, model, dimensions)
            for c in chunks
        ]
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM document_chunks WHERE document_id=%s", (document_id,))
            if rows:
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO document_chunks
                               (document_id, org_id, seq, content, char_start, char_end,
                                embedding, embed_provider, embed_model, embed_dimensions)
                           VALUES (%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s)""", rows,
                    )
            conn.execute(
                """UPDATE documents SET status='indexed', chunk_count=%s,
                          embed_provider=%s, embed_model=%s, embed_dimensions=%s,
                          last_error=NULL
                   WHERE id=%s""",
                (len(rows), provider, model, dimensions, document_id),
            )
        return len(rows)

    # -- search ---------------------------------------------------------------
    @translate_db_errors
    def search(self, *, org_id: str, query_vector: str, provider: str, model: str,
               dimensions: int, top_k: int = 6, engagement_id: str | None = None,
               scope: str | None = None) -> list[dict]:
        """Cosine search inside ONE org (§7.4). Vectors from other embedders are
        excluded — cross-model similarity is meaningless, not just imprecise."""
        query = """SELECT c.document_id, c.seq, c.content, c.char_start, c.char_end,
                          d.title, d.engagement_id, d.scope,
                          c.embedding <=> %s::vector AS distance
                   FROM document_chunks c
                   JOIN documents d ON d.id = c.document_id
                   WHERE c.org_id=%s
                     AND c.embed_provider=%s AND c.embed_model=%s
                     AND c.embed_dimensions=%s"""
        params: list = [query_vector, org_id, provider, model, dimensions]
        if engagement_id:
            query += " AND d.engagement_id=%s"
            params.append(engagement_id)
        if scope:
            query += " AND d.scope=%s"
            params.append(scope)
        query += " ORDER BY distance LIMIT %s"
        params.append(max(1, min(top_k, 50)))
        with self._pool.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "document_id": str(r["document_id"]), "title": r["title"],
                "engagement_id": r["engagement_id"], "scope": r["scope"],
                "seq": r["seq"], "content": r["content"],
                "char_start": r["char_start"], "char_end": r["char_end"],
                "distance": round(float(r["distance"]), 6),
            }
            for r in rows
        ]

    @translate_db_errors
    def stats(self, org_id: str) -> dict:
        with self._pool.connection() as conn:
            doc_rows = conn.execute(
                "SELECT status, count(*) AS n FROM documents WHERE org_id=%s GROUP BY status",
                (org_id,)).fetchall()
            chunk = conn.execute(
                "SELECT count(*) AS n, COALESCE(sum(length(content)),0) AS chars"
                " FROM document_chunks WHERE org_id=%s", (org_id,)).fetchone()
        return {
            "documents_by_status": {r["status"]: r["n"] for r in doc_rows},
            "chunks": chunk["n"],
            "indexed_chars": chunk["chars"],
        }

    def close(self) -> None:
        self._pool.close()


def to_pgvector(vector: list[float]) -> str:
    """pgvector text literal for a psycopg %s::vector cast — no extra driver
    dependency needed for the portable skeleton."""
    return "[" + ",".join(f"{v:.7g}" for v in vector) + "]"


def _doc_dict(row) -> dict:
    out = dict(row)
    for key in ("created_at", "updated_at"):
        if key in out and isinstance(out[key], datetime):
            out[key] = out[key].astimezone(timezone.utc).isoformat(timespec="seconds")
    for key in ("id", "document_id"):
        if key in out and isinstance(out[key], uuid.UUID):
            out[key] = str(out[key])
    return out


def json_safe(obj):
    return json.loads(json.dumps(obj, default=str))
