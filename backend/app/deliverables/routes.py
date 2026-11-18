"""Deliverables API (Phase 6 — CAIOS-DEL-001).

    POST /deliverables                 spec → row (queued) + report.generate job (202)
    GET  /deliverables                 metadata list for one org (no specs, no bytes)
    GET  /deliverables/{id}            full record: spec, status, event trail
    GET  /deliverables/{id}/download   stream the artefact through the API (authorised)

Isolation model (§7.4, TV-012): every route takes org_id explicitly and never
returns rows from another org; ids alone are never enough to cross the
boundary (wrong org → 404, same as coordination). Downloads stream through
the backend so access stays behind the authorisation layer — no public URLs,
matching the storage posture (§7.4).
"""

import logging
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints

from app.api.deps import get_current_principal
from app.core.config import get_settings
from app.core.security import Principal
from app.deliverables.generate import SpecError, MIME_BY_KIND
from app.deliverables.store import DeliverableStore
from app.jobs.queue import PostgresJobQueue

logger = logging.getLogger("caios.deliverables")
router = APIRouter(tags=["deliverables"])

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _store() -> DeliverableStore:
    return DeliverableStore(get_settings())


def _queue() -> PostgresJobQueue:
    return PostgresJobQueue(get_settings())


def _storage():
    """Same lazy indirection as the handlers — one place to resolve the
    storage adapter (S3-compatible or the dev local dir)."""
    from app.adapters.storage_adapter import get_storage_adapter
    return get_storage_adapter(get_settings())


class Section(BaseModel):
    heading: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=20_000)
    bullets: list[Annotated[str, StringConstraints(max_length=500)]] = Field(
        default_factory=list, max_length=50)


class Table(BaseModel):
    title: str = Field(default="", max_length=120)
    columns: list[Annotated[str, StringConstraints(max_length=80)]] = Field(
        min_length=1, max_length=30)
    rows: list[list[str | int | float | None]] = Field(default_factory=list, max_length=5000)


class Spec(BaseModel):
    meta: dict[str, str] = Field(default_factory=dict)
    sections: list[Section] = Field(default_factory=list, max_length=50)
    tables: list[Table] = Field(default_factory=list, max_length=10)


class NewDeliverable(BaseModel):
    org_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern="^(docx|xlsx|pdf|md)$")
    scope: str = Field(default="company", pattern="^(client|company)$")
    engagement_id: str | None = Field(default=None, max_length=120)
    assignment_id: uuid.UUID | None = None
    spec: Spec


@router.post("/deliverables", status_code=status.HTTP_202_ACCEPTED)
async def create_deliverable(
    body: NewDeliverable,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    if body.scope == "client" and body.engagement_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "client-scope deliverables need an engagement_id")
    spec = body.spec.model_dump()
    if not spec["sections"] and not spec["tables"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "spec must contain at least one section or table")
    created_by = (getattr(principal, "sub", None) or "api")[:80]
    store = _store()
    try:
        row = store.create(org_id=body.org_id, scope=body.scope,
                           engagement_id=body.engagement_id,
                           assignment_id=body.assignment_id, title=body.title,
                           kind=body.kind, spec=spec, created_by=created_by)
    finally:
        store.close()
    q = _queue()
    try:
        job_id = q.enqueue("report.generate", {"deliverable_id": str(row["id"])},
                           priority=4)
    finally:
        q.close()
    return {"id": str(row["id"]), "status": row["status"], "kind": body.kind,
            "job_id": str(job_id)}


@router.get("/deliverables")
async def list_deliverables(
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
    limit: int = 50,
) -> dict:
    store = _store()
    try:
        rows = store.list(org_id, limit=limit)
    finally:
        store.close()
    return {"org_id": org_id, "count": len(rows), "deliverables": rows}


@router.get("/deliverables/{deliverable_id}")
async def deliverable_detail(
    deliverable_id: uuid.UUID,
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
) -> dict:
    store = _store()
    try:
        row = store.get(deliverable_id, org_id=org_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "deliverable not found")
        events = store.events(deliverable_id)
    finally:
        store.close()
    return {"deliverable": row, "events": events}


@router.get("/deliverables/{deliverable_id}/download")
async def download_deliverable(
    deliverable_id: uuid.UUID,
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
) -> Response:
    store = _store()
    try:
        row = store.get(deliverable_id, org_id=org_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "deliverable not found")
        if row["status"] != "ready" or not row["storage_key"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"deliverable is not ready (status: {row['status']})"
                + (f" — {row['last_error']}" if row["last_error"] else ""))
        key, mime, title, kind = row["storage_key"], row["mime_type"], row["title"], row["kind"]
    finally:
        store.close()
    data = _storage().get(key)
    filename = _SAFE_NAME.sub("_", title).strip("._")[:100] or "deliverable"
    return Response(
        content=data,
        media_type=mime or MIME_BY_KIND.get(kind, "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{filename}.{kind}"'},
    )
