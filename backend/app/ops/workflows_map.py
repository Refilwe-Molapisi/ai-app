"""Ops division configuration (CAIOS-OPS-001).

The single place where §11 divisions and the workflow→division allocation
live. The unit test suite asserts this map covers every workflow in the
coordination registry, so the two registries cannot drift apart silently.
"""

from __future__ import annotations

# The four desks of the company (§11): three delivery divisions served by
# the specialist agents, plus the Governance & Operations desk (MD/company
# secretary function) that runs the non-agent workflows and the gates.
DIVISIONS = (
    "BI & Analytics",
    "Commercial Intelligence",
    "Risk & Controls",
    "Governance & Operations",
)

# Workflow allocation per §11: agent-executed workflows belong to their
# agent's division; workflows without an agent step (knowledge briefs,
# onboarding checks) belong to the Governance & Operations desk.
WORKFLOW_DIVISIONS: dict[str, str] = {
    "company.knowledge-brief": "Governance & Operations",
    "client.onboarding-check": "Governance & Operations",
    "company.bi-diagnostic": "BI & Analytics",
    "client.competitor-scan": "Commercial Intelligence",
    "client.controls-review": "Risk & Controls",
}


def division_of_workflow(workflow: str) -> str:
    """Division for a workflow key; unknown workflows are Governance & Operations
    work until the map is updated — never a silent misattribution."""
    return WORKFLOW_DIVISIONS.get(workflow, "Governance & Operations")
