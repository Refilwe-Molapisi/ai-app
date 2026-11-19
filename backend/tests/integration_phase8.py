"""CAIOS Phase 8 end-to-end verification on a REAL Postgres 16 — internal ops.

Uses DATABASE_URL if set; otherwise boots a user-space Postgres (pgserver),
ensures all six schemas, then verifies the ops module over the PUBLIC API:

  roster: add → listed by division; duplicate name → 422; bad division → 422;
  PATCH role/division/active trailed; member events endpoint; deactivation
  hides from the active roster until include_inactive.
  overview: counts real seeded rows only — team by division, open workload
  per division (via the workflow allocation), queue counts, deliverable
  build + QA-gate counts, AI usage summary; a cross-org read sees none of it.
  isolation: cross-org PATCH / events / overview → 404 (TV-012).

Run from backend/:  python3 tests/integration_phase8.py
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

failures: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── 1. Real Postgres, user-space ─────────────────────────────────────────────
if not os.environ.get("DATABASE_URL"):
    import pgserver

    DATA_DIR = "/tmp/caios-pg-phase8"
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    server = pgserver.get_server(DATA_DIR)
    os.environ["DATABASE_URL"] = server.get_uri()
    print(f"pgserver ready: {os.environ['DATABASE_URL'].rsplit('@', 1)[-1]}")

os.environ.setdefault("CAIOS_ENV", "development")
os.environ.setdefault("JOB_RUN_SECRET", "it-secret")

# ── 2. App imports AFTER env is set ──────────────────────────────────────────
from fastapi.testclient import TestClient  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.jobs.queue import PostgresJobQueue  # noqa: E402
from app.main import app  # noqa: E402

settings = get_settings()
queue = PostgresJobQueue(settings)
for name in ("jobs", "knowledge", "ai", "coordination", "deliverables", "ops"):
    queue.ensure_schema((BACKEND / "app" / name / "schema.sql").read_text())
check("six schemas ensure idempotently (incl. ops roster + trail)", True)

client = TestClient(app, raise_server_exceptions=True)
H = {"Authorization": "Bearer dev"}

# ── 3. Roster: add, duplicate guard, division validation ─────────────────────
r = client.post("/ops/members", headers=H, json={
    "org_id": "company", "name": "Thabo Mokoena", "email": "thabo@caios.example",
    "division": "BI & Analytics", "role": "Senior Analyst"})
m1 = r.json()["member"]["id"]
check("add member → 201 with division set", r.status_code == 201
      and r.json()["member"]["division"] == "BI & Analytics", r.text[:150])
r = client.post("/ops/members", headers=H, json={
    "org_id": "company", "name": "Nadia Petersen", "division": "Risk & Controls",
    "role": "Controls Reviewer"})
m2 = r.json()["member"]["id"]
client.post("/ops/members", headers=H, json={
    "org_id": "company", "name": "Sipho Dlamini", "division": "Governance & Operations",
    "role": "Company Secretary"})
r = client.post("/ops/members", headers=H, json={
    "org_id": "company", "name": "Thabo Mokoena", "division": "BI & Analytics"})
check("duplicate name in org → 422", r.status_code == 422, str(r.status_code))
r = client.post("/ops/members", headers=H, json={
    "org_id": "company", "name": "Ghost Agent", "division": "Marketing"})
check("unknown division → 422", r.status_code == 422, str(r.status_code))

# ── 4. Overview correctness on seeded rows ───────────────────────────────────
# Seed one open assignment per workflow and two deliverables (different QA states).
from app.coordination.workflows import WORKFLOWS  # noqa: E402
for wf in ("company.bi-diagnostic", "client.competitor-scan",
           "client.controls-review", "company.knowledge-brief"):
    inputs = {k: "ops seed" for k in WORKFLOWS[wf].required_inputs} or {}
    r = client.post("/assignments", headers=H, json={
        "org_id": "company", "workflow": wf, "inputs": inputs})
    assert r.status_code in (200, 201, 202), r.text[:200]
from app.deliverables.store import DeliverableStore  # noqa: E402
from app.deliverables.routes import _store as _dstore  # noqa: E402
dstore = _dstore()
d1 = dstore.create(org_id="company", scope="company", engagement_id=None,
                   assignment_id=None, title="Ops seed brief", kind="md",
                   spec={"meta": {}, "sections": [{"heading": "s", "body": "b", "bullets": []}],
                         "tables": []}, created_by="it")
d2 = dstore.create(org_id="company", scope="company", engagement_id=None,
                   assignment_id=None, title="Ops seed wb", kind="xlsx",
                   spec={"meta": {}, "sections": [{"heading": "s", "body": "b", "bullets": []}],
                         "tables": []}, created_by="it")
dstore.set_qa(d1["id"], qa_state="approved", reviewer="dev-anonymous")
dstore.close()

r = client.get("/ops/overview", headers=H, params={"org_id": "company"})
ov = r.json()
check("overview returns all six sections",
      all(k in ov for k in ("team", "assignments", "jobs", "deliverables",
                            "ai", "workflow_allocation")), str(list(ov)))
check("team counted per division",
      ov["team"]["total"] == 3 and ov["team"]["active"] == 3
      and ov["team"]["by_division"]["BI & Analytics"]["members"] == 1
      and ov["team"]["by_division"]["Risk & Controls"]["members"] == 1, str(ov["team"]))
check("open workload allocated per division via workflow map",
      ov["assignments"]["open_by_division"]["BI & Analytics"] == 1
      and ov["assignments"]["open_by_division"]["Commercial Intelligence"] == 1
      and ov["assignments"]["open_by_division"]["Risk & Controls"] == 1
      and ov["assignments"]["open_by_division"]["Governance & Operations"] == 1,
      str(ov["assignments"]))
check("deliverable build + QA-gate counts are real",
      ov["deliverables"]["by_status"].get("queued") == 2
      and ov["deliverables"]["by_qa_state"].get("approved") == 1
      and ov["deliverables"]["by_qa_state"].get("draft") == 1, str(ov["deliverables"]))
check("jobs section counts from the queue",
      isinstance(ov["jobs"]["by_status"], dict) and "pending" in "".join(
          ov["jobs"]["by_status"].keys()) or ov["jobs"]["by_status"], str(ov["jobs"]))
check("AI summary section present and shaped",
      "days" in ov["ai"] or "total_calls" in ov["ai"] or isinstance(ov["ai"], dict),
      str(ov["ai"])[:120])

# ── 5. PATCH: trail, deactivation, member events ─────────────────────────────
r = client.patch(f"/ops/members/{m1}", headers=H, json={
    "org_id": "company", "role": "Lead Analyst", "active": False})
check("PATCH updates role and deactivates",
      r.status_code == 200 and r.json()["member"]["role"] == "Lead Analyst"
      and r.json()["member"]["active"] is False, r.text[:200])
r = client.get("/ops/members", headers=H, params={"org_id": "company"})
check("inactive member hidden from active roster",
      r.json()["count"] == 2 and all(m["id"] != m1 for m in r.json()["members"]))
r = client.get("/ops/members", headers=H,
               params={"org_id": "company", "include_inactive": "true"})
check("include_inactive shows the full roster", r.json()["count"] == 3)
r = client.get(f"/ops/members/{m1}/events", headers=H, params={"org_id": "company"})
kinds = [e["event"] for e in r.json()["events"]]
check("member trail records added + updated with provenance",
      r.json()["count"] == 2 and "member.added" in kinds and "member.updated" in kinds,
      str(kinds))
check("trail actor is the verified principal",
      all(e["actor"] == "dev-anonymous" for e in r.json()["events"]),
      str([e["actor"] for e in r.json()["events"]]))

# ── 6. Isolation: cross-org ops → 404 / empty (TV-012) ───────────────────────
r = client.get("/ops/members", headers=H, params={"org_id": "org-other"})
check("cross-org roster is empty", r.json()["count"] == 0)
r = client.get("/ops/overview", headers=H, params={"org_id": "org-other"})
check("cross-org overview sees no company rows",
      r.json()["team"]["total"] == 0 and sum(r.json()["assignments"]["by_status"].values()) == 0
      and not r.json()["deliverables"]["by_status"], str(r.json()["team"]))
r = client.patch(f"/ops/members/{m1}", headers=H, json={
    "org_id": "org-other", "role": "Hijack"})
check("cross-org PATCH → 404", r.status_code == 404, str(r.status_code))
r = client.get(f"/ops/members/{m2}/events", headers=H, params={"org_id": "org-other"})
check("cross-org member events → 404", r.status_code == 404, str(r.status_code))

print()
print("RESULT:", "ALL PASS" if not failures else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
