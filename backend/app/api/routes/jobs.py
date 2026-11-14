"""Job endpoints.

POST /jobs/run  — machine-facing batch consumer (cron-job.org, every 2–5 min).
                  Secret-guarded; runs ONE bounded batch and returns fast.
POST /jobs      — authenticated enqueue (employees/agents submit work).
GET  /jobs/{id} — authenticated status (assignment progress tracking, §14).
GET  /jobs/queue/stats — authenticated queue depth snapshot.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_principal, require_job_secret
from app.core.config import get_settings
from app.core.security import Principal
from app.jobs.queue import PostgresJobQueue
from app.jobs.runner import run_batch

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _queue() -> PostgresJobQueue:
    return PostgresJobQueue(get_settings())


@router.post("/run", dependencies=[Depends(require_job_secret)])
async def run_now() -> dict:
    """One bounded batch per call — the Layer-2 workhorse. Safe to call
    concurrently: claim uses SKIP LOCKED (TV-016)."""
    settings = get_settings()
    queue = _queue()
    try:
        report = await run_batch(settings, queue)
    finally:
        queue.close()
    return {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **report.as_dict()}


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_job(
    body: dict,
    principal: Principal = Depends(get_current_principal),
) -> dict:
    kind = str(body.get("kind", "")).strip()
    if not kind:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'kind' is required")
    queue = _queue()
    try:
        job_id = queue.enqueue(
            kind,
            {"submitted_by": principal.subject, **(body.get("payload") or {})},
            priority=int(body.get("priority", 5)),
            run_at=_parse_run_at(body.get("run_at")),
        )
    finally:
        queue.close()
    return {"id": str(job_id), "status": "pending"}


@router.get("/queue/stats")
async def queue_stats(_: Principal = Depends(get_current_principal)) -> dict:
    queue = _queue()
    try:
        return queue.counts()
    finally:
        queue.close()


@router.get("/{job_id}")
async def job_status(job_id: uuid.UUID, _: Principal = Depends(get_current_principal)) -> dict:
    queue = _queue()
    try:
        job = queue.get(job_id)
    finally:
        queue.close()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return {
        "id": str(job.id), "kind": job.kind, "status": job.status,
        "attempts": job.attempts, "max_attempts": job.max_attempts,
        "last_error": job.last_error, "result": job.result,
    }


def _parse_run_at(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "'run_at' must be ISO-8601") from exc
