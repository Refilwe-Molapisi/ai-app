"""Phase 6/7 job handler — `report.generate` (CAIOS-DEL-001, CAIOS-QA-001).

    report.generate   spec → bytes → storage   (status: queued → generating → ready | failed)

One queue job like any other, executed inside the /jobs/run budget. Failure
semantics mirror the knowledge pipeline: the deliverable row is marked `failed`
with the honest reason AND the job fails, so the queue's retry policy (TV-016)
applies — a transient storage outage retries instead of stranding the row.

Phase 7 additions:
- storage keys are versioned ({id}.v{n}.{ext}) so a revision never overwrites
  a superseded artefact — history stays downloadable;
- every successful build records an immutable row in deliverable_versions;
- a regenerate that changes the checksum of an APPROVED artefact resets the
  QA gate to 'draft' with a qa.reset event — deterministic specs normally
  re-render byte-identically (checksum match proves it), so approval survives;
  drift never slips through silently.

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
                    did: uuid.UUID, version: int, ext: str) -> str:
    """Governance-recognised prefixes (§7.4): client-scope artefacts live under
    the client/engagement prefix; company artefacts under company-knowledge.
    The artefact version is part of the key — revisions never clobber history."""
    if scope == "client":
        return f"clients/{org_id}/{engagement_id or 'general'}/deliverables/{did}.v{version}.{ext}"
    return f"company-knowledge/deliverables/{org_id}/{did}.v{version}.{ext}"


def make_generate_handler(queue: PostgresJobQueue | None = None) -> Handler:
    async def handle_generate(job: Job) -> dict:
        payload = job.payload or {}
        raw = payload.get("deliverable_id")
        force = bool(payload.get("force"))
        try:
            did = uuid.UUID(str(raw))
        except (ValueError, AttributeError):
            raise ValueError(f"job 'report.generate' requires a valid 'deliverable_id' payload (got {raw!r})")

        store = _store()
        try:
            row = store.get(did, org_id=None)  # worker path: job payload already carries the org
            if row is None:
                raise ValueError(f"deliverable {did} not found")
            if row["status"] == "ready" and not force:
                return {"deliverable_id": str(did), "status": "ready", "skipped": True}
            previous_checksum = row["checksum_sha256"] if row["status"] == "ready" else None

            store.update(did, status="generating", last_error=None)
            store.append_event(did, "deliverable.generating", actor="worker",
                               detail=f"v{row['spec_version']}"
                                      + (" (regenerate)" if force else ""))
            try:
                data, mime, ext = build_deliverable(row["kind"], row["title"], row["spec"])
                key = deliverable_key(row["org_id"], row["scope"], row["engagement_id"],
                                      did, row["spec_version"], ext)
                _storage().put(key, data, content_type=mime)
            except Exception as exc:  # noqa: BLE001 — mark failed, then re-raise for queue retry
                message = f"generation failed: {type(exc).__name__}: {exc}"
                store.update(did, status="failed", last_error=message[:500])
                store.append_event(did, "deliverable.failed", actor="worker",
                                   detail=message[:300])
                raise
            new_checksum = checksum(data)
            store.update(did, status="ready", storage_key=key, mime_type=mime,
                         size_bytes=len(data), checksum_sha256=new_checksum,
                         last_error=None)
            store.record_version(did, version=row["spec_version"], spec=row["spec"],
                                 storage_key=key, mime_type=mime, size_bytes=len(data),
                                 checksum_sha256=new_checksum, qa_state=row["qa_state"],
                                 qa_checks=row["qa_checks"])
            detail = (f"v{row['spec_version']} · {len(data)} bytes · "
                      f"sha256 {new_checksum[:12]}…")
            if (previous_checksum is not None and previous_checksum != new_checksum
                    and row["qa_state"] == "approved"):
                # Approved bytes changed under us: the approval is void.
                store.set_qa(did, qa_state="draft", reviewer="system",
                             comment="checksum changed after re-render — QA approval reset")
                store.append_event(did, "qa.reset", actor="worker",
                                   detail=f"checksum drift {previous_checksum[:8]}… → "
                                          f"{new_checksum[:8]}…; approval voided")
                detail += " · QA approval reset (drift)"
            store.append_event(did, "deliverable.ready", actor="worker", detail=detail)
        finally:
            store.close()
        logger.info("deliverable %s ready: %d bytes (%s)", did, len(data), mime)
        return {"deliverable_id": str(did), "status": "ready", "bytes": len(data),
                "mime": mime, "storage_key": key, "version": row["spec_version"]}
    return handle_generate


def default_deliverable_handlers(queue: PostgresJobQueue | None = None) -> dict[str, Handler]:
    return {"report.generate": make_generate_handler(queue)}
