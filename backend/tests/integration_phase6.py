"""CAIOS Phase 6 end-to-end verification on a REAL Postgres 16.

Uses DATABASE_URL if set; otherwise boots a user-space Postgres (pgserver),
ensures all five schemas, then verifies the deliverables pipeline over the
PUBLIC API:

  xlsx lifecycle: create (202 + job id) → runner drains report.generate →
  status ready with size/checksum → download streams the exact bytes →
  event trail created→generating→ready.
  md/docx/pdf kinds all render and download with correct mime types.
  Not-ready download → 409. Cross-org reads → 404 (isolation, TV-012).
  Honest failure: storage put fails → row failed with last_error AND job
  failed (queue retry policy applies); failure recorded on the trail.
  Validation: empty spec / bad kind / client-scope without engagement → 422.

Run from backend/:  python3 tests/integration_phase6.py
"""

import asyncio
import io
import os
import shutil
import sys
import uuid
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

    DATA_DIR = "/tmp/caios-pg-phase6"
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    server = pgserver.get_server(DATA_DIR)
    os.environ["DATABASE_URL"] = server.get_uri()
    print(f"pgserver ready: {os.environ['DATABASE_URL'].rsplit('@', 1)[-1]}")

os.environ.setdefault("CAIOS_ENV", "development")
os.environ.setdefault("JOB_RUN_SECRET", "it-secret")

# ── 2. App imports AFTER env is set ──────────────────────────────────────────
from fastapi.testclient import TestClient  # noqa: E402
from app.adapters.storage_adapter import StorageAdapter  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.deliverables import handlers as dhandlers  # noqa: E402
from app.jobs.queue import PostgresJobQueue  # noqa: E402
from app.jobs.runner import run_batch  # noqa: E402
from app.main import app  # noqa: E402

settings = get_settings()
queue = PostgresJobQueue(settings)
for name in ("jobs", "knowledge", "ai", "coordination", "deliverables"):
    queue.ensure_schema((BACKEND / "app" / name / "schema.sql").read_text())
check("five schemas ensure idempotently", True)

FAKE_OBJECTS: dict[str, bytes] = {}


class FakeStorage(StorageAdapter):
    def __init__(self, *a, **k):
        self.objects = FAKE_OBJECTS
        self.fail_puts = False
    def put(self, key, data, content_type="application/octet-stream"):
        if self.fail_puts:
            raise OSError("simulated storage outage (put failed)")
        FAKE_OBJECTS[key] = data
        return type("Stored", (), {"key": key, "bucket": "fake", "size_bytes": len(data)})()
    def get(self, key):
        return FAKE_OBJECTS[key]


FAKE = FakeStorage()
dhandlers._storage = lambda: FAKE
from app.deliverables import routes as droutes  # noqa: E402
droutes._storage = lambda: FAKE
client = TestClient(app, raise_server_exceptions=True)
H = {"Authorization": "Bearer dev"}


def drain(n=1):
    for _ in range(n):
        asyncio.run(run_batch(settings, queue))


XLSX_SPEC = {
    "meta": {"prepared_for": "Example (Pty) Ltd", "period": "Q3 2026"},
    "sections": [
        {"heading": "Findings", "body": "Revenue grew 12% on the period.", "bullets": ["Verified: mix shift"]},
        {"heading": "Assumptions", "body": "FX held flat at R18.20/USD.", "bullets": []},
    ],
    "tables": [{"title": "Revenue", "columns": ["Line", "Q2", "Q3", "Var"],
                "rows": [["Product", 1200, 1350, "+12.5%"], ["Services", 400, 390, "-2.5%"]]}],
}

# ── 3. Full lifecycle: create → generate → download ──────────────────────────
r = client.post("/deliverables", headers=H, json={
    "org_id": "company", "title": "Q3 Variance Workbook", "kind": "xlsx",
    "scope": "company", "spec": XLSX_SPEC})
check("create returns 202 with id + job id", r.status_code == 202
      and r.json().get("id") and r.json().get("job_id"), r.text[:200])
did = r.json()["id"]

r = client.get(f"/deliverables/{did}/download", headers=H, params={"org_id": "company"})
check("download before generation → 409", r.status_code == 409, str(r.status_code))

drain(1)

r = client.get(f"/deliverables/{did}", headers=H, params={"org_id": "company"})
row = r.json()["deliverable"]
check("status ready with size + checksum", row["status"] == "ready"
      and row["size_bytes"] > 0 and (row["checksum_sha256"] or "").startswith(tuple("0123456789abcdef")),
      str(row)[:220])
check("mime is xlsx", row["mime_type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
events = r.json()["events"]
check("event trail created→generating→ready",
      [e["event"] for e in events] == ["deliverable.created", "deliverable.generating", "deliverable.ready"],
      str([e["event"] for e in events]))

r = client.get(f"/deliverables/{did}/download", headers=H, params={"org_id": "company"})
check("download 200 streams exact stored bytes", r.status_code == 200
      and r.content == FAKE_OBJECTS[row["storage_key"]])
check("download Content-Disposition names the artefact",
      "Q3_Variance_Workbook.xlsx" in r.headers.get("content-disposition", ""),
      r.headers.get("content-disposition", ""))

# ── 4. Every kind renders and downloads ──────────────────────────────────────
kind_checks = {
    "md": ("AI answer export — VAT brief", "text/markdown", b"Generated by CAIOS"),
    "docx": ("Field review report", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK"),
    "pdf": ("Client proposal", "application/pdf", b"%PDF-"),
}
for kind, (title, mime, magic) in kind_checks.items():
    spec = dict(XLSX_SPEC)
    r = client.post("/deliverables", headers=H, json={
        "org_id": "company", "title": title, "kind": kind, "scope": "company", "spec": spec})
    kid = r.json()["id"]
    drain(1)
    r = client.get(f"/deliverables/{kid}/download", headers=H, params={"org_id": "company"})
    bytes_ok = (r.content[:len(magic)] == magic) if kind in ("docx", "pdf") \
        else (magic in r.content[:600])
    check(f"{kind}: ready + mime + expected bytes",
          r.status_code == 200 and r.headers["content-type"].startswith(mime) and bytes_ok,
          f"{r.status_code} {r.headers.get('content-type')}")

# ── 5. List + isolation (TV-012) ────────────────────────────────────────────
r = client.get("/deliverables", headers=H, params={"org_id": "company"})
listed = r.json()["deliverables"]
check("list shows 4 deliverables, no spec column leaked",
      r.json()["count"] == 4 and all("spec" not in d for d in listed),
      str(r.json().get("count")))

r = client.post("/deliverables", headers=H, json={
    "org_id": "org-client-x", "title": "Client artefact", "kind": "md",
    "scope": "client", "engagement_id": "eng-42", "spec": XLSX_SPEC})
xid = r.json()["id"]
drain(1)
r = client.get(f"/deliverables/{xid}", headers=H, params={"org_id": "company"})
check("cross-org detail read → 404", r.status_code == 404, str(r.status_code))
r = client.get(f"/deliverables/{xid}/download", headers=H, params={"org_id": "company"})
check("cross-org download → 404", r.status_code == 404, str(r.status_code))
r = client.get(f"/deliverables/{did}", headers=H, params={"org_id": "org-client-x"})
check("reverse cross-org read → 404", r.status_code == 404, str(r.status_code))
r = client.get(f"/deliverables/{xid}", headers=H, params={"org_id": "org-client-x"})
check("own-org client-scope deliverable ready with client key prefix",
      r.json()["deliverable"]["status"] == "ready"
      and str(r.json()["deliverable"]["storage_key"]).startswith("clients/org-client-x/eng-42/"),
      r.json()["deliverable"].get("storage_key", ""))

# ── 6. Honest failure: storage outage marks the row AND fails the job ────────
FAKE.fail_puts = True
r = client.post("/deliverables", headers=H, json={
    "org_id": "company", "title": "Doomed report", "kind": "md", "scope": "company",
    "spec": XLSX_SPEC})
didf = r.json()["id"]
report = asyncio.run(run_batch(settings, queue))
FAKE.fail_puts = False
r = client.get(f"/deliverables/{didf}", headers=H, params={"org_id": "company"})
rowf = r.json()["deliverable"]
check("storage outage → deliverable failed with honest last_error",
      rowf["status"] == "failed" and "OSError" in (rowf["last_error"] or ""),
      str(rowf)[:200])
check("failure recorded on the event trail",
      any(e["event"] == "deliverable.failed" for e in r.json()["events"]))
check("job failed (retry policy applies)", report.failed == 1, str(report.details))
check("failed artefact download → 409 with reason",
      client.get(f"/deliverables/{didf}/download", headers=H,
                 params={"org_id": "company"}).status_code == 409)
# TV-016 backoff is 30 s; pull available_at forward so the retry lands in this test.
with queue._pool.connection() as conn:
    conn.execute("UPDATE jobs SET available_at = now() - interval '1 second' "
                 "WHERE kind = 'report.generate' AND status = 'pending'")
drain(1)  # retry with the outage over → it completes
r = client.get(f"/deliverables/{didf}", headers=H, params={"org_id": "company"})
check("after outage clears, retry completes → ready",
      r.json()["deliverable"]["status"] == "ready", r.json()["deliverable"]["status"])

# ── 7. Validation (422 class) ────────────────────────────────────────────────
r = client.post("/deliverables", headers=H, json={
    "org_id": "company", "title": "Empty", "kind": "md", "scope": "company",
    "spec": {"sections": [], "tables": []}})
check("empty spec → 422", r.status_code == 422, str(r.status_code))
r = client.post("/deliverables", headers=H, json={
    "org_id": "company", "title": "Bad kind", "kind": "pptx", "scope": "company",
    "spec": XLSX_SPEC})
check("unsupported kind → 422", r.status_code == 422, str(r.status_code))
r = client.post("/deliverables", headers=H, json={
    "org_id": "company", "title": "Client no engagement", "kind": "md", "scope": "client",
    "spec": XLSX_SPEC})
check("client scope without engagement_id → 422", r.status_code == 422, str(r.status_code))

print()
print("RESULT:", "ALL PASS" if not failures else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
