"""CAIOS Phase 4 unit tests — no database required.

Covers: workflow registry self-validation and rejection matrix, pure
assignment planning (input templating, concrete params), registry summary
shape, and the API contract for coordination routes without a DB (auth
fail-closed, fast 422s, OpenAPI surface).

Run from backend/:  python3 tests/test_phase4.py
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


# ── 1. Registry self-validation (runs at import; make it explicit) ───────────
from app.coordination.workflows import (  # noqa: E402
    STEP_KINDS, WORKFLOWS, StepSpec, UnknownWorkflow, WorkflowDef,
    get_workflow, registry_summary, validate_definition,
)

for name in WORKFLOWS:
    validate_definition(WORKFLOWS[name])
check("all registered definitions valid", True)

wf = get_workflow("company.knowledge-brief")
check("knowledge-brief: 2 steps, trailing gate",
      len(wf.steps) == 2 and wf.steps[-1].is_gate and not wf.steps[0].is_gate)
check("knowledge-brief requires topic", wf.required_inputs == ("topic",))
check("gate executor is a known kind", wf.steps[-1].kind in STEP_KINDS)

try:
    get_workflow("does.not-exist")
    check("unknown workflow rejected", False)
except UnknownWorkflow:
    check("unknown workflow rejected", True)

for bad, why in [
    (WorkflowDef("x", "", steps=()), "empty steps"),
    (WorkflowDef("x", "", steps=(StepSpec("s", "not-a-kind"),)), "unknown kind"),
    (WorkflowDef("x", "", steps=(StepSpec("g", "gate.review", is_gate=True),
                                 StepSpec("s", "kb.check"))), "mid-pipeline gate"),
    (WorkflowDef("x", "", steps=(StepSpec("g", "gate.review", is_gate=True),
                                 StepSpec("g2", "gate.review", is_gate=True))), "two gates"),
]:
    try:
        validate_definition(bad)
        check(f"rejects: {why}", False, f"accepted {bad}")
    except ValueError:
        check(f"rejects: {why}", True)

summary = registry_summary()
check("registry summary shape",
      {w["name"] for w in summary} == set(WORKFLOWS)
      and all(w["steps"][i]["seq"] == i + 1 for w in summary for i in range(len(w["steps"]))))

# ── 2. Pure planning (§11 creation-time validation) ──────────────────────────
from app.coordination.engine import InvalidWorkflowInput, plan_assignment  # noqa: E402

wf, steps = plan_assignment("company.knowledge-brief", {"topic": "VAT registration"})
check("plan renders concrete question",
      steps[0]["params"].get("question") == str(steps[0]["params"]["question"])
      and "VAT registration" in steps[0]["params"]["question"]
      and "question_template" not in steps[0]["params"], str(steps[0]["params"]))
check("plan marks gate", steps[1]["is_gate"] is True and steps[1]["kind"] == "gate.review")
check("plan sequences 1..n", [s["seq"] for s in steps] == [1, 2])

for args, why in [
    (("nope", {}), "unknown workflow"),
    (("company.knowledge-brief", {}), "missing required input"),
    (("company.knowledge-brief", {"topic": ""}), "empty required input"),
]:
    try:
        plan_assignment(*args)
        check(f"plan rejects: {why}", False, f"accepted {args}")
    except InvalidWorkflowInput:
        check(f"plan rejects: {why}", True)

# Bad template placeholder → creation-time rejection (temporary registry entry).
WORKFLOWS["test.broken-template"] = WorkflowDef(
    "test.broken-template", "", required_inputs=(),
    steps=(StepSpec("s", "ai.brief", params={"question_template": "Ask {nope}"}),))
try:
    plan_assignment("test.broken-template", {})
    check("plan rejects: template references unknown input", False)
except InvalidWorkflowInput as exc:
    check("plan rejects: template references unknown input", True, str(exc))
finally:
    del WORKFLOWS["test.broken-template"]

# ── 3. Handler dispatch table agrees with the registry ───────────────────────
from app.coordination.handlers import _EXECUTORS  # noqa: E402

check("every registered kind has an executor", set(_EXECUTORS) == set(STEP_KINDS))

# ── 4. API contract without DB ───────────────────────────────────────────────
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
paths = client.get("/openapi.json").json()["paths"]

for route in ("/workflows", "/assignments", "/assignments/{assignment_id}",
              "/assignments/{assignment_id}/approve", "/assignments/{assignment_id}/cancel"):
    check(f"openapi exposes {route}", route in paths)

r = client.get("/workflows")
check("workflows without token -> 401", r.status_code == 401, str(r.status_code))
r = client.post("/assignments", json={"org_id": "org-a", "workflow": "x"})
check("create without token -> 401", r.status_code == 401, str(r.status_code))
r = client.post("/assignments/x/approve", json={"org_id": "org-a"})
check("approve without token -> 401", r.status_code == 401, str(r.status_code))
r = client.post("/assignments/x/cancel", json={"org_id": "org-a"})
check("cancel without token -> 401", r.status_code == 401, str(r.status_code))

H = {"Authorization": "Bearer dev"}
r = client.post("/assignments", headers=H,
                json={"org_id": "org-a", "workflow": "nope.workflow"})
check("unknown workflow -> 422 (no DB touched)", r.status_code == 422, str(r.status_code))
r = client.post("/assignments", headers=H,
                json={"org_id": "org-a", "workflow": "company.knowledge-brief"})
check("missing inputs -> 422 (no DB touched)", r.status_code == 422, str(r.status_code))
check("422 names the missing input", "topic" in r.json()["detail"], r.json()["detail"])

print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(1 if failures else 0)
