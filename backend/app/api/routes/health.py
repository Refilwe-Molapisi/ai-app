"""Health & keep-alive endpoint (Layer 2 pattern, blueprint §4.3).

cron-job.org pings this every 10 minutes to hold the Render free service warm
(~744 of 750 monthly instance-hours). It must be cheap, stateless and
cold-start-safe: no database round-trip unless ?deep=1 (used by monitoring,
not by keep-alive).
"""

from datetime import datetime, timezone

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(deep: bool = False) -> dict:
    settings = get_settings()
    payload = {
        "status": "ok",
        "env": settings.caios_env,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if deep:
        try:
            from app.jobs.queue import PostgresJobQueue
            queue = PostgresJobQueue(settings)
            payload["db"] = "ok"
            payload["jobs"] = queue.counts()
            queue.close()
        except Exception as exc:  # noqa: BLE001
            payload.update({"status": "degraded", "db": f"error: {exc}"})
    return payload
