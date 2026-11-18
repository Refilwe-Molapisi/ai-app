"""CAIOS Phase 5 unit tests — no database required.

Covers: agent registry self-validation and rejection matrix (§11 definitions
complete: responsibilities, tools, outputs, standards, constraints, grounding
block), summary shape, executor/step-kind agreement, planning-time agent-slug
validation (unknown agent → fast 422), and the /agents API contract without a
DB. Run from backend/:  python3 tests/test_phase5.py
"""

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("CAIOS_ENV", "development")
os.environ.setdefault("JOB_RUN_SECRET", "test-secret")

failures: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── 1. Agent registry (§11.1–11.3) ───────────────────────────────────────────
from app.agents.definitions import (  # noqa: E402
    AGENTS, CONTEXT_PLACEHOLDER, AgentDef, UnknownAgent, agents_summary,
    get_agent, validate_definition,
)

for slug in AGENTS:
    validate_definition(AGENTS[slug])
check("all registered agent definitions valid", True)
check("exactly the three §11 division agents", sorted(AGENTS) ==
      ["bi-analytics", "commercial-intelligence", "risk-controls"], str(sorted(AGENTS)))

for slug, must in [("bi-analytics", "alter original client accounting records"),
                   ("commercial-intelligence", "invent sources"),
                   ("risk-controls", "legal opinions")]:
    agent = get_agent(slug)
    text = " ".join(agent.constraints) + " " + agent.default_instructions
    check(f"{slug}: §11 constraint present", must in text, text[:200])

for slug in AGENTS:
    a = get_agent(slug)
    check(f"{slug}: grounding block in instructions", CONTEXT_PLACEHOLDER in a.default_instructions)
    check(f"{slug}: prompt name versioned", a.prompt_name == f"agent.{slug}")

try:
    get_agent("nope")
    check("unknown agent rejected", False)
except UnknownAgent:
    check("unknown agent rejected", True)

for bad, why in [
    (AgentDef("Bad Slug", "", "", purpose="p", responsibilities=("r",),
              permitted_tools=("t",), required_outputs=("o",),
              quality_standards=("q",), constraints=("c",),
              default_instructions="{{context}}"), "slug not kebab-lowercase"),
    (AgentDef("ok", "", "", purpose="p", responsibilities=("r",),
              permitted_tools=(), required_outputs=("o",),
              quality_standards=("q",), constraints=("c",),
              default_instructions="{{context}}"), "empty permitted tools"),
    (AgentDef("ok", "", "", purpose="p", responsibilities=("r",),
              permitted_tools=("t",), required_outputs=("o",),
              quality_standards=("q",), constraints=("c",),
              default_instructions="no grounding"), "instructions lack {{context}}"),
    (AgentDef("ok", "", "", purpose="p", responsibilities=(),
              permitted_tools=("t",), required_outputs=("o",),
              quality_standards=("q",), constraints=("c",),
              default_instructions="{{context}}"), "empty responsibilities"),
]:
    try:
        validate_definition(bad)
        check(f"rejects: {why}", False, f"accepted {bad.slug}")
    except ValueError:
        check(f"rejects: {why}", True)

summary = agents_summary()
check("agents summary shape",
      {a["slug"] for a in summary} == set(AGENTS)
      and all({"responsibilities", "permitted_tools", "required_outputs",
               "quality_standards", "constraints"} <= set(a) for a in summary))

# ── 2. Coordination integration: kinds, executors, planning-time validation ──
from app.coordination.workflows import WORKFLOWS, STEP_KINDS  # noqa: E402
from app.coordination.handlers import _EXECUTORS  # noqa: E402
from app.coordination.engine import InvalidWorkflowInput, plan_assignment  # noqa: E402

check("agent.task is a registered kind+executor",
      "agent.task" in STEP_KINDS and "agent.task" in _EXECUTORS)
check("every kind has an executor", set(_EXECUTORS) == set(STEP_KINDS))

agent_workflows = [w for w in WORKFLOWS.values()
                   if any(s.kind == "agent.task" for s in w.steps)]
check("three agent workflows registered", len(agent_workflows) == 3,
      str([w.name for w in agent_workflows]))
slugs_in_workflows = {s.params["agent"] for w in agent_workflows
                      for s in w.steps if s.kind == "agent.task"}
check("agent workflows reference registered agents only",
      slugs_in_workflows == set(AGENTS), str(slugs_in_workflows))

_, steps = plan_assignment("client.competitor-scan", {"subject": "Maize milling in Limpopo"})
research = steps[0]
check("agent step planned concrete",
      research["params"].get("agent") == "commercial-intelligence"
      and "Maize milling in Limpopo" in research["params"].get("question", "")
      and "question_template" not in research["params"], str(research)[:220])

_, steps = plan_assignment("company.bi-diagnostic", {"topic": "Revenue trends"})
check("bi-diagnostic: kb.check before agent, trailing gate",
      [s["kind"] for s in steps] == ["kb.check", "agent.task", "gate.review"])

try:
    plan_assignment("client.competitor-scan", {})
    check("agent workflow rejects missing input", False)
except InvalidWorkflowInput:
    check("agent workflow rejects missing input", True)

# Temporary workflow with an unregistered agent slug → planning-time 422.
from app.coordination.workflows import StepSpec, WorkflowDef  # noqa: E402
WORKFLOWS["test.bad-agent"] = WorkflowDef(
    "test.bad-agent", "", required_inputs=(),
    steps=(StepSpec("s", "agent.task", params={"agent": "ghost-agent"}),))
try:
    plan_assignment("test.bad-agent", {})
    check("planning rejects unregistered agent slug", False)
except InvalidWorkflowInput as exc:
    check("planning rejects unregistered agent slug", True, str(exc))
finally:
    del WORKFLOWS["test.bad-agent"]

# ── 3. run_agent error path (no DB needed for slug resolution order) ─────────
import asyncio  # noqa: E402
from app.agents.service import run_agent  # noqa: E402

try:
    asyncio.run(run_agent(agent_slug="ghost", question="q", org_id="org"))
    check("run_agent rejects unknown slug", False)
except ValueError as exc:
    check("run_agent rejects unknown slug", True, str(exc)[:100])

# ── 4. API contract without DB ───────────────────────────────────────────────
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
paths = client.get("/openapi.json").json()["paths"]
check("openapi exposes /agents", "/agents" in paths)
check("no ad-hoc agent-run endpoint exists (coordinator-only entry, §11.4)",
      not any("/agents/" in p and p != "/agents" for p in paths), str(paths))
r = client.get("/agents")
check("agents without token -> 401", r.status_code == 401, str(r.status_code))

print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(1 if failures else 0)
