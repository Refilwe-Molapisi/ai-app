"""Central coordination API (Phase 4 — CAIOS-MASTER-001 §11, §12, §13).

    GET  /workflows                 registry: what can be started, and what it needs
    POST /assignments               start an assignment (materialises steps, enqueues first)
    GET  /assignments               list for one org (status filter, summaries only)
    GET  /assignments/{id}          full detail: steps + events (§13 trail)
    POST /assignments/{id}/approve  consume the open gate (human in the loop)
    POST /assignments/{id}/cancel   cancel a non-terminal assignment

Isolation: every route takes org_id explicitly and a verified Client User is
restricted to their own organisation (§16.4) — same rule as documents and
answers. Cross-org reads are 404, never leaks.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_principal
from app.coordination.engine import (
    InvalidWorkflowInput, approve_gate, cancel_assignment, plan_assignment,
    start_assignment,
)
from app.coordination.workflows import registry_summary
from app.core.config import get_settings
from app.core.security import Principal
from app.jobs.queue import PostgresJobQueue

logger = logging.getLogger("caios.coordination")
router = APIRouter(tags=["coordination"])


def _store():
    from app.coordination.store import CoordinationStore
    return CoordinationStore(get_settings())


def _queue() -> PostgresJobQueue:
    return PostgresJobQueue(get_settings())


def _org_for(body_org: str, principal: Principal) -> str:
    org_id = body_org.strip()
    if principal.client_org_id and principal.client_org_id != org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "assignments are restricted to the caller's client organisation")
    return org_id


class AssignmentCreate(BaseModel):
    org_id: str = Field(min_length=1, max_length=120)
    workflow: str = Field(min_length=1, max_length=120)
    title: str | None = Field(default=None, max_length=200)
    engagement_id: str | None = Field(default=None, max_length=120)
    inputs: dict = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=500)


class CancelRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=300)


@router.get("/workflows")
async def list_workflows(
    _: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    return {"count": len(registry_summary()), "workflows": registry_summary()}


@router.post("/assignments", status_code=status.HTTP_201_CREATED)
async def create_assignment(
    body: AssignmentCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    org_id = _org_for(body.org_id, principal)
    try:
        # Pure validation first — an unknown workflow or missing input is a
        # fast 422 before any pool is opened (cold-start friendliness).
        plan_assignment(body.workflow.strip(), body.inputs)
    except InvalidWorkflowInput as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    queue = _queue()
    try:
        return start_assignment(
            get_settings(), queue,
            org_id=org_id, workflow=body.workflow.strip(),
            title=(body.title or "").strip() or None,
            engagement_id=(body.engagement_id or "").strip() or None,
            inputs=body.inputs, created_by=principal.subject,
        )
    finally:
        queue.close()


@router.get("/assignments")
async def list_assignments(
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: int = 50,
) -> dict:
    store = _store()
    try:
        assignments = store.list_assignments(org_id=org_id, status=status_filter,
                                             limit=limit)
    finally:
        store.close()
    return {"org_id": org_id, "count": len(assignments), "assignments": assignments}


@router.get("/assignments/{assignment_id}")
async def assignment_detail(
    assignment_id: uuid.UUID,
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
) -> dict:
    store = _store()
    try:
        assignment = store.get_assignment(assignment_id, org_id=org_id)
    finally:
        store.close()
    if assignment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Assignment not found in this organisation")
    return assignment


@router.post("/assignments/{assignment_id}/approve")
async def approve(
    assignment_id: uuid.UUID,
    body: ApprovalRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    org_id = _org_for(body.org_id, principal)
    queue = _queue()
    try:
        try:
            return approve_gate(get_settings(), queue,
                                assignment_id=assignment_id, org_id=org_id,
                                actor=principal.subject, note=body.note)
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    finally:
        queue.close()


@router.post("/assignments/{assignment_id}/cancel")
async def cancel(
    assignment_id: uuid.UUID,
    body: CancelRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    org_id = _org_for(body.org_id, principal)
    queue = _queue()
    try:
        try:
            return cancel_assignment(get_settings(), queue,
                                     assignment_id=assignment_id, org_id=org_id,
                                     actor=principal.subject, reason=body.reason)
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    finally:
        queue.close()
