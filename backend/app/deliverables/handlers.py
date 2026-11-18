"""Phase 6 job handler — `report.generate` (CAIOS-DEL-001).

    report.generate   spec → bytes → storage   (status: queued → generating → ready | failed)

One queue job like any other, executed inside the /jobs/run budget. Failure
semantics mirror the knowledge pipeline: the deliverable row is marked `failed`
with the honest reason AND the job fails, so the queue's retry policy (TV-016)
applies — a transient storage outage retries instead of stranding the row.

Adapters resolve lazily via _storage() so tests can substitute fakes the same
way the knowledge handlers do.
"""

import logging
import uuid

from app.core.config import Settings, get_settings
from app.deliverables.generate import build_deliverable, checksum
from app.deliverables.store import DeliverableStore
from app.jobs.queue import PostgresJobQueue
from app.jobs.runner import Handler, Job

logger = logging.getLogger("caios.deliverables")


def _store(settings: Settings | None = None) -> DeliverableStore:
    return DeliverableStore(settings or get_settings())


def _storage():
    from app.adapters.storage_adapter import get_storage_adapter
    return get_storage_adapter(get_settings())


def deliverable_key(org_id: str, scope: str, engagement_id: str | None,
                    did: uuid.UUID, ext: str) -> str:
    """Governance-recognised prefixes (§7.4): client-scope artefacts live under
    the client/engagement prefix; company artefacts under company-knowledge."""
    if scope == "client":
        return f"clients/{org_id}/{engagement_id or 'general'}/deliverables/{did}.{ext}"
    return f"company-knowledge/deliverables/{org_id}/{did}.{ext}"


def make_generate_handler(queue: PostgresJobQueue | None = None) -> Handler:
    async def handle_generate(job: Job) -> dict:
        raw = (job.payload or {}).get("deliverable_id")
        try:
            did = uuid.UUID(str(raw))
        except (ValueError, AttributeError):
            raise ValueError(f"job 'report.generate' requires a valid 'deliverable_id' payload (got {raw!r})")

        store = _store()
        try:
            row = store.get(did, org_id=None)  # worker path: job payload already carries the org
            if row is None:
                raise ValueError(f"deliverable {did} not found")
            if row["status"] == "ready":
                return {"deliverable_id": str(did), "status": "ready", "skipped": True}

            store.update(did, status="generating", last_error=None)
            store.append_event(did, "deliverable.generating", actor="worker")
            try:
                data, mime, ext = build_deliverable(row["kind"], row["title"], row["spec"])
                key = deliverable_key(row["org_id"], row["scope"], row["engagement_id"], did, ext)
                _storage().put(key, data, content_type=mime)
            except Exception as exc:  # noqa: BLE001 — mark failed, then re-raise for queue retry
                message = f"generation failed: {type(exc).__name__}: {exc}"
                store.update(did, status="failed", last_error=message[:500])
                store.append_event(did, "deliverable.failed", actor="worker",
                                   detail=message[:300])
                raise
            store.update(did, status="ready", storage_key=key, mime_type=mime,
                         size_bytes=len(data), checksum_sha256=checksum(data),
                         last_error=None)
            store.append_event(did, "deliverable.ready", actor="worker",
                               detail=f"{len(data)} bytes · sha256 {checksum(data)[:12]}…")
        finally:
            store.close()
        logger.info("deliverable %s ready: %d bytes (%s)", did, len(data), mime)
        return {"deliverable_id": str(did), "status": "ready", "bytes": len(data),
                "mime": mime, "storage_key": key}
    return handle_generate


def default_deliverable_handlers(queue: PostgresJobQueue | None = None) -> dict[str, Handler]:
    return {"report.generate": make_generate_handler(queue)}
