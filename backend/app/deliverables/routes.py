"""Deliverables API (Phase 6 — CAIOS-DEL-001; Phase 7 QA — CAIOS-QA-001).

    POST /deliverables                 spec → row (queued) + report.generate job (202)
    GET  /deliverables                 metadata list for one org (no specs, no bytes)
    GET  /deliverables/{id}            full record: spec, status, event trail
    GET  /deliverables/{id}/download   stream the artefact through the API (authorised)
    POST /deliverables/{id}/submit-review      draft|changes_requested → in_review
    POST /deliverables/{id}/qa                 in_review → approved | changes_requested
    POST /deliverables/{id}/revise             new spec version → queued, QA gate reset
    POST /deliverables/{id}/regenerate         re-render current version (force)
    GET  /deliverables/{id}/versions           immutable per-version artefact records

Isolation model (§7.4, TV-012): every route takes org_id explicitly and never
returns rows from another org; ids alone are never enough to cross the
boundary (wrong org → 404, same as coordination). Downloads stream through
the backend so access stays behind the authorisation layer — no public URLs,
matching the storage posture (§7.4).

QA gate (§13): only qa_state='approved' is releasable. Transitions are a
state machine enforced here with honest 409s; the reviewer identity comes
from the verified principal, never the request body.
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

# QA state machine (Phase 7): (current state, decision) → next state.
# submit-review moves draft|changes_requested → in_review; decisions only
# apply to in_review. Anything else is a 409, never a silent overwrite.
QA_DECISIONS = {"approve": "approved", "request_changes": "changes_requested"}
QA_ALLOWED_FROM = {"approve": ("in_review",), "request_changes": ("in_review",)}
SUBMIT_ALLOWED_FROM = ("draft", "changes_requested")


def qa_transition(current: str, decision: str) -> str:
    """Pure helper so the transition matrix is unit-testable."""
    if decision not in QA_DECISIONS:
        raise ValueError(f"unknown QA decision: {decision!r}")
    if current not in QA_ALLOWED_FROM[decision]:
        raise ValueError(f"cannot {decision} from qa_state {current!r}")
    return QA_DECISIONS[decision]


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
    created_by = (getattr(principal, "subject", None) or "api")[:80]
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


# ── Phase 7: QA governance (CAIOS-QA-001) ───────────────────────────────────

class QADecision(BaseModel):
    org_id: str = Field(min_length=1, max_length=120)
    decision: str = Field(pattern="^(approve|request_changes)$")
    comment: str | None = Field(default=None, max_length=2000)
    checks: dict[str, bool] = Field(default_factory=dict, max_length=10)


class ReviseRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=120)
    spec: Spec
    comment: str | None = Field(default=None, max_length=500)


class OrgRef(BaseModel):
    org_id: str = Field(min_length=1, max_length=120)


def _load_owned(store: DeliverableStore, did: uuid.UUID, org_id: str) -> dict:
    row = store.get(did, org_id=org_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deliverable not found")
    return row


def _enqueue_generate(did: uuid.UUID, *, force: bool = False) -> str:
    q = _queue()
    try:
        payload: dict = {"deliverable_id": str(did)}
        if force:
            payload["force"] = True
        return str(q.enqueue("report.generate", payload, priority=4))
    finally:
        q.close()


@router.post("/deliverables/{deliverable_id}/submit-review")
async def submit_for_review(
    deliverable_id: uuid.UUID,
    body: OrgRef,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    actor = (getattr(principal, "subject", None) or "api")[:80]
    store = _store()
    try:
        row = _load_owned(store, deliverable_id, body.org_id)
        if row["status"] != "ready":
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"only ready deliverables can enter review (status: {row['status']})")
        if row["qa_state"] not in SUBMIT_ALLOWED_FROM:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"cannot submit from qa_state {row['qa_state']!r}")
        row = store.set_qa(deliverable_id, qa_state="in_review", reviewer=actor,
                           comment=row["qa_comment"], checks=row["qa_checks"])
        store.append_event(deliverable_id, "qa.submitted", actor=actor,
                           detail=f"v{row['spec_version']} submitted for QA review")
    finally:
        store.close()
    return {"id": str(row["id"]), "qa_state": row["qa_state"],
            "version": row["spec_version"]}


@router.post("/deliverables/{deliverable_id}/qa")
async def qa_decision(
    deliverable_id: uuid.UUID,
    body: QADecision,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    reviewer = (getattr(principal, "subject", None) or "api")[:80]
    if body.decision == "request_changes" and not (body.comment or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "request_changes needs a comment — 'changes requested' without a reason is not governable")
    store = _store()
    try:
        row = _load_owned(store, deliverable_id, body.org_id)
        try:
            next_state = qa_transition(row["qa_state"], body.decision)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
        row = store.set_qa(deliverable_id, qa_state=next_state, reviewer=reviewer,
                           comment=body.comment, checks=body.checks or None)
        store.append_event(deliverable_id,
                           "qa.approved" if next_state == "approved" else "qa.changes_requested",
                           actor=reviewer,
                           detail=f"v{row['spec_version']} → {next_state}"
                                  + (f" · {body.comment[:200]}" if body.comment else "")
                                  + (f" · checks: {sorted(k for k, v in (body.checks or {}).items() if v)}"
                                     if body.checks else ""))
    finally:
        store.close()
    return {"id": str(row["id"]), "qa_state": row["qa_state"],
            "qa_reviewer": row["qa_reviewer"], "version": row["spec_version"]}


@router.post("/deliverables/{deliverable_id}/revise", status_code=status.HTTP_202_ACCEPTED)
async def revise_deliverable(
    deliverable_id: uuid.UUID,
    body: ReviseRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    actor = (getattr(principal, "subject", None) or "api")[:80]
    spec = body.spec.model_dump()
    if not spec["sections"] and not spec["tables"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "revised spec must contain at least one section or table")
    store = _store()
    try:
        row = _load_owned(store, deliverable_id, body.org_id)
        if row["status"] not in ("ready", "failed"):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"deliverable is {row['status']!r} — wait for the current build to finish before revising")
        previous_version = row["spec_version"]
        row = store.revise(deliverable_id, new_spec=spec, comment=body.comment)
        store.append_event(deliverable_id, "qa.gate_reset", actor=actor,
                           detail=f"revision supersedes v{previous_version}; "
                                  "QA approval does not carry over")
    finally:
        store.close()
    job_id = _enqueue_generate(deliverable_id)
    return {"id": str(row["id"]), "version": row["spec_version"],
            "previous_version": previous_version, "status": row["status"],
            "qa_state": row["qa_state"], "job_id": job_id}


@router.post("/deliverables/{deliverable_id}/regenerate", status_code=status.HTTP_202_ACCEPTED)
async def regenerate_deliverable(
    deliverable_id: uuid.UUID,
    body: OrgRef,
    _: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    store = _store()
    try:
        row = _load_owned(store, deliverable_id, body.org_id)
        if row["status"] not in ("ready", "failed"):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"deliverable is {row['status']!r} — nothing to regenerate yet")
        version = row["spec_version"]
        store.append_event(deliverable_id, "deliverable.regenerated", actor="worker",
                           detail=f"v{version} re-render requested")
    finally:
        store.close()
    job_id = _enqueue_generate(deliverable_id, force=True)
    return {"id": str(deliverable_id), "status": "queued", "version": version,
            "job_id": job_id,
            "note": "deterministic re-render: identical bytes keep QA approval, checksum drift voids it"}


@router.get("/deliverables/{deliverable_id}/versions")
async def deliverable_versions(
    deliverable_id: uuid.UUID,
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
) -> dict:
    store = _store()
    try:
        _load_owned(store, deliverable_id, org_id)
        versions = store.get_versions(deliverable_id)
    finally:
        store.close()
    return {"deliverable_id": str(deliverable_id), "count": len(versions),
            "versions": versions}
