"""CAIOS Phase 7 unit tests — QA governance logic.

No database: the QA transition matrix and versioned key scheme are pure
functions. The state machine is the governance core — every illegal
transition must be refused, never silently absorbed.

Run from backend/:  python3 tests/test_phase7.py
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

failures: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


from app.deliverables.handlers import deliverable_key  # noqa: E402
from app.deliverables.routes import QA_ALLOWED_FROM, QA_DECISIONS, qa_transition  # noqa: E402

# ── 1. QA state machine ──────────────────────────────────────────────────────
check("approve from in_review → approved",
      qa_transition("in_review", "approve") == "approved")
check("request_changes from in_review → changes_requested",
      qa_transition("in_review", "request_changes") == "changes_requested")

for current, decision in [("draft", "approve"), ("draft", "request_changes"),
                          ("approved", "approve"), ("approved", "request_changes"),
                          ("changes_requested", "approve"), ("in_review", "approve")]:
    try:
        qa_transition(current, decision)
        ok = current == "in_review" and decision == "approve"
        check(f"transition {current}+{decision} guarded correctly", ok,
              "illegal transition accepted" if not ok else "")
    except ValueError:
        check(f"transition {current}+{decision} guarded correctly",
              not (current == "in_review" and decision == "approve"))

try:
    qa_transition("in_review", "ship_it")
    check("unknown decision refused", False)
except ValueError:
    check("unknown decision refused", True)

check("transition table complete for both decisions",
      set(QA_ALLOWED_FROM) == set(QA_DECISIONS)
      and all(v == ("in_review",) for v in QA_ALLOWED_FROM.values()))

# ── 2. Versioned storage keys ────────────────────────────────────────────────
did = uuid.uuid4()
check("company key carries version",
      deliverable_key("org-a", "company", None, did, 3, "xlsx")
      == f"company-knowledge/deliverables/org-a/{did}.v3.xlsx")
check("client key carries version under engagement prefix",
      deliverable_key("org-a", "client", "eng-42", did, 1, "md")
      == f"clients/org-a/eng-42/deliverables/{did}.v1.md")
check("distinct versions never share a key",
      deliverable_key("o", "company", None, did, 1, "pdf")
      != deliverable_key("o", "company", None, did, 2, "pdf"))

# ── 3. Schema carries the Phase 7 QA objects ────────────────────────────────
schema = (Path(__file__).resolve().parent.parent / "app" / "deliverables" / "schema.sql").read_text()
for fragment in ["ADD COLUMN IF NOT EXISTS qa_state", "deliverables_qa_state_check",
                 "CREATE TABLE IF NOT EXISTS deliverable_versions", "qa_checks"]:
    check(f"schema has {fragment!r}", fragment in schema)

print()
print("RESULT:", "ALL PASS" if not failures else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
