"""Job runner — executes a bounded batch within a wall-clock budget.

This is where the free-tier hosting strategy lives in code: cron-job.org calls
POST /jobs/run every few minutes; each call runs ONE bounded batch and returns
inside the ~30 s HTTP timeout (docs/01-hosting-blueprint.md §4.4). Jobs that
cannot fit the budget are chunked by the workflow engine or routed to GitHub
Actions via /maintenance/tick — never by silently overrunning.

Retry/timeout/dead-letter semantics (TV-008/TV-016) are properties of the
runner + queue, not of any hosting provider.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.config import Settings
from app.jobs.queue import Job, PostgresJobQueue

logger = logging.getLogger("caios.jobs")

Handler = Callable[[Job], Awaitable[dict | None]]


@dataclass
class BatchReport:
    claimed: int = 0
    done: int = 0
    failed: int = 0
    requeued: int = 0
    budget_exceeded: bool = False
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "claimed": self.claimed, "done": self.done, "failed": self.failed,
            "requeued": self.requeued, "budget_exceeded": self.budget_exceeded,
            "details": self.details,
        }


async def _handle_echo(job: Job) -> dict | None:
    """Built-in demo handler: proves the queue end-to-end in dev/tests."""
    return {"echo": job.payload, "attempts": job.attempts}


def default_registry(queue: PostgresJobQueue | None = None) -> dict[str, Handler]:
    """Kind → handler map. Handlers receive the runner-owned queue so pipelines
    can chain follow-up jobs without opening extra pools. Specialist-agent
    handlers (Phases 5–6) register here; nothing else in the codebase needs to
    know how a job is executed."""
    from app.knowledge.handlers import default_knowledge_handlers
    from app.coordination.handlers import default_coordination_handlers

    registry: dict[str, Handler] = {
        "demo.echo": _handle_echo,
        **default_knowledge_handlers(queue),     # Phase 2: knowledge.extract / knowledge.embed
        **default_coordination_handlers(queue),  # Phase 4: assignment.step (workflow engine)
        # TODO(Phase 6): "report.generate"    — deliverable generation
    }
    return registry


async def run_batch(
    settings: Settings,
    queue: PostgresJobQueue,
    *,
    registry: dict[str, Handler] | None = None,
    batch_size: int | None = None,
    time_budget_seconds: int | None = None,
) -> BatchReport:
    handlers = registry if registry is not None else default_registry(queue=queue)
    limit = batch_size or settings.job_batch_size
    budget = time_budget_seconds or settings.job_time_budget_seconds
    worker_id = f"runner-{uuid.uuid4().hex[:8]}"
    deadline = time.monotonic() + budget
    report = BatchReport()

    jobs = queue.claim_batch(worker_id, limit)
    report.claimed = len(jobs)

    for job in jobs:
        if time.monotonic() >= deadline:
            # Out of budget: release as pending immediately (short backoff) so
            # the next tick picks it up — the job is never abandoned.
            queue.fail(job.id, "runner time budget exceeded (requeued)", backoff_seconds=5)
            report.requeued += 1
            report.budget_exceeded = True
            continue

        handler = handlers.get(job.kind)
        if handler is None:
            queue.fail(job.id, f"no handler registered for kind '{job.kind}'")
            report.failed += 1
            report.details.append({"id": str(job.id), "kind": job.kind,
                                   "status": "dead", "reason": "unregistered"})
            continue

        try:
            import asyncio
            result = await asyncio.wait_for(
                handler(job), timeout=max(1.0, deadline - time.monotonic())
            )
            queue.complete(job.id, result or {})
            report.done += 1
            report.details.append({"id": str(job.id), "kind": job.kind, "status": "done"})
        except TimeoutError:
            queue.fail(job.id, "handler timeout", backoff_seconds=30)
            report.failed += 1
            report.details.append({"id": str(job.id), "kind": job.kind,
                                   "status": "retry/dead", "reason": "timeout"})
        except Exception as exc:  # noqa: BLE001 — jobs must never kill the runner
            logger.exception("job %s failed", job.id)
            queue.fail(job.id, f"{type(exc).__name__}: {exc}", backoff_seconds=30)
            report.failed += 1
            report.details.append({"id": str(job.id), "kind": job.kind,
                                   "status": "retry/dead", "reason": str(exc)[:200]})

    return report
