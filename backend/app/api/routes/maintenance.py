"""Maintenance tick — heavy-work entry point (GitHub Actions scheduled).

POST /maintenance/tick  (X-Job-Secret guarded)

Layer 2 division of labour (blueprint §4.1): light jobs run in /jobs/run
batches; heavy work (bulk extraction, embedding re-indexing, off-peak DeepSeek
generation, admin tasks) is enqueued here with a maintenance priority and
either executed by the Actions runner directly or drained over successive
ticks. Keeping the endpoint as pure "enqueue a plan" keeps it inside the HTTP
timeout by construction.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.api.deps import require_job_secret
from app.core.config import get_settings
from app.jobs.queue import PostgresJobQueue

logger = logging.getLogger("caios.maintenance")
router = APIRouter(tags=["maintenance"])

# Maintenance task catalogue — Phase 2+ handlers register real implementations
# in app/jobs/runner.py; kinds stay stable so schedules never change.
MAINTENANCE_KINDS = {
    "reindex_embeddings": "knowledge.embed-maintenance",   # TV-019 re-indexing
    "audit_rollup": "audit.rollup",                        # §22 audit compaction
    "offpeak_generation": "llm.offpeak-batch",             # DeepSeek off-peak runs
}


@router.post("/maintenance/tick", dependencies=[Depends(require_job_secret)])
async def maintenance_tick(body: dict | None = None) -> dict:
    requested = (body or {}).get("task", "all")
    kinds = list(MAINTENANCE_KINDS.values()) if requested == "all" else [requested]
    queue = PostgresJobQueue(get_settings())
    enqueued = []
    try:
        for kind in kinds:
            if kind not in MAINTENANCE_KINDS.values():
                continue
            job_id = queue.enqueue(kind, {"trigger": "maintenance"}, priority=8)
            enqueued.append({"kind": kind, "id": str(job_id)})
    finally:
        queue.close()
    logger.info("maintenance tick enqueued %d job(s)", len(enqueued))
    return {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "enqueued": enqueued}
