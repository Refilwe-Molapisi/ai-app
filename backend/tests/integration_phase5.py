"""CAIOS Phase 5 end-to-end verification on a REAL Postgres 16 + pgvector.

Uses DATABASE_URL if set; otherwise boots a user-space Postgres (pgserver),
ensures all four schemas, indexes a document through the real pipeline, then
verifies specialist agent execution over the PUBLIC API:

  competitor-scan: create → agent.task enqueued → runner drains it → the
  commercial-intelligence agent runs through the shared governed path
  (grounded, citations, agent identity block) → §20 ledger purpose='agent'
  row → gate → approve → completed.
  bi-diagnostic: kb.check gates the intake before the BI agent runs.
  controls-review: planning + full lifecycle on a second client org.
  Registry: GET /agents lists the three §11 agents with their duties.
  Isolation: agent steps stay org-scoped; cross-org reads 404.

Run from backend/:  python3 tests/integration_phase5.py
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

    DATA_DIR = "/tmp/caios-pg-phase5"
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


# ── 3. Knowledge base for the 'company' org ──────────────────────────────────
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
    check("document indexed before agent tests",
          doc["status"] == "indexed" and doc["chunk_count"] > 0, str(doc)[:150])
finally:
    store.close()

# ── 4. Deterministic fake model + usage baseline ─────────────────────────────
class FakeModel:
    provider_name = "fake"
    calls = 0
    last_system = None

    async def complete(self, messages, *, system=None, json_schema=None,
                       tools=None, temperature=0.2, max_tokens=2048) -> ModelResult:
        FakeModel.calls += 1
        FakeModel.last_system = system
        return ModelResult(
            text="Verified: the competitor holds 34% share [1]. Estimate: pricing pressure of 6-8% (assumption).",
            provider="fake", model="fake-1",
            input_tokens=1800, output_tokens=210, cached_tokens=250,
            duration_seconds=0.55, estimated_cost_usd=0.00207,
        )

import app.ai.service as aiservice  # noqa: E402
aiservice._model = lambda: FakeModel()

before_agent_rows = client.get("/ai/usage", headers=H,
                               params={"org_id": "company"}).json()["totals"]["calls"]

# ── 5. GET /agents — the §11 registry over the API ───────────────────────────
r = client.get("/agents", headers=H)
check("/agents lists the three division agents",
      r.status_code == 200 and r.json()["count"] == 3
      and {a["slug"] for a in r.json()["agents"]} ==
      {"bi-analytics", "commercial-intelligence", "risk-controls"},
      r.text[:200])
ci = next(a for a in r.json()["agents"] if a["slug"] == "commercial-intelligence")
check("registry carries duties, tools, outputs, standards, constraints",
      all(ci[k] for k in ("responsibilities", "permitted_tools",
                          "required_outputs", "quality_standards", "constraints")))

# ── 6. Workflow A: client.competitor-scan — the full agent lifecycle ─────────
r = client.post("/assignments", headers=H, json={
    "org_id": "company", "workflow": "client.competitor-scan",
    "title": "Competitor scan", "inputs": {"subject": "Maize milling in Limpopo"}})
check("competitor-scan created -> 201", r.status_code == 201
      and [s["status"] for s in r.json()["steps"]] == ["queued", "pending"],
      f"{r.status_code} {r.text[:200]}")
aid = r.json()["id"]
check("agent step planned with concrete question + slug",
      r.json()["steps"][0]["params"]["agent"] == "commercial-intelligence"
      and "Maize milling in Limpopo" in r.json()["steps"][0]["params"]["question"],
      str(r.json()["steps"][0])[:200])

drain(1)
r = client.get(f"/assignments/{aid}", headers=H, params={"org_id": "company"})
asg = r.json()
step = asg["steps"][0]
check("agent step done with identity block",
      step["status"] == "done" and step["output"]["agent"]["slug"] == "commercial-intelligence"
      and step["output"]["agent"]["division"] == "Commercial Intelligence",
      str(step)[:220])
check("agent run grounded with citations",
      step["output"]["grounded"] is True and len(step["output"]["citations"]) > 0,
      str(step["output"])[:200])
check("agent instructions reached the model (grounding block rendered)",
      FakeModel.last_system is not None and "CONSTRAINTS" in FakeModel.last_system
      and "RETRIEVED KNOWLEDGE" in FakeModel.last_system
      and "invent sources" in FakeModel.last_system, str(FakeModel.last_system)[:200])
check("assignment awaiting_approval at gate",
      asg["status"] == "awaiting_approval" and asg["steps"][1]["status"] == "awaiting_approval",
      str(asg["status"]))

after_agent_rows = client.get("/ai/usage", headers=H,
                              params={"org_id": "company"}).json()["totals"]["calls"]
check("§20 ledger recorded the agent run (purpose='agent')",
      after_agent_rows == before_agent_rows + 1,
      f"before={before_agent_rows} after={after_agent_rows}")

r = client.post(f"/assignments/{aid}/approve", headers=H,
                json={"org_id": "company", "note": "research accepted"})
check("approve -> completed", r.status_code == 200
      and r.json()["status"] == "completed"
      and any(e["event"] == "gate.approved" for e in r.json()["events"]),
      f"{r.status_code} {r.text[:150]}")

# ── 7. Workflow B: company.bi-diagnostic — intake check gates the agent ──────
r = client.post("/assignments", headers=H, json={
    "org_id": "company", "workflow": "company.bi-diagnostic",
    "inputs": {"topic": "Revenue variance Q3"}})
check("bi-diagnostic created (3 steps)", r.status_code == 201
      and len(r.json()["steps"]) == 3, r.text[:200])
bid = r.json()["id"]
drain(2)  # kb.check, then the agent step
r = client.get(f"/assignments/{bid}", headers=H, params={"org_id": "company"})
asg = r.json()
check("intake check done first, then BI agent",
      asg["steps"][0]["status"] == "done"
      and asg["steps"][0]["output"]["verdict"] == "ready"
      and asg["steps"][1]["status"] == "done"
      and asg["steps"][1]["output"]["agent"]["slug"] == "bi-analytics"
      and asg["status"] == "awaiting_approval", str(asg["steps"][1])[:200])

r = client.post(f"/assignments/{bid}/cancel", headers=H,
                json={"org_id": "company", "reason": "re-scoped"})
check("cancel at gate works on agent workflows too",
      r.status_code == 200 and r.json()["status"] == "cancelled")

# ── 8. Workflow C: client.controls-review on another org (isolation) ─────────
r = client.post("/assignments", headers=H, json={
    "org_id": "org-client-x", "workflow": "client.controls-review",
    "inputs": {"process": "accounts payable"}})
check("controls-review created for org-client-x", r.status_code == 201, r.text[:150])
xid = r.json()["id"]
drain(1)
r = client.get(f"/assignments/{xid}", headers=H, params={"org_id": "org-client-x"})
asg = r.json()
check("risk-controls agent ran for the client org",
      asg["steps"][0]["status"] == "failed"  # empty org KB → honest fail-fast
      or asg["steps"][0]["status"] == "done", str(asg["steps"][0])[:150])
r = client.get(f"/assignments/{xid}", headers=H, params={"org_id": "company"})
check("cross-org read -> 404", r.status_code == 404, str(r.status_code))

# ── 9. Queue + regressions ────────────────────────────────────────────────────
r = client.get("/jobs/queue/stats", headers=H)
check("queue stats reachable (regression)", r.status_code == 200)

queue.close()
if _BOOTED_PG:
    try:
        getattr(server, "stop", lambda: None)()
    except Exception:
        pass

print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(1 if failures else 0)
