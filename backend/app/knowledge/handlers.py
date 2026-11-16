"""Phase 2 job handlers — the knowledge ingestion pipeline (§7, §9).

    knowledge.extract   storage → text          (status: uploaded → extracting → extracted)
    knowledge.embed     text → chunks → vectors (status: extracted → indexing → indexed)

Both are queue jobs like any other, executed inside the /jobs/run budget by
the runner. `extract` chains `embed` automatically, so a single upload drains
to a searchable index across cron ticks with no orchestrator. Failures mark
the document 'failed' with last_error AND fail the job — the queue's retry
policy then applies (TV-016), because a transient storage outage must retry,
not strand a half-ingested document.

Adapters are resolved lazily via the _storage()/_embedder() indirections so
tests (and future Layers) can substitute fakes without monkeypatching modules
at import time.
"""

import logging
import uuid

from app.core.config import Settings, get_settings
from app.jobs.queue import PostgresJobQueue
from app.jobs.runner import Handler, Job
from app.knowledge.chunking import chunk_text
from app.knowledge.extraction import extract_document
from app.knowledge.store import KnowledgeStore, to_pgvector

logger = logging.getLogger("caios.knowledge")


def _store(settings: Settings | None = None) -> KnowledgeStore:
    return KnowledgeStore(settings or get_settings())


def _storage():
    from app.adapters.storage_adapter import get_storage_adapter
    return get_storage_adapter(get_settings())


def _embedder():
    from app.adapters.embedding_adapter import get_embedding_adapter
    return get_embedding_adapter(get_settings())


def _transient_queue(settings: Settings | None = None) -> PostgresJobQueue:
    """Fallback for direct handler invocation without a runner-owned queue."""
    return PostgresJobQueue(settings or get_settings())


def _doc_id(job: Job) -> uuid.UUID:
    raw = (job.payload or {}).get("document_id")
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError):
        raise ValueError(f"job '{job.kind}' requires a valid 'document_id' payload (got {raw!r})")


def make_extract_handler(queue: PostgresJobQueue | None = None) -> Handler:
    async def handle_extract(job: Job) -> dict:
        doc_id = _doc_id(job)
        store = _store()
        try:
            doc = store.get_document(doc_id)
        finally:
            store.close()
        if doc is None:
            raise ValueError(f"document {doc_id} not found")
        if doc["status"] in ("indexed", "extracted"):
            return {"document_id": str(doc_id), "status": doc["status"], "skipped": True}

        store = _store()
        try:
            store.update_document(doc_id, status="extracting")
            try:
                data = _storage().get(doc["storage_key"])
                extracted = extract_document(
                    data, filename=doc["title"], mime_type=doc["mime_type"],
                )
            except Exception as exc:  # noqa: BLE001 — mark document failed, then re-raise for queue retry
                message = f"extraction failed: {type(exc).__name__}: {exc}"
                store.update_document(doc_id, status="failed",
                                      last_error=message[:500])
                raise
            store.update_document(
                doc_id, status="extracted", extractor=extracted.extractor_id,
                extracted_text=extracted.text, text_chars=len(extracted.text),
                last_error=None,
            )
        finally:
            store.close()

        q = queue if queue is not None else _transient_queue()
        try:
            embed_job = q.enqueue(
                "knowledge.embed",
                {"document_id": str(doc_id), "trigger": "knowledge.extract"},
                priority=3,
            )
        finally:
            if queue is None:
                q.close()
        logger.info("document %s extracted (%d chars); embed job %s enqueued",
                    doc_id, len(extracted.text), embed_job)
        return {
            "document_id": str(doc_id), "status": "extracted",
            "chars": len(extracted.text), "extractor": extracted.extractor_id,
            "kind": extracted.kind, "embed_job_id": str(embed_job),
        }

    return handle_extract


def make_embed_handler(queue: PostgresJobQueue | None = None) -> Handler:  # queue kept for symmetry/future chaining
    async def handle_embed(job: Job) -> dict:
        doc_id = _doc_id(job)
        settings = get_settings()
        store = _store(settings)
        try:
            doc = store.get_document(doc_id)
            if doc is None:
                raise ValueError(f"document {doc_id} not found")
            text = doc.get("extracted_text")
            if not text:
                raise ValueError("document has no extracted text; run knowledge.extract first")

            # Idempotency: same embedder + already indexed → nothing to do.
            embedder = _embedder()
            probe = await embedder.embed(["__probe__"])
            if (doc["status"] == "indexed"
                    and doc["embed_provider"] == probe.provider
                    and doc["embed_model"] == probe.model
                    and doc["embed_dimensions"] == probe.dimensions
                    and not job.payload.get("force")):
                return {"document_id": str(doc_id), "status": "indexed", "skipped": True}

            store.update_document(doc_id, status="indexing")
            try:
                chunks = chunk_text(text, settings.knowledge_chunk_chars,
                                    settings.knowledge_chunk_overlap)
                rows: list[dict] = []
                dims: int | None = None
                for offset in range(0, len(chunks), settings.knowledge_embed_batch):
                    batch = chunks[offset:offset + settings.knowledge_embed_batch]
                    result = await embedder.embed([c.content for c in batch])
                    if result.dimensions == 0:
                        raise ValueError("embedder returned no vectors")
                    if dims is not None and result.dimensions != dims:
                        raise ValueError(
                            f"embedder dimension drift within one document "
                            f"({dims} vs {result.dimensions}) — refusing mixed-space index")
                    dims = result.dimensions
                    for chunk, vector in zip(batch, result.vectors):
                        if len(vector) != dims:
                            raise ValueError("embedder returned ragged vectors")
                        rows.append({
                            "seq": chunk.seq, "content": chunk.content,
                            "char_start": chunk.char_start, "char_end": chunk.char_end,
                            "vector": to_pgvector(vector),
                        })
                if dims is None:
                    raise ValueError("no chunks produced — nothing to embed")
                store.index_chunks(doc_id, org_id=doc["org_id"], chunks=rows,
                                   provider=probe.provider, model=probe.model,
                                   dimensions=dims)
            except Exception as exc:  # noqa: BLE001 — mark document, then re-raise for queue retry
                store.update_document(doc_id, status="failed",
                                      last_error=f"embedding failed: {type(exc).__name__}: {exc}"[:500])
                raise
        finally:
            store.close()
        logger.info("document %s indexed: %d chunks, %s/%s (%d dims)",
                    doc_id, len(rows), probe.provider, probe.model, dims)
        return {
            "document_id": str(doc_id), "status": "indexed", "chunks": len(rows),
            "provider": probe.provider, "model": probe.model, "dimensions": dims,
        }

    return handle_embed


def default_knowledge_handlers(queue: PostgresJobQueue | None = None) -> dict[str, Handler]:
    return {
        "knowledge.extract": make_extract_handler(queue),
        "knowledge.embed": make_embed_handler(queue),
    }
