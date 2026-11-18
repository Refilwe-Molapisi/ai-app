"""Agents API (Phase 5 — §11).

    GET /agents    registry listing: division agents, responsibilities,
                   permitted tools, required outputs, quality standards,
                   constraints and how to override instructions per org.

Execution is not exposed as a free-form endpoint on purpose: agent runs are
governed work, started as workflow steps through POST /assignments (the
coordinator is the only entry point, §11.4 "workflow coordination"). Direct
ad-hoc invocation would bypass gates, scopes and the assignment trail.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from app.agents.definitions import agents_summary
from app.api.deps import get_current_principal
from app.core.security import Principal

logger = logging.getLogger("caios.agents")
router = APIRouter(tags=["agents"])


@router.get("/agents")
async def list_agents(
    _: Annotated[Principal, Depends(get_current_principal)],
) -> dict:
    return {"count": len(agents_summary()), "agents": agents_summary()}
