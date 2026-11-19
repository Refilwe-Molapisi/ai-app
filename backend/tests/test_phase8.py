"""CAIOS Phase 8 unit tests — internal ops configuration logic.

No database: the division configuration and its completeness against the
coordination workflow registry are pure data problems. An incomplete map
would misattribute workload on the ops desk, so coverage is asserted.

Run from backend/:  python3 tests/test_phase8.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

failures: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


from app.coordination.workflows import WORKFLOWS  # noqa: E402
from app.ops.workflows_map import (DIVISIONS, WORKFLOW_DIVISIONS,  # noqa: E402
                                   division_of_workflow)
from app.agents.definitions import AGENTS  # noqa: E402

# ── 1. The workflow→division map covers the registry ────────────────────────
registry_keys = set(WORKFLOWS.keys())
check("ops map covers every registry workflow",
      registry_keys.issubset(WORKFLOW_DIVISIONS.keys()),
      f"missing: {sorted(registry_keys - set(WORKFLOW_DIVISIONS))}")
check("ops map has no stale workflows",
      set(WORKFLOW_DIVISIONS.keys()) == registry_keys,
      f"stale: {sorted(set(WORKFLOW_DIVISIONS) - registry_keys)}")

# ── 2. Agent-executed workflows map to their agent's division ───────────────
agent_divisions = {slug: a.division for slug, a in AGENTS.items()}
for key, wf in WORKFLOWS.items():
    step_agents = [s.params.get("agent") for s in wf.steps
                   if s.kind == "agent.task" and s.params.get("agent")]
    if step_agents:
        expected = agent_divisions.get(step_agents[0])
        check(f"{key} maps to its executing agent's division ({expected})",
              WORKFLOW_DIVISIONS.get(key) == expected,
              f"ops says {WORKFLOW_DIVISIONS.get(key)!r}, agent is {expected!r}")
    else:
        check(f"{key} (no agent step) → Governance & Operations",
              division_of_workflow(key) == "Governance & Operations")

# ── 3. Divisions and fallback behaviour ─────────────────────────────────────
check("four desks defined (3 delivery divisions + G&O)",
      len(DIVISIONS) == 4 and "Governance & Operations" in DIVISIONS
      and set(agent_divisions.values()).issubset(set(DIVISIONS)),
      str(DIVISIONS))
check("unknown workflow falls back to Governance & Operations (never silent)",
      division_of_workflow("company.not-in-registry") == "Governance & Operations")

# ── 4. Schema carries roster + trail ────────────────────────────────────────
schema = (Path(__file__).resolve().parent.parent / "app" / "ops" / "schema.sql").read_text()
for fragment in ["CREATE TABLE IF NOT EXISTS team_members",
                 "CREATE TABLE IF NOT EXISTS ops_events",
                 "'BI & Analytics'", "'Risk & Controls'", "UNIQUE (org_id, name)"]:
    check(f"ops schema has {fragment!r}", fragment in schema)

print()
print("RESULT:", "ALL PASS" if not failures else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
