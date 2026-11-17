"""CAIOS Phase 3 end-to-end verification on a REAL Postgres 16 + pgvector.

Uses DATABASE_URL if set (e.g. the compose db); otherwise boots a user-space
Postgres (pip install pgserver — no root, no Docker), ensures all three
schemas, indexes a document through the real pipeline, then verifies Phase 3:

  prompt create/versioning (409/422 paths) → /ai/answer grounded=true with
  citations and a §20 ledger row → empty-org ungrounded path → model-failure
  path (502 + ledger 'error' row) → /ai/usage summary.

Run from backend/:  python3 tests/integration_phase3.py
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

    DATA_DIR = "/tmp/caios-pg-phase3"
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
from app.jobs.queue import PostgresJobQueue  # noqa: E402
from app.jobs.runner import run_batch  # noqa: E402
from app.knowledge import handlers as khandlers  # noqa: E402
from app.adapters.storage_adapter import StorageAdapter  # noqa: E402
from app.main import app  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent

from app.core.config import get_settings  # noqa: E402

settings = get_settings()
queue = PostgresJobQueue(settings)
for name in ("jobs", "knowledge", "ai"):
    sql = (BACKEND_DIR / "app" / name / "schema.sql").read_text()
    queue.ensure_schema(sql)
queue.ensure_schema((BACKEND_DIR / "app" / "ai" / "schema.sql").read_text())
check("three schemas ensure idempotently", True)

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


def drain(n=1):
    for _ in range(n):
        asyncio.run(run_batch(settings, queue))


# ── 3. Index one document through the real pipeline ──────────────────────────
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
    check("document indexed before AI tests", doc["status"] == "indexed" and doc["chunk_count"] > 0,
          str(doc)[:150])
finally:
    store.close()

# ── 4. Fake model (deterministic; usage numbers asserted later) ──────────────
class FakeModel:
    provider_name = "fake"
    last_system = None

    async def complete(self, messages, *, system=None, json_schema=None,
                       tools=None, temperature=0.2, max_tokens=2048) -> ModelResult:
        FakeModel.last_system = system
        return ModelResult(
            text="Registration is mandatory above R1 million (see [1]).",
            provider="fake", model="fake-1",
            input_tokens=1500, output_tokens=120, cached_tokens=300,
            duration_seconds=0.42, estimated_cost_usd=0.001725,
        )

class BrokenModel:
    provider_name = "fake"
    async def complete(self, *a, **k):
        raise RuntimeError("provider unreachable (simulated)")

import app.ai.routes as airoutes  # noqa: E402
airoutes._model = lambda: FakeModel()

# ── 5. Prompt management ─────────────────────────────────────────────────────
r = client.post("/prompts", headers={"Authorization": "Bearer dev"}, json={
    "org_id": "company", "name": "rag.answer",
    "description": "grounded answer template",
    "body": "Answer ONLY from:\n{{context}}\nCite as [n].",
})
check("create prompt v1 -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
prompt_id = r.json()["id"]

r = client.post("/prompts", headers={"Authorization": "Bearer dev"},
                json={"org_id": "company", "name": "rag.answer", "body": "{{context}}"})
check("duplicate prompt name -> 409", r.status_code == 409, str(r.status_code))

r = client.post("/prompts", headers={"Authorization": "Bearer dev"},
                json={"org_id": "company", "name": "bad.prompt", "body": "no placeholder"})
check("template without {{context}} -> 422", r.status_code == 422, str(r.status_code))

r = client.post(f"/prompts/{prompt_id}/versions", headers={"Authorization": "Bearer dev"}, json={
    "org_id": "company",
    "body": "You are CAIOS. Use ONLY these documents:\n{{context}}\nCite as [1], [2]. Say when unsure.",
})
check("add version 2 -> 201 + active", r.status_code == 201 and r.json()["version"] == 2,
      f"{r.status_code} {r.text[:150]}")

r = client.post(f"/prompts/{prompt_id}/versions", headers={"Authorization": "Bearer dev"},
                json={"org_id": "company", "body": "{{question}}"})
check("version with illegal placeholder -> 422", r.status_code == 422, str(r.status_code))

r = client.get(f"/prompts/{prompt_id}", headers={"Authorization": "Bearer dev"},
               params={"org_id": "company"})
detail = r.json()
check("prompt detail has both versions", len(detail.get("versions", [])) == 2
      and detail["versions"][0]["version"] == 2, str(detail)[:200])
check("only v2 active", [v["is_active"] for v in detail["versions"]] == [True, False])

r = client.get("/prompts", headers={"Authorization": "Bearer dev"}, params={"org_id": "org-x"})
check("prompt list org-scoped (empty elsewhere)", r.json()["count"] == 0, r.text[:120])

# ── 6. The grounded answer loop ──────────────────────────────────────────────
r = client.post("/ai/answer", headers={"Authorization": "Bearer dev"}, json={
    "question": "When is VAT registration mandatory?",
    "org_id": "company",
})
check("answer -> 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
payload = r.json()
check("answer text from model", payload["answer"].startswith("Registration is mandatory"),
      str(payload.get("answer"))[:80])
check("grounded=true with citations", payload["grounded"] is True
      and len(payload["citations"]) > 0
      and payload["citations"][0]["title"] == "sample.md", str(payload.get("citations"))[:150])
check("active prompt v2 used", payload["prompt"]["version"] == 2, str(payload.get("prompt")))
check("template received numbered context",
      FakeModel.last_system is not None and "[1] sample.md" in (FakeModel.last_system or ""),
      str(FakeModel.last_system)[:150])
usage = payload["usage"]
check("usage tokens from adapter", usage["input_tokens"] == 1500
      and usage["output_tokens"] == 120 and usage["estimated_cost_usd"] == 0.001725,
      str(usage))

# ── 7. Ledger (§20) persisted ────────────────────────────────────────────────
r = client.get("/ai/usage", headers={"Authorization": "Bearer dev"},
               params={"org_id": "company", "days": 30})
summary = r.json()
check("ledger totals recorded", summary["totals"]["calls"] == 1
      and summary["totals"]["input_tokens"] == 1500
      and summary["totals"]["estimated_cost_usd"] == 0.001725, str(summary["totals"]))
check("ledger by_model row", summary["by_model"][0]["model"] == "fake-1",
      str(summary["by_model"]))

# ── 8. Ungrounded path (org with no knowledge) ───────────────────────────────
r = client.post("/ai/answer", headers={"Authorization": "Bearer dev"}, json={
    "question": "Anything at all?", "org_id": "org-empty",
})
check("empty-org answer -> 200 ungrounded + hint", r.status_code == 200
      and r.json()["grounded"] is False
      and "no chunks indexed" in (r.json()["retrieval"]["hint"] or ""), r.text[:250])

# ── 9. Model failure path: 502 + ledger 'error' row ──────────────────────────
airoutes._model = lambda: BrokenModel()
r = client.post("/ai/answer", headers={"Authorization": "Bearer dev"}, json={
    "question": "Will this fail?", "org_id": "company",
})
check("model failure -> 502", r.status_code == 502, f"{r.status_code} {r.text[:150]}")
r = client.get("/ai/usage", headers={"Authorization": "Bearer dev"},
               params={"org_id": "company", "days": 30})
summary = r.json()
check("failure recorded in ledger (org-scoped: 1 ok + 1 error)",
      summary["totals"]["errors"] == 1 and summary["totals"]["calls"] == 2,
      str(summary["totals"]))
r = client.get("/ai/usage", headers={"Authorization": "Bearer dev"},
               params={"org_id": "org-empty", "days": 30})
check("org-empty ledger holds its own call",
      r.json()["totals"]["calls"] == 1 and r.json()["totals"]["errors"] == 0,
      str(r.json()["totals"]))
airoutes._model = lambda: FakeModel()

# ── 10. Auth regression on AI routes ─────────────────────────────────────────
r = client.post("/ai/answer", json={"question": "x", "org_id": "company"})
check("answer without token -> 401", r.status_code == 401, str(r.status_code))
r = client.get("/jobs/queue/stats", headers={"Authorization": "Bearer dev"})
check("queue stats reachable (regression)", r.status_code == 200, str(r.status_code))

queue.close()
if _BOOTED_PG:
    try:
        getattr(server, "stop", lambda: None)()
    except Exception:
        pass

print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(1 if failures else 0)
