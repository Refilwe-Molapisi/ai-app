"""Phase 4 job handlers — the workflow step executor (§11, §12).

    assignment.step    executes ONE assignment step, then advances the workflow

Registered under kind 'assignment.step'; the payload identifies the step:
    {"assignment_id": "...", "step_seq": 2, "trigger": "workflow"}

Entry guards make retries and races harmless (the queue may re-attempt after
backoff, and /cancel may race a queued job):
    - assignment terminal (completed/cancelled/failed) → step skipped, job done;
    - step already done/failed/cancelled → skip;
    - a racing 'cancel' that lost the cancellation (job already claimed) sees
      the terminal assignment and exits cleanly.

Step executors:
    ai.brief     shared AI service (grounded_answer) — retrieval, active prompt
                 template, model call, §20 ledger row; output = answer +
                 citations + usage, stored on the step.
    kb.check     knowledge-base readiness: org index stats vs min_chunks;
                 below minimum ⇒ step fails (honest 422-class signal upstream).
    gate.review  never enqueued — a bug if it is; refused loudly.
"""

import logging
import uuid

from app.core.config import Settings, get_settings
from app.jobs.queue import PostgresJobQueue
from app.jobs.runner import Handler, Job

logger = logging.getLogger("caios.coordination")


def _transient_queue(settings: Settings | None = None) -> PostgresJobQueue:
    """Fallback for direct handler invocation without a runner-owned queue."""
    return PostgresJobQueue(settings or get_settings())


async def _exec_ai_brief(assignment: dict, step: dict, settings) -> dict:
    from app.ai.service import grounded_answer

    question = (step.get("params") or {}).get("question") \
        or str(assignment.get("inputs") or {}).get("topic", "")
    if not question:
        raise ValueError("ai.brief step has no question (params.question missing)")
    result = await grounded_answer(
        question=question, org_id=assignment["org_id"],
        principal_label=f"assignment:{assignment['id']}",
        engagement_id=assignment.get("engagement_id"),
        settings=settings,
    )
    return {
        "question": question,
        "answer": result["answer"],
        "grounded": result["grounded"],
        "citations": result["citations"],
        "usage": result["usage"],
        "retrieval": {"chunks_in_index": result["retrieval"]["chunks_in_index"]},
    }


async def _exec_kb_check(assignment: dict, step: dict, settings) -> dict:
    from app.knowledge.store import KnowledgeStore

    min_chunks = int((step.get("params") or {}).get("min_chunks", 1))
    store = KnowledgeStore(settings)
    try:
        stats = store.stats(assignment["org_id"])
    finally:
        store.close()
    if stats["chunks"] < min_chunks:
        raise ValueError(
            f"knowledge base below minimum: {stats['chunks']} chunks indexed, "
            f"{min_chunks} required — upload and index documents first")
    return {"stats": stats, "min_chunks": min_chunks, "verdict": "ready"}


async def _exec_gate_review(assignment: dict, step: dict, settings) -> dict:
    raise ValueError("gate.review steps are advanced via "
                     "POST /assignments/{id}/approve, never as queue jobs")


_EXECUTORS = {
    "ai.brief": _exec_ai_brief,
    "kb.check": _exec_kb_check,
    "gate.review": _exec_gate_review,
}


def make_step_handler(queue: PostgresJobQueue | None = None) -> Handler:
    async def handle_step(job: Job) -> dict:
        settings = get_settings()
        payload = job.payload or {}
        try:
            assignment_id = uuid.UUID(str(payload.get("assignment_id")))
            step_seq = int(payload.get("step_seq"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"job '{job.kind}' requires a valid "
                             f"'assignment_id' and 'step_seq' payload") from exc

        from app.coordination import engine
        from app.coordination.store import CoordinationStore

        store = CoordinationStore(settings)
        try:
            assignment_row = store.get_assignment(assignment_id, org_id=None)
        finally:
            store.close()
        if assignment_row is None:
            raise ValueError(f"assignment {assignment_id} not found")

        # Terminal-state guards: retries and cancel races exit as skips.
        if assignment_row["status"] in ("completed", "cancelled", "failed"):
            store = CoordinationStore(settings)
            try:
                step = store.get_step(assignment_id, step_seq)
                if step and step["status"] not in ("done", "failed", "cancelled", "skipped"):
                    store.update_step(assignment_id, step_seq, status="skipped",
                                      last_error=f"assignment {assignment_row['status']}",
                                      mark_finished=True)
            finally:
                store.close()
            return {"skipped": True,
                    "reason": f"assignment {assignment_row['status']}"}

        step = next((s for s in assignment_row["steps"] if s["seq"] == step_seq), None)
        if step is None:
            raise ValueError(f"assignment {assignment_id} has no step {step_seq}")
        if step["status"] in ("done", "failed", "cancelled", "skipped"):
            return {"skipped": True, "reason": f"step already '{step['status']}'"}

        store = CoordinationStore(settings)
        try:
            store.update_step(assignment_id, step_seq, status="running",
                              mark_started=True)
            store.append_event(assignment_id, "step.started",
                               detail={"step": step["name"], "seq": step_seq})
        finally:
            store.close()

        executor = _EXECUTORS.get(step["kind"])
        if executor is None:
            error = f"no executor registered for step kind '{step['kind']}'"
            engine.fail_step(settings, assignment_id, step_seq, error)
            raise ValueError(error)

        try:
            output = await executor(assignment_row, step, settings)
        except Exception as exc:  # noqa: BLE001 — fail-fast assignment, then re-raise
            engine.fail_step(settings, assignment_id, step_seq,
                             f"{type(exc).__name__}: {exc}")
            raise

        store = CoordinationStore(settings)
        try:
            store.update_step(assignment_id, step_seq, status="done",
                              output=output, mark_finished=True)
            store.append_event(assignment_id, "step.completed",
                               detail={"step": step["name"], "seq": step_seq,
                                       "summary": _summarise(output)})
            engine._advance(store, queue if queue is not None else _transient_queue(settings),
                            assignment_id)
        finally:
            store.close()
        return {"assignment_id": str(assignment_id), "step_seq": step_seq,
                "status": "done"}

    return handle_step


def _summarise(output: dict | None) -> dict:
    """Small, log/event-friendly slice of a step output (never the full answer)."""
    if not isinstance(output, dict):
        return {}
    summary = {}
    if "answer" in output:
        summary["answer_chars"] = len(output.get("answer") or "")
        summary["grounded"] = output.get("grounded")
        summary["citations"] = len(output.get("citations") or [])
    if "usage" in output:
        summary["cost_usd"] = (output.get("usage") or {}).get("estimated_cost_usd")
    if "verdict" in output:
        summary["verdict"] = output.get("verdict")
    return summary


def default_coordination_handlers(queue: PostgresJobQueue | None = None) -> dict[str, Handler]:
    return {"assignment.step": make_step_handler(queue)}
