"""CAIOS Phase 4 end-to-end verification on a REAL Postgres 16 + pgvector.

Uses DATABASE_URL if set; otherwise boots a user-space Postgres (pgserver),
ensures all four schemas, indexes a document through the real pipeline, then
verifies the coordination engine over the PUBLIC API:

  knowledge-brief: create → first step queued as a real job → runner drains it
  (grounded AI brief via the shared service, §20 ledger row) → gate → approve
  → completed; events trail complete.
  onboarding-check: passes on an indexed org (cancel path exercised) and
  fails honestly on an empty org (fail-fast assignment).
  Isolation: cross-org reads 404; status filter works; queue cancel on racing
  step jobs; §20 ledger records workflow model calls.

Run from backend/:  python3 tests/integration_phase4.py
"""

import asyncio
import os
import shutil
import sys
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
FIX = str(Path(__file__).resolve().parent / "fixtures")

failures: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── 1. Real Postgres with pgvector, user-space ───────────────────────────────
if not os.environ.get("DATABASE_URL"):
    import pgserver

    DATA_DIR = "/tmp/caios-pg-phase4"
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    server = pgserver.get_server(DATA_DIR)
    os.environ["DATABASE_URL"] = server.get_uri()
    print(f"pgserver ready: {os.environ['DATABASE_URL'].rsplit('@', 1)[-1]}")
    _BOOTED_PG = True
else:
    _BOOTED_PG = False

os.environ.setdefault("CAIOS_ENV", "development")
os.environ.setdefault("JOB_RUN_SECRET", "it-secret")

# ── 2. App imports AFTER env is set ──────────────────────────────────────────
from fastapi.testclient import TestClient  # noqa: E402
from app.adapters.model_adapter import ModelResult  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.jobs.queue import PostgresJobQueue  # noqa: E402
from app.jobs.runner import run_batch  # noqa: E402
from app.knowledge import handlers as khandlers  # noqa: E402
from app.adapters.storage_adapter import StorageAdapter  # noqa: E402
from app.main import app  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
settings = get_settings()
queue = PostgresJobQueue(settings)
for name in ("jobs", "knowledge", "ai", "coordination"):
    queue.ensure_schema((BACKEND_DIR / "app" / name / "schema.sql").read_text())
check("four schemas ensure idempotently", True)

FAKE_OBJECTS: dict[str, bytes] = {}


class FakeStorage(StorageAdapter):
    def __init__(self, *a, **k):
        self.objects = FAKE_OBJECTS
    def put(self, key, data, content_type="application/octet-stream"):
        FAKE_OBJECTS[key] = data
        return type("Stored", (), {"key": key, "bucket": "fake", "size_bytes": len(data)})()
    def get(self, key):
        return FAKE_OBJECTS[key]


khandlers._storage = lambda: FakeStorage()
client = TestClient(app, raise_server_exceptions=True)
H = {"Authorization": "Bearer dev"}


def drain(n=1):
    for _ in range(n):
        asyncio.run(run_batch(settings, queue))


# ── 3. Index one document for the 'company' org ──────────────────────────────
md_bytes = Path(FIX, "sample.md").read_bytes()
FAKE_OBJECTS["company-knowledge/documents/sample.md"] = md_bytes
from app.knowledge.store import KnowledgeStore  # noqa: E402

store = KnowledgeStore(settings)
try:
    doc = store.register_document(
        org_id="company", scope="company", engagement_id=None,
        title="sample.md", storage_key="company-knowledge/documents/sample.md",
        mime_type="text/markdown", size_bytes=len(md_bytes),
        checksum_sha256="integration", uploaded_by="it",
    )
finally:
    store.close()
queue.enqueue("knowledge.extract", {"document_id": doc["id"]}, priority=3)
drain(2)
store = KnowledgeStore(settings)
try:
    doc = store.get_document(uuid.UUID(doc["id"]))
    check("document indexed before coordination tests",
          doc["status"] == "indexed" and doc["chunk_count"] > 0, str(doc)[:150])
finally:
    store.close()

# ── 4. Deterministic fake model through the shared service ───────────────────
class FakeModel:
    provider_name = "fake"
    calls = 0

    async def complete(self, messages, *, system=None, json_schema=None,
                       tools=None, temperature=0.2, max_tokens=2048) -> ModelResult:
        FakeModel.calls += 1
        return ModelResult(
            text="VAT registration is mandatory once taxable supplies exceed R1 million (see [1]).",
            provider="fake", model="fake-1",
            input_tokens=1500, output_tokens=120, cached_tokens=300,
            duration_seconds=0.42, estimated_cost_usd=0.001725,
        )

import app.ai.service as aiservice  # noqa: E402
aiservice._model = lambda: FakeModel()

# ── 5. Workflow A: company.knowledge-brief — the full happy path ─────────────
r = client.get("/workflows", headers=H)
check("workflows listed via API",
      r.status_code == 200
      and {"company.knowledge-brief", "client.onboarding-check"}
      <= {w["name"] for w in r.json()["workflows"]}, r.text[:150])

before_usage = client.get("/ai/usage", headers=H,
                          params={"org_id": "company"}).json()["totals"]["calls"]

r = client.post("/assignments", headers=H, json={
    "org_id": "company", "workflow": "company.knowledge-brief",
    "title": "VAT brief", "inputs": {"topic": "VAT registration thresholds"},
})
check("create assignment -> 201 running", r.status_code == 201
      and r.json()["status"] == "running", f"{r.status_code} {r.text[:250]}")
asg = r.json()
aid = asg["id"]

check("steps materialised concrete (gate pending, brief queued)",
      [s["status"] for s in asg["steps"]] == ["queued", "pending"]
      and "VAT registration thresholds" in asg["steps"][0]["params"].get("question", "")
      and asg["steps"][1]["is_gate"] is True, str(asg["steps"])[:250])
check("events: created + step.queued",
      [e["event"] for e in asg["events"]] == ["assignment.created", "step.queued"],
      str(asg["events"])[:200])

step_job = uuid.UUID(asg["steps"][0]["job_id"])
job = queue.get(step_job)
check("first step is a real queue job", job is not None and job.kind == "assignment.step",
      str(job)[:120])

drain(1)  # runner executes the brief step
r = client.get(f"/assignments/{aid}", headers=H, params={"org_id": "company"})
asg = r.json()
brief = asg["steps"][0]
check("brief step done with grounded output",
      brief["status"] == "done" and brief["output"]["grounded"] is True
      and len(brief["output"]["citations"]) > 0
      and "R1 million" in brief["output"]["answer"], str(brief)[:220])
check("brief output carries usage", brief["output"]["usage"]["input_tokens"] == 1500,
      str(brief.get("output", {}).get("usage"))[:120])
check("assignment awaiting_approval at gate",
      asg["status"] == "awaiting_approval" and asg["steps"][1]["status"] == "awaiting_approval",
      str(asg["status"]))
check("events trail: started → completed → awaiting",
      [e["event"] for e in asg["events"]][-3:] ==
      ["step.started", "step.completed", "assignment.awaiting_approval"],
      str([e["event"] for e in asg["events"]]))

after_usage = client.get("/ai/usage", headers=H,
                         params={"org_id": "company"}).json()["totals"]["calls"]
check("§20 ledger recorded the workflow model call", after_usage == before_usage + 1,
      f"before={before_usage} after={after_usage}")

r = client.post(f"/assignments/{aid}/approve", headers=H,
                json={"org_id": "company", "note": "brief looks right"})
check("approve gate -> completed (gate was last)",
      r.status_code == 200 and r.json()["status"] == "completed"
      and r.json()["steps"][1]["status"] == "done"
      and r.json()["steps"][1]["output"]["approved_by"] == "dev-anonymous",
      f"{r.status_code} {r.text[:220]}")
check("completed_at stamped", r.json()["completed_at"] is not None)
check("events: gate.approved + assignment.completed",
      [e["event"] for e in r.json()["events"]][-2:] ==
      ["gate.approved", "assignment.completed"])

r = client.post(f"/assignments/{aid}/approve", headers=H, json={"org_id": "company"})
check("approve with no open gate -> 409", r.status_code == 409, str(r.status_code))
r = client.post(f"/assignments/{aid}/cancel", headers=H, json={"org_id": "company"})
check("cancel completed assignment -> 409", r.status_code == 409, str(r.status_code))

# ── 6. Workflow B: onboarding-check — ready org, then CANCEL path ────────────
r = client.post("/assignments", headers=H, json={
    "org_id": "company", "workflow": "client.onboarding-check"})
check("onboarding-check created", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
bid = r.json()["id"]
drain(1)
r = client.get(f"/assignments/{bid}", headers=H, params={"org_id": "company"})
asg = r.json()
check("index-check done, verdict ready",
      asg["steps"][0]["status"] == "done"
      and asg["steps"][0]["output"]["verdict"] == "ready"
      and asg["status"] == "awaiting_approval", str(asg["steps"][0])[:200])

r = client.post(f"/assignments/{bid}/cancel", headers=H,
                json={"org_id": "company", "reason": "operator re-planned"})
check("cancel while awaiting_approval -> cancelled",
      r.status_code == 200 and r.json()["status"] == "cancelled"
      and r.json()["steps"][1]["status"] == "cancelled", r.text[:200])
check("cancel event with actor + reason",
      [e for e in r.json()["events"] if e["event"] == "assignment.cancelled"][0]["actor"] == "dev-anonymous",
      str(r.json()["events"])[-150:])

# ── 7. Workflow B on an EMPTY org — honest fail-fast ─────────────────────────
r = client.post("/assignments", headers=H, json={
    "org_id": "org-empty", "workflow": "client.onboarding-check"})
check("onboarding-check created for empty org", r.status_code == 201)
eid = r.json()["id"]
drain(1)
r = client.get(f"/assignments/{eid}", headers=H, params={"org_id": "org-empty"})
asg = r.json()
check("empty org: index-check failed honestly",
      asg["steps"][0]["status"] == "failed"
      and "below minimum" in (asg["steps"][0].get("last_error") or ""), str(asg["steps"][0])[:200])
check("assignment failed with event",
      asg["status"] == "failed"
      and any(e["event"] == "assignment.failed" for e in asg["events"]),
      str(asg["status"]))

# ── 8. Isolation, filters, validation ────────────────────────────────────────
r = client.get(f"/assignments/{aid}", headers=H, params={"org_id": "org-other"})
check("cross-org read -> 404", r.status_code == 404, str(r.status_code))
r = client.get("/assignments", headers=H, params={"org_id": "company", "status": "completed"})
check("status filter returns only completed",
      r.json()["count"] == 1 and r.json()["assignments"][0]["id"] == aid, r.text[:200])
r = client.get("/assignments", headers=H, params={"org_id": "org-empty"})
check("org-empty sees only its own failed assignment",
      r.json()["count"] == 1 and r.json()["assignments"][0]["id"] == eid, r.text[:150])

r = client.post("/assignments", headers=H,
                json={"org_id": "company", "workflow": "nope.nope"})
check("unknown workflow -> 422", r.status_code == 422, str(r.status_code))

r = client.get("/jobs/queue/stats", headers=H)
check("queue stats reachable (regression)", r.status_code == 200, str(r.status_code))

queue.close()
if _BOOTED_PG:
    try:
        getattr(server, "stop", lambda: None)()
    except Exception:
        pass

print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(1 if failures else 0)
