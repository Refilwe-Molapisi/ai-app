"""Knowledge & documents API (Phase 2 — CAIOS-MASTER-001 §7, §9).

    POST /documents/upload          multipart upload → storage → extract job (202)
    GET  /documents                 metadata list for one org (no text, no binaries)
    GET  /documents/{id}            ingest status / index metadata
    POST /documents/{id}/reindex    re-run embedding (TV-019 controlled re-index)
    POST /knowledge/search          embed query → cosine search, org-scoped
    GET  /knowledge/stats           corpus snapshot for one org

Isolation model (§7.4, TV-012): every route takes org_id explicitly and never
returns rows from another org. In Phase 4 the workflow engine derives org_id
from verified claims (Principal.client_org_id) instead of trusting the client;
until then uploads already refuse org mismatches when the claim is present.
"""

import hashlib
import logging
import re
import uuid
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.adapters.storage_adapter import StorageAdapter
from app.api.deps import get_current_principal
from app.core.config import Settings, get_settings
from app.core.security import Principal
from app.jobs.queue import PostgresJobQueue
from app.knowledge.store import KnowledgeStore, to_pgvector
from app.knowledge.extraction import SUPPORTED_EXTENSIONS

logger = logging.getLogger("caios.knowledge")
router = APIRouter(tags=["knowledge"])

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _store() -> KnowledgeStore:
    return KnowledgeStore(get_settings())


def _queue() -> PostgresJobQueue:
    return PostgresJobQueue(get_settings())


def _clean_filename(name: str) -> str:
    base = (name or "document").replace("\\", "/").split("/")[-1] or "document"
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "document"
    return cleaned[:120]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    org_id: str = Field(min_length=1, max_length=120)
    top_k: int | None = Field(default=None, ge=1, le=50)
    engagement_id: str | None = None
    scope: str | None = Field(default=None, pattern="^(client|company)$")


@router.post("/documents/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    principal: Annotated[Principal, Depends(get_current_principal)],
    file: UploadFile,
    org_id: Annotated[str, Form(min_length=1, max_length=120)],
    scope: Annotated[str, Form(pattern="^(client|company)$")] = "client",
    engagement_id: Annotated[str | None, Form(max_length=120)] = None,
    title: Annotated[str | None, Form(max_length=200)] = None,
) -> dict:
    """Register a document: bytes → object storage (server-side, §19), metadata
    row → knowledge.extract job. The database never stores file bytes."""
    settings = get_settings()
    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty upload")
    max_bytes = settings.knowledge_max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"upload exceeds {settings.knowledge_max_upload_mb} MB limit")
    if scope == "company":
        org_id = "company"  # internal knowledge base lives under its own namespace
    org_id = org_id.strip()
    if scope == "client" and principal.client_org_id and principal.client_org_id != org_id:
        # A verified Client User can only ever file into their own org (§16.4).
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "uploads are restricted to the caller's client organisation")

    filename = _clean_filename(file.filename or "document")
    suffix = "." + filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if suffix and suffix not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"unsupported document type '{suffix}'; supported: {supported}")

    engagement = (engagement_id or "").strip() or None
    if scope == "client":
        key = StorageAdapter.client_key(org_id, engagement or "_general", "documents", filename)
    else:
        key = StorageAdapter.knowledge_key("documents", filename)

    checksum = hashlib.sha256(data).hexdigest()
    try:
        stored = StorageAdapter(settings).put(
            key, data, content_type=file.content_type or "application/octet-stream")
    except RuntimeError as exc:  # storage unconfigured — loud, per §19
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    store = _store()
    try:
        doc = store.register_document(
            org_id=org_id, scope=scope, engagement_id=engagement,
            title=title or filename, storage_key=stored.key,
            mime_type=file.content_type or "application/octet-stream",
            size_bytes=len(data), checksum_sha256=checksum,
            uploaded_by=principal.subject,
        )
    except psycopg.errors.IntegrityError as exc:
        # Two documents may not share one storage object (ambiguous source of
        # truth). Re-upload of the same file should replace content FIRST or be
        # a deliberate re-index (TV-019) — surface it as a conflict, not a 500.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a document with this storage key is already registered; "
            "replace the object or use /documents/{id}/reindex") from exc
    finally:
        store.close()

    queue = _queue()
    try:
        job_id = queue.enqueue("knowledge.extract",
                               {"document_id": doc["id"], "trigger": "upload"},
                               priority=3)
    finally:
        queue.close()
    return {"id": doc["id"], "status": doc["status"], "storage_key": stored.key,
            "checksum_sha256": checksum, "extract_job_id": str(job_id)}


@router.get("/documents")
async def list_documents(
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
    scope: str | None = None,
    engagement_id: str | None = None,
    limit: int = 50,
) -> dict:
    if scope and scope not in ("client", "company"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "scope must be client|company")
    store = _store()
    try:
        docs = store.list_documents(org_id=org_id, scope=scope,
                                    engagement_id=engagement_id, limit=limit)
    finally:
        store.close()
    return {"org_id": org_id, "count": len(docs), "documents": docs}


@router.get("/documents/{document_id}")
async def document_detail(
    document_id: uuid.UUID,
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
) -> dict:
    store = _store()
    try:
        doc = store.get_document(document_id, org_id=org_id)
    finally:
        store.close()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found in this organisation")
    return doc


@router.post("/documents/{document_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex_document(
    document_id: uuid.UUID,
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
) -> dict:
    """Manual TV-019 trigger: re-embed a document with the CURRENT embedder.
    Chunks are replaced atomically (store.index_chunks) — no mixed-space index."""
    store = _store()
    try:
        doc = store.get_document(document_id, org_id=org_id)
    finally:
        store.close()
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found in this organisation")
    if not doc.get("extracted_text") and doc["status"] not in ("extracted", "indexed"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "document is not extracted yet; wait for knowledge.extract")
    queue = _queue()
    try:
        job_id = queue.enqueue("knowledge.embed",
                               {"document_id": str(document_id), "trigger": "reindex",
                                "force": True}, priority=4)
    finally:
        queue.close()
    return {"id": str(document_id), "status": doc["status"], "embed_job_id": str(job_id)}


@router.post("/knowledge/search")
async def knowledge_search(
    body: SearchRequest,
    _: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    """Semantic search inside one org. Chunks indexed by a different embedder
    than the one answering this query are EXCLUDED (never compared across
    vector spaces) — an empty result plus a hint means: re-index (TV-019)."""
    from app.adapters.embedding_adapter import get_embedding_adapter

    settings = get_settings()
    try:
        embedder = get_embedding_adapter(settings)
        result = await embedder.embed([body.query])
        vector = to_pgvector(result.vectors[0])
    except RuntimeError as exc:  # no embedder reachable
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    store = _store()
    try:
        hits = store.search(
            org_id=body.org_id, query_vector=vector,
            provider=result.provider, model=result.model, dimensions=result.dimensions,
            top_k=body.top_k or settings.knowledge_top_k,
            engagement_id=body.engagement_id, scope=body.scope,
        )
        stats = store.stats(body.org_id)
    finally:
        store.close()
    return {
        "query": body.query,
        "embedder": {"provider": result.provider, "model": result.model,
                     "dimensions": result.dimensions},
        "index": stats,
        "results": hits,
        "hint": None if hits else
        ("no chunks indexed for this org yet" if not stats["chunks"]
         else "index was built with a different embedder — re-index documents (TV-019)"),
    }


@router.get("/knowledge/stats")
async def knowledge_stats(
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
) -> dict:
    store = _store()
    try:
        return {"org_id": org_id, **store.stats(org_id)}
    finally:
        store.close()
