"""Workflow registry — declarative definitions, validated at registration
time (§11). A workflow is an ordered list of step specs; the engine materialises
concrete steps from it when an assignment starts. Adding a workflow = adding
an entry here — no engine changes, matching ADR-006's provider-selection
philosophy ('add adapters here, nowhere else').

Step kinds (executors registered in app/coordination/handlers.py):
    ai.brief      grounded knowledge-base answer → step output (Phase 3 service)
    kb.check      knowledge-base readiness check (stats vs min_chunks)
    gate.review   human approval gate — advanced via POST /assignments/{id}/approve

Rules enforced here:
    - steps non-empty; kinds must be registered executors;
    - at most one trailing gate; nothing after a gate (a gate is a checkpoint,
      not a mid-pipeline pause — keeps the state machine inspectable).
"""

from dataclasses import dataclass, field


class UnknownWorkflow(KeyError):
    pass


@dataclass(frozen=True)
class StepSpec:
    name: str
    kind: str
    is_gate: bool = False
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowDef:
    name: str
    description: str
    required_inputs: tuple[str, ...] = ()
    steps: tuple[StepSpec, ...] = ()


WORKFLOWS: dict[str, WorkflowDef] = {
    "company.knowledge-brief": WorkflowDef(
        name="company.knowledge-brief",
        description="Produce a knowledge-grounded brief on a topic from the "
                    "company knowledge base, then hold it for human review.",
        required_inputs=("topic",),
        steps=(
            StepSpec(name="brief", kind="ai.brief",
                     params={"question_template": "Provide a knowledge-grounded "
                             "brief on: {topic}. Lead with the answer, cite sources."}),
            StepSpec(name="review", kind="gate.review", is_gate=True),
        ),
    ),
    "client.onboarding-check": WorkflowDef(
        name="client.onboarding-check",
        description="Verify the client's knowledge base is indexed and ready "
                    "for AI work, then hold for the operator's go-ahead.",
        steps=(
            StepSpec(name="index-check", kind="kb.check",
                     params={"min_chunks": 1}),
            StepSpec(name="review", kind="gate.review", is_gate=True),
        ),
    ),
}


def get_workflow(name: str) -> WorkflowDef:
    wf = WORKFLOWS.get(name)
    if wf is None:
        raise UnknownWorkflow(
            f"unknown workflow '{name}'; available: {sorted(WORKFLOWS)}")
    return wf


def validate_definition(wf: WorkflowDef) -> None:
    """Registry self-check (also run in unit tests — a broken definition must
    fail loudly at import/test time, not when a client creates an assignment)."""
    if not wf.steps:
        raise ValueError(f"workflow '{wf.name}' has no steps")
    for step in wf.steps:
        if step.kind not in STEP_KINDS:
            raise ValueError(f"workflow '{wf.name}' step '{step.name}' has "
                             f"unregistered kind '{step.kind}'")
    gate_positions = [i for i, s in enumerate(wf.steps) if s.is_gate]
    if gate_positions and (len(gate_positions) > 1 or gate_positions[0] != len(wf.steps) - 1):
        raise ValueError(f"workflow '{wf.name}': at most one trailing gate step is allowed")


# Executor keys — kept in one place so both the registry self-check and the
# handler dispatch table agree (handlers.py builds the actual callables).
STEP_KINDS = ("ai.brief", "kb.check", "gate.review")


def registry_summary() -> list[dict]:
    return [
        {
            "name": wf.name,
            "description": wf.description,
            "required_inputs": list(wf.required_inputs),
            "steps": [
                {"seq": i + 1, "name": s.name, "kind": s.kind, "is_gate": s.is_gate}
                for i, s in enumerate(wf.steps)
            ],
        }
        for wf in WORKFLOWS.values()
    ]


for _wf in WORKFLOWS.values():
    validate_definition(_wf)
