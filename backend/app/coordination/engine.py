"""Coordination engine — turns a workflow definition into a running assignment
and walks it forward, one governed transition at a time (§11, §12).

State machine (assignment):  running → awaiting_approval ⇄ running → completed
                             running/awaiting_approval → cancelled | failed
State machine (step):        pending → queued → running → done | failed
                             gate steps: pending → awaiting_approval → done

Design decisions worth recording:
- Input templating happens ONCE, at creation: a step's question_template is
  rendered against the assignment inputs and stored concrete. A bad placeholder
  is a 422 to the API caller — never a runtime surprise five jobs deep.
- A step failure fails the assignment immediately (fail-fast) AND re-raises so
  the queue's retry bookkeeping still applies; a retried job re-enters the
  handler, sees the terminal state and exits as a no-op skip. Transient
  resilience comes from re-creating the assignment (a `retry` endpoint is a
  Phase 5+ candidate), not from silently retrying LLM spend.
- Gates pause BEFORE their position is consumed: the gate step sits in
  awaiting_approval until POST /assignments/{id}/approve — approval consumes
  the gate and enqueues whatever follows.
"""

import logging
import uuid

from app.core.config import Settings, get_settings
from app.coordination.store import CoordinationStore
from app.coordination.workflows import StepSpec, UnknownWorkflow, get_workflow

logger = logging.getLogger("caios.coordination")


class InvalidWorkflowInput(ValueError):
    """Creation-time validation failure — 422 at the route layer."""


def plan_assignment(workflow: str, inputs: dict | None) -> tuple[object, list[dict]]:
    """Pure creation-time validation + planning — no database, no pools.
    Resolves the workflow definition, checks required inputs, and renders step
    templates to CONCRETE params (a bad placeholder is a 422 here, never a
    runtime surprise five jobs deep). Returns (definition, finalised steps)."""
    try:
        wf = get_workflow(workflow)
    except UnknownWorkflow as exc:
        raise InvalidWorkflowInput(str(exc)) from exc

    inputs = dict(inputs or {})
    missing = [k for k in wf.required_inputs if k not in inputs or inputs[k] in (None, "")]
    if missing:
        raise InvalidWorkflowInput(
            f"workflow '{workflow}' requires inputs: {missing} (got {sorted(inputs)})")

    steps: list[dict] = []
    for i, spec in enumerate(wf.steps, start=1):
        params = dict(spec.params)
        if "question_template" in params:
            try:
                params["question"] = str(params.pop("question_template")).format(
                    **{k: ("" if v is None else str(v)) for k, v in inputs.items()})
            except (KeyError, IndexError) as exc:
                raise InvalidWorkflowInput(
                    f"step '{spec.name}' question template references an input "
                    f"that was not provided: {exc}") from exc
        steps.append({"seq": i, "name": spec.name, "kind": spec.kind,
                      "params": params, "is_gate": spec.is_gate})
    return wf, steps


def start_assignment(
    settings: Settings,
    queue,
    *,
    org_id: str,
    workflow: str,
    title: str | None = None,
    engagement_id: str | None = None,
    inputs: dict | None = None,
    created_by: str | None = None,
) -> dict:
    """Validate the workflow + inputs, materialise concrete steps, persist the
    assignment and enqueue its first executable step."""
    _, steps = plan_assignment(workflow, inputs)

    store = CoordinationStore(settings)
    try:
        assignment = store.create_assignment(
            org_id=org_id, workflow=workflow, title=title,
            engagement_id=engagement_id, inputs=inputs,
            created_by=created_by, steps=steps,
        )
        _advance(store, queue, uuid.UUID(assignment["id"]))
        detail = store.get_assignment(uuid.UUID(assignment["id"]), org_id=org_id)
    finally:
        store.close()
    return detail


def _advance(store: CoordinationStore, queue, assignment_id: uuid.UUID) -> None:
    """Walk the assignment forward from its current step. Called after
    creation, after each step completion and after gate approval."""
    step = store.next_actionable_step(assignment_id)
    if step is None:
        store.set_status(assignment_id, status="completed", completed=True)
        store.append_event(assignment_id, "assignment.completed",
                           detail={"outcome": "all steps resolved"})
        logger.info("assignment %s completed", assignment_id)
        return

    if step["is_gate"]:
        store.update_step(assignment_id, step["seq"], status="awaiting_approval")
        store.set_status(assignment_id, status="awaiting_approval")
        store.append_event(assignment_id, "assignment.awaiting_approval",
                           detail={"step": step["name"], "seq": step["seq"],
                                   "how": "POST /assignments/{id}/approve"})
        return

    # Automatic step: enqueue one 'assignment.step' job (budget-bounded by the
    # runner like every other job — ADR-009 / TV-008).
    job_id = queue.enqueue(
        "assignment.step",
        {"assignment_id": str(assignment_id), "step_seq": step["seq"],
         "trigger": "workflow"},
        priority=3,
    )
    store.update_step(assignment_id, step["seq"], status="queued", job_id=job_id)
    store.append_event(assignment_id, "step.queued",
                       detail={"step": step["name"], "seq": step["seq"],
                               "job_id": str(job_id)})


def approve_gate(settings: Settings, queue, *, assignment_id: uuid.UUID,
                 org_id: str, actor: str, note: str | None = None) -> dict:
    """Consume the awaiting gate: gate → done, assignment → running, next step
    enqueued (or assignment completed). 409-class error if no gate is open."""
    store = CoordinationStore(settings)
    try:
        assignment = store.get_assignment(assignment_id, org_id=org_id)
        if assignment is None:
            raise LookupError("assignment not found in this organisation")
        if assignment["status"] != "awaiting_approval":
            raise PermissionError(
                f"assignment is '{assignment['status']}'; nothing to approve")
        gate = store.next_actionable_step(assignment_id)
        if gate is None or not gate.get("is_gate"):
            raise PermissionError("no open gate step on this assignment")

        store.update_step(assignment_id, gate["seq"], status="done",
                          output={"approved_by": actor, "note": (note or "")[:500]},
                          mark_finished=True)
        store.append_event(assignment_id, "gate.approved", actor=actor,
                           detail={"step": gate["name"], "seq": gate["seq"],
                                   "note": (note or "")[:200] or None})
        store.set_status(assignment_id, status="running")
        _advance(store, queue, assignment_id)
        return store.get_assignment(assignment_id, org_id=org_id)
    finally:
        store.close()


def cancel_assignment(settings: Settings, queue, *, assignment_id: uuid.UUID,
                      org_id: str, actor: str, reason: str | None = None) -> dict:
    """Cancel a non-terminal assignment: stop the pending step job (if it has
    not started), mark open steps cancelled, log the event. Terminal
    assignments raise PermissionError (409 at the route layer)."""
    store = CoordinationStore(settings)
    try:
        assignment = store.get_assignment(assignment_id, org_id=org_id)
        if assignment is None:
            raise LookupError("assignment not found in this organisation")
        if assignment["status"] in ("completed", "cancelled", "failed"):
            raise PermissionError(
                f"assignment is already '{assignment['status']}' and cannot be cancelled")

        for step in assignment["steps"]:
            if step["status"] == "queued" and step.get("job_id"):
                try:
                    queue.cancel(uuid.UUID(step["job_id"]))  # only cancels pending jobs
                except Exception:  # noqa: BLE001 — cancel is best-effort; the
                    pass                                 # handler's terminal-state guard
                                                         # makes a racing job harmless
        store.cancel_open_steps(assignment_id)
        store.set_status(assignment_id, status="cancelled")
        store.append_event(assignment_id, "assignment.cancelled", actor=actor,
                           detail={"reason": (reason or "")[:300] or None})
        return store.get_assignment(assignment_id, org_id=org_id)
    finally:
        store.close()


def fail_step(settings: Settings, assignment_id: uuid.UUID, seq: int,
              error: str, *, actor: str = "system") -> None:
    """Fail-fast step handling (see module docstring): the step and the
    assignment both terminate with the error preserved for inspection."""
    store = CoordinationStore(settings)
    try:
        store.update_step(assignment_id, seq, status="failed",
                          last_error=error[:500], mark_finished=True)
        store.set_status(assignment_id, status="failed")
        store.append_event(assignment_id, "assignment.failed",
                           detail={"step_seq": seq, "error": error[:300]})
    finally:
        store.close()
    logger.error("assignment %s step %d failed: %s", assignment_id, seq, error[:200])
