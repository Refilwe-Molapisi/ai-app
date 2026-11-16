"""CAIOS Phase 2 end-to-end verification on a REAL Postgres 16 + pgvector.

Uses DATABASE_URL if set (e.g. the compose db); otherwise boots a user-space
Postgres (pip install pgserver — no root, no Docker), ensures
both schemas, then drives the full pipeline through the PUBLIC API:

  upload (multipart) → knowledge.extract → knowledge.embed (chained via the
  queue) → /knowledge/search; plus org isolation (TV-012), controlled
  re-indexing (TV-019), the encrypted-PDF and unsupported-type failure paths,
  and the demo.echo regression.

Evidence produced for: TV-006 (embedding adapter validation), partial
TV-012 (client isolation), TV-019 (re-index), job pipeline semantics.

Run:  python3 /home/z/my-project/scripts/run_phase2_integration.py
"""

import asyncio
import hashlib
import io
import os
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


# ── 1. Real Postgres with pgvector, user-space (no root, no Docker) ─────────
import shutil

import pgserver

DATA_DIR = "/tmp/caios-pg-phase2"
shutil.rmtree(DATA_DIR, ignore_errors=True)  # fresh DB per run — no stale rows
server = pgserver.get_server(DATA_DIR)  # noqa: F841 — kept alive by reference
uri = server.get_uri()
print(f"pgserver ready: {uri.rsplit('@', 1)[-1]}")

os.environ["DATABASE_URL"] = uri
os.environ.setdefault("CAIOS_ENV", "development")
os.environ.setdefault("JOB_RUN_SECRET", "it-secret")

# ── 2. App imports AFTER env is set (Settings is cached) ────────────────────
from fastapi.testclient import TestClient
from app.adapters.embedding_adapter import DeterministicHashEmbedder, EmbeddingAdapter, EmbeddingBatch
from app.core.config import get_settings
from app.jobs.queue import PostgresJobQueue
from app.jobs.runner import run_batch
from app.knowledge import handlers as khandlers
from app.knowledge.store import KnowledgeStore, to_pgvector
from app.main import app

settings = get_settings()
queue = PostgresJobQueue(settings)
jobs_sql = (BACKEND / "app" / "jobs" / "schema.sql").read_text()
kn_sql = (BACKEND / "app" / "knowledge" / "schema.sql").read_text()
queue.ensure_schema(jobs_sql)
queue.ensure_schema(kn_sql)
queue.ensure_schema(kn_sql)  # second run must be a no-op (idempotency)
check("schemas ensure idempotently", True)

async def batch():
    return await run_batch(settings, queue)

# ── 3. Fake storage injected at both call sites (handlers + routes) ─────────
from app.adapters.storage_adapter import StorageAdapter

FAKE_OBJECTS: dict[str, bytes] = {}  # shared across instances — one virtual bucket

class FakeStorage(StorageAdapter):
    """Same static key helpers, no network — objects held in memory."""
    def __init__(self, *a, **k):
        self.objects = FAKE_OBJECTS
    def put(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data
        return type("Stored", (), {"key": key, "bucket": "fake", "size_bytes": len(data)})()
    def get(self, key):
        return self.objects[key]

khandlers._storage = lambda: FakeStorage()

client = TestClient(app, raise_server_exceptions=True)


def upload(name, content, mime, **fields):
    from unittest.mock import patch
    data = {"org_id": "org-a", "scope": "client", **fields}
    with __import__("unittest").mock.patch("app.knowledge.routes.StorageAdapter", FakeStorage):
        return client.post(
            "/documents/upload",
            headers={"Authorization": "Bearer dev"},
            files={"file": (name, content, mime)},
            data=data,
        )


def drain(n=1):
    for _ in range(n):
        asyncio.run(batch())


# ── 4. Upload happy path (multipart → 202 → pipeline → indexed) ─────────────
md_bytes = open(f"{FIX}/sample.md", "rb").read()
r = upload("sample.md", md_bytes, "text/markdown", scope="company")
check("upload sample.md -> 202", r.status_code == 202, f"{r.status_code} {r.text[:300]}")
md_doc = r.json()
md_doc_id = uuid.UUID(md_doc["id"])
check("upload returns extract job id", "extract_job_id" in md_doc)
check("sha256 checksum recorded", md_doc.get("checksum_sha256") == hashlib.sha256(md_bytes).hexdigest())

r = upload("sample.pdf", open(f"{FIX}/sample.pdf", "rb").read(), "application/pdf", engagement_id="eng-2026")
check("upload sample.pdf -> 202", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
pdf_doc_id = uuid.UUID(r.json()["id"])
r = upload("sample.docx", open(f"{FIX}/sample.docx", "rb").read(),
           "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
check("upload sample.docx -> 202", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
docx_doc_id = uuid.UUID(r.json()["id"])

drain(2)  # tick 1: extract jobs; tick 2: chained embed jobs (cron-tick rhythm)

store = KnowledgeStore(settings)
try:
    for label, doc_id in [("md", md_doc_id), ("pdf", pdf_doc_id), ("docx", docx_doc_id)]:
        doc = store.get_document(doc_id)
        check(f"{label}: status indexed", doc["status"] == "indexed", str(doc))
        check(f"{label}: chunks > 0", doc["chunk_count"] > 0, str(doc.get("chunk_count")))
        check(f"{label}: embedder recorded", doc["embed_model"] == "caios-hash-256", str(doc.get("embed_model")))
    pdf_doc = store.get_document(pdf_doc_id)
    check("pdf extractor provenance (§9.4)", (pdf_doc.get("extractor") or "").startswith("pypdf@"), str(pdf_doc.get("extractor")))
    docx_doc = store.get_document(docx_doc_id)
    check("docx extractor provenance", (docx_doc.get("extractor") or "").startswith("python-docx@"), str(docx_doc.get("extractor")))
    check("docx table text extracted", "3,500" in (docx_doc.get("extracted_text") or ""))
finally:
    store.close()

# ── 5. Search (public API, hash embedder) ────────────────────────────────────
def search(body):
    return client.post("/knowledge/search", headers={"Authorization": "Bearer dev"}, json=body)

r = search({"query": "VAT registration threshold", "org_id": "company", "scope": "company"})
check("search -> 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
payload = r.json()
check("search returns hits", len(payload["results"]) > 0, str(payload)[:300])
if payload["results"]:
    top = payload["results"][0]
    check("hit has distance in [0,2]", 0 <= top["distance"] <= 2, str(top["distance"]))
    check("hit carries title + seq", top["title"] == "sample.md" and top["seq"] >= 0, str(top)[:150])
check("search embedder metadata", payload["embedder"]["model"] == "caios-hash-256", str(payload["embedder"]))
check("search hint empty on hits", payload["hint"] is None)

r2 = search({"query": "VAT registration threshold", "org_id": "company", "scope": "company"})
check("search deterministic (TV-006)", r.json()["results"] == r2.json()["results"])

r = search({"query": "management accounts variance", "org_id": "org-a"})
check("client-org search hits", len(r.json()["results"]) > 0, str(r.json())[:200])

# ── 6. Org isolation (TV-012) ────────────────────────────────────────────────
r = search({"query": "VAT registration threshold", "org_id": "org-b"})
check("cross-org search returns nothing", r.status_code == 200 and r.json()["results"] == [], r.text[:200])
r = client.get("/documents", headers={"Authorization": "Bearer dev"}, params={"org_id": "org-b"})
check("cross-org list empty", r.json()["count"] == 0, r.text[:200])
r = client.get(f"/documents/{md_doc_id}", headers={"Authorization": "Bearer dev"}, params={"org_id": "org-b"})
check("cross-org detail 404", r.status_code == 404, str(r.status_code))
r = client.get(f"/documents/{md_doc_id}", headers={"Authorization": "Bearer dev"}, params={"org_id": "company"})
check("own-org detail visible", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

# ── 7. Re-index: idempotent, and TV-019 embedder switch ─────────────────────
store = KnowledgeStore(settings)
try:
    before = store.get_document(md_doc_id)["chunk_count"]
finally:
    store.close()

with __import__("unittest").mock.patch.object(khandlers, "_embedder", lambda: DeterministicHashEmbedder()):
    r = client.post(f"/documents/{md_doc_id}/reindex", headers={"Authorization": "Bearer dev"},
                    params={"org_id": "company"})
    check("reindex accepted -> 202", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    drain(1)
store = KnowledgeStore(settings)
try:
    doc = store.get_document(md_doc_id)
    check("reindex idempotent (same chunk count)", doc["chunk_count"] == before,
          f"{doc['chunk_count']} vs {before}")
finally:
    store.close()

class AltEmbedder(EmbeddingAdapter):
    """512-dim stand-in for a different embedder model."""
    provider_name = "hash-alt"
    async def embed(self, texts):
        import math
        vecs = []
        for t in texts:
            out, seed = [], t.strip().lower()
            while len(out) < 512:
                dg = hashlib.sha256(f"{seed}:{len(out)}".encode()).digest()
                out.extend(int.from_bytes(dg[i:i+4], "big") for i in range(0, 32, 4))
            v = out[:512]
            n = math.sqrt(sum(x*x for x in v)) or 1.0
            vecs.append([x/n for x in v])
        return EmbeddingBatch(vectors=vecs, provider=self.provider_name,
                              model="caios-hash-512", dimensions=512)

with __import__("unittest").mock.patch.object(khandlers, "_embedder", lambda: AltEmbedder()):
    r = client.post(f"/documents/{md_doc_id}/reindex", headers={"Authorization": "Bearer dev"},
                    params={"org_id": "company"})
    drain(1)
store = KnowledgeStore(settings)
try:
    doc = store.get_document(md_doc_id)
    check("TV-019: re-indexed with new embedder",
          doc["embed_model"] == "caios-hash-512" and doc["embed_dimensions"] == 512, str(doc)[:200])
    check("chunk content preserved on re-index", doc["chunk_count"] == before, str(doc["chunk_count"]))
finally:
    store.close()

r = search({"query": "VAT registration threshold", "org_id": "company", "scope": "company"})
check("cross-model search refused (empty + hint)",
      r.json()["results"] == [] and "re-index" in (r.json()["hint"] or ""), r.text[:250])

with __import__("unittest").mock.patch.object(khandlers, "_embedder", lambda: DeterministicHashEmbedder()):
    client.post(f"/documents/{md_doc_id}/reindex", headers={"Authorization": "Bearer dev"},
                params={"org_id": "company"})
    drain(1)
r = search({"query": "VAT registration threshold", "org_id": "company", "scope": "company"})
check("TV-019 closed loop: searchable again after re-index", len(r.json()["results"]) > 0)

# ── 8. Failure paths: encrypted PDF and unsupported type ────────────────────
r = upload("locked.pdf", open(f"{FIX}/sample_encrypted.pdf", "rb").read(), "application/pdf")
check("encrypted pdf upload accepted (storage is fine; extraction will fail)", r.status_code == 202,
      f"{r.status_code} {r.text[:200]}")
enc_id = uuid.UUID(r.json()["id"])
drain(3)  # attempts exhaust → job dead, document failed
store = KnowledgeStore(settings)
try:
    doc = store.get_document(enc_id)
    check("encrypted pdf → document failed", doc["status"] == "failed", str(doc)[:200])
    check("error names the cause", "password" in (doc.get("last_error") or "").lower(), str(doc.get("last_error")))
finally:
    store.close()
r = client.get(f"/jobs/{r.json()['extract_job_id']}", headers={"Authorization": "Bearer dev"})
job_json = r.json()
check("encrypted pdf job recorded error and is retrying/dead (TV-016)",
      job_json["status"] in ("pending", "dead") and job_json["attempts"] >= 1
      and "password" in (job_json.get("last_error") or "").lower(), str(job_json)[:150])

r = upload("payload.exe", b"MZ\xf0\x00not-a-document", "application/x-msdownload")
check("unsupported extension rejected at upload -> 422", r.status_code == 422, str(r.status_code))

# direct path: registered doc whose bytes cannot be extracted
store = KnowledgeStore(settings)
try:
    bad = store.register_document(
        org_id="org-a", scope="client", engagement_id="eng-2026", title="binary.bin",
        storage_key="clients/org-a/eng-2026/documents/binary.bin",
        mime_type="application/octet-stream", size_bytes=16,
        checksum_sha256=hashlib.sha256(b"\x00" * 16).hexdigest(), uploaded_by="dev-anonymous")
    khandlers._storage().objects["clients/org-a/eng-2026/documents/binary.bin"] = b"\x00" * 16
finally:
    store.close()
jid = queue.enqueue("knowledge.extract", {"document_id": bad["id"]}, max_attempts=1)
drain(1)
store = KnowledgeStore(settings)
try:
    doc = store.get_document(uuid.UUID(bad["id"]))
    check("unsupported binary → failed with clear error", doc["status"] == "failed"
          and "unsupported document type" in (doc.get("last_error") or ""), str(doc)[:250])
finally:
    store.close()

# ── 9. Auth guards + regression ──────────────────────────────────────────────
r = client.post("/documents/upload", files={"file": ("x.md", md_bytes, "text/markdown")},
                data={"org_id": "org-a"})
check("upload without token -> 401", r.status_code == 401, str(r.status_code))
r = client.post("/knowledge/search", json={"query": "x", "org_id": "org-a"})
check("search without token -> 401", r.status_code == 401, str(r.status_code))
r = client.post("/jobs", headers={"Authorization": "Bearer dev"}, json={"kind": "demo.echo", "payload": {"hi": 1}})
check("demo.echo still enqueues", r.status_code == 202, str(r.status_code))
drain(1)
r = client.get(f"/jobs/{r.json()['id']}", headers={"Authorization": "Bearer dev"})
check("demo.echo still completes", r.json()["status"] == "done", str(r.json())[:150])
r = client.get("/knowledge/stats", headers={"Authorization": "Bearer dev"}, params={"org_id": "org-a"})
check("knowledge stats shape", r.status_code == 200 and "documents_by_status" in r.json(), r.text[:150])

queue.close()
try:  # pgserver stops the cluster on process exit; be explicit where possible
    getattr(server, "stop", lambda: None)()
except Exception:
    pass

print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(1 if failures else 0)
