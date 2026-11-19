"""Internal-ops API (Phase 8 — CAIOS-OPS-001).

    POST /ops/members        add a team member to a division
    GET  /ops/members        roster for one org (active by default)
    PATCH /ops/members/{id}  change role/division/active — every change trailed
    GET  /ops/members/{id}/events   roster trail for one member
    GET  /ops/overview       honest cross-module rollup for the ops desk

The overview aggregates counts that already exist in the other modules'
stores (queue, coordination, deliverables, AI ledger) plus the roster —
it never invents numbers, it counts rows. Workload per division comes from
the workflow→division allocation in ops/workflows_map.py, which is
unit-tested against the coordination registry.

Isolation model (§7.4, TV-012): every route takes org_id explicitly; ids
alone are never enough to cross the boundary (wrong org → 404).
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_principal
from app.core.config import get_settings
from app.core.security import Principal
from app.ops.store import OpsStore
from app.ops.workflows_map import DIVISIONS, WORKFLOW_DIVISIONS, division_of_workflow

logger = logging.getLogger("caios.ops")
router = APIRouter(tags=["ops"])

OPEN_ASSIGNMENT_STATUSES = ("running", "awaiting_approval")


def _store() -> OpsStore:
    return OpsStore(get_settings())


class NewMember(BaseModel):
    org_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=200)
    division: str = Field(min_length=1, max_length=60)
    role: str | None = Field(default=None, max_length=120)


class MemberUpdate(BaseModel):
    org_id: str = Field(min_length=1, max_length=120)
    name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=200)
    division: str | None = Field(default=None, max_length=60)
    role: str | None = Field(default=None, max_length=120)
    active: bool | None = None


@router.post("/ops/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    body: NewMember,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    if body.division not in DIVISIONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"division must be one of: {', '.join(DIVISIONS)}")
    actor = (getattr(principal, "subject", None) or "api")[:80]
    store = _store()
    try:
        try:
            row = store.add_member(org_id=body.org_id, name=body.name.strip(),
                                   email=body.email, division=body.division,
                                   role=body.role, actor=actor)
        except Exception as exc:  # unique (org_id, name) violation → honest 422
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    f"a member named {body.name!r} already exists in this org")
            raise
    finally:
        store.close()
    return {"member": row}


@router.get("/ops/members")
async def list_members(
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
    include_inactive: bool = False,
) -> dict:
    store = _store()
    try:
        rows = store.list_members(org_id, include_inactive=include_inactive)
        events = store.events(org_id, limit=10)
    finally:
        store.close()
    by_division = {d: 0 for d in DIVISIONS}
    for r in rows:
        by_division[r["division"]] = by_division.get(r["division"], 0) + 1
    return {"org_id": org_id, "count": len(rows), "by_division": by_division,
            "members": rows, "recent_events": events}


@router.patch("/ops/members/{member_id}")
async def update_member(
    member_id: uuid.UUID,
    body: MemberUpdate,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    if body.division is not None and body.division not in DIVISIONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"division must be one of: {', '.join(DIVISIONS)}")
    actor = (getattr(principal, "subject", None) or "api")[:80]
    changes = {k: v for k, v in body.model_dump().items()
               if k != "org_id" and v is not None}
    store = _store()
    try:
        row = store.update_member(member_id, org_id=body.org_id,
                                  changes=changes, actor=actor)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    finally:
        store.close()
    return {"member": row, "changes": sorted(changes)}


@router.get("/ops/members/{member_id}/events")
async def member_events(
    member_id: uuid.UUID,
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
) -> dict:
    store = _store()
    try:
        if store.get_member(member_id, org_id=org_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
        events = [e for e in store.events(org_id, limit=200)
                  if e.get("member_id") == member_id]
    finally:
        store.close()
    return {"member_id": str(member_id), "count": len(events), "events": events}


@router.get("/ops/overview")
async def ops_overview(
    _: Annotated[Principal, Depends(get_current_principal)],
    org_id: str,
) -> dict:
    """One honest rollup for the ops desk: roster, open workload per division
    (via the workflow allocation), queue health, deliverable QA states and the
    AI ledger — all counted from the modules' own stores."""
    from app.ai.store import AIStore
    from app.coordination.store import CoordinationStore
    from app.deliverables.store import DeliverableStore
    from app.jobs.queue import PostgresJobQueue

    settings = get_settings()
    ops = _store()
    coord = CoordinationStore(settings)
    deliv = DeliverableStore(settings)
    ai = AIStore(settings)
    queue = PostgresJobQueue(settings)
    try:
        members = ops.list_members(org_id, include_inactive=True)
        assignment_counts = coord.status_counts(org_id)
        deliverable_counts = deliv.status_counts(org_id)
        qa_counts = deliv.qa_counts(org_id)
        job_counts = queue.counts()
        ai_summary = ai.usage_summary(org_id=org_id, days=30)
    finally:
        for s in (ops, coord, deliv, ai, queue):
            s.close()

    by_division = {d: {"members": 0, "active": 0} for d in DIVISIONS}
    for m in members:
        bucket = by_division.setdefault(m["division"], {"members": 0, "active": 0})
        bucket["members"] += 1
        if m["active"]:
            bucket["active"] += 1

    open_by_division = {d: 0 for d in DIVISIONS}
    for workflow, n in assignment_counts.get("open_by_workflow", {}).items():
        open_by_division[division_of_workflow(workflow)] = \
            open_by_division.get(division_of_workflow(workflow), 0) + n

    return {
        "org_id": org_id,
        "team": {"total": len(members),
                 "active": sum(1 for m in members if m["active"]),
                 "by_division": by_division},
        "assignments": {"by_status": assignment_counts["by_status"],
                        "open_by_division": open_by_division},
        "jobs": {"by_status": job_counts},
        "deliverables": {"by_status": deliverable_counts, "by_qa_state": qa_counts},
        "ai": ai_summary,
        "workflow_allocation": dict(WORKFLOW_DIVISIONS),
    }
