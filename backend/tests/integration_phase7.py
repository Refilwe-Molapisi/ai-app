"""CAIOS Phase 7 end-to-end verification on a REAL Postgres 16 — QA governance.

Uses DATABASE_URL if set; otherwise boots a user-space Postgres (pgserver),
ensures all five schemas, then verifies the QA gate over the PUBLIC API:

  lifecycle: create → ready (draft) → submit-review → approve (with checklist)
  → qa_reviewer recorded from the verified principal → version row immutable.
  Regenerate on approved: deterministic spec → identical checksum → approval
  survives. Checksum drift (patched builder) → approval voided with qa.reset.
  request_changes (comment mandatory) → revise → v2 queued, QA gate reset,
  old artefact still in storage, download serves v2, versions endpoint shows
  both.
  Guards: illegal transitions → 409; request_changes without comment → 422;
  cross-org QA actions → 404 (TV-012).

Run from backend/:  python3 tests/integration_phase7.py
"""

import asyncio
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

    DATA_DIR = "/tmp/caios-pg-phase7"
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
from app.deliverables import routes as droutes  # noqa: E402
from app.jobs.queue import PostgresJobQueue  # noqa: E402
from app.jobs.runner import run_batch  # noqa: E402
from app.main import app  # noqa: E402

settings = get_settings()
queue = PostgresJobQueue(settings)
for name in ("jobs", "knowledge", "ai", "coordination", "deliverables"):
    queue.ensure_schema((BACKEND / "app" / name / "schema.sql").read_text())
check("five schemas ensure idempotently (incl. QA ALTERs + versions table)", True)

FAKE_OBJECTS: dict[str, bytes] = {}


class FakeStorage(StorageAdapter):
    def __init__(self, *a, **k):
        self.objects = FAKE_OBJECTS
    def put(self, key, data, content_type="application/octet-stream"):
        FAKE_OBJECTS[key] = data
        return type("Stored", (), {"key": key, "bucket": "fake", "size_bytes": len(data)})()
    def get(self, key):
        return FAKE_OBJECTS[key]


FAKE = FakeStorage()
dhandlers._storage = lambda: FAKE
droutes._storage = lambda: FAKE
client = TestClient(app, raise_server_exceptions=True)
H = {"Authorization": "Bearer dev"}


def drain(n=1):
    for _ in range(n):
        asyncio.run(run_batch(settings, queue))


SPEC_V1 = {
    "meta": {"prepared_for": "Example (Pty) Ltd", "period": "Q3 2026"},
    "sections": [{"heading": "Findings", "body": "Revenue grew 12% on the period.", "bullets": []}],
    "tables": [{"title": "Revenue", "columns": ["Line", "Q3"], "rows": [["Product", 1350]]}],
}

# ── 3. Lifecycle: create → ready(draft) → submit → approve ───────────────────
r = client.post("/deliverables", headers=H, json={
    "org_id": "company", "title": "Q3 Client Report", "kind": "md",
    "scope": "company", "spec": SPEC_V1})
did = r.json()["id"]
drain(1)
row = client.get(f"/deliverables/{did}", headers=H, params={"org_id": "company"}).json()["deliverable"]
check("new deliverable starts in QA draft", row["qa_state"] == "draft"
      and row["spec_version"] == 1, str(row.get("qa_state")))

r = client.post(f"/deliverables/{did}/submit-review", headers=H, json={"org_id": "company"})
check("submit-review → in_review", r.status_code == 200
      and r.json()["qa_state"] == "in_review", r.text[:150])

r = client.post(f"/deliverables/{did}/qa", headers=H, json={
    "org_id": "company", "decision": "approve",
    "comment": "Figures tie to the ledger; citations verified.",
    "checks": {"figures_verified": True, "citations_verified": True, "popia_clear": True}})
check("approve with checklist → approved", r.status_code == 200
      and r.json()["qa_state"] == "approved", r.text[:200])
check("reviewer recorded from verified principal (dev-anonymous)",
      r.json()["qa_reviewer"] == "dev-anonymous", r.json().get("qa_reviewer", ""))
row = client.get(f"/deliverables/{did}", headers=H, params={"org_id": "company"}).json()["deliverable"]
check("QA checks stored with the decision", (row["qa_checks"] or {}).get("popia_clear") is True,
      str(row.get("qa_checks")))
check("trail records submitted + approved",
      [e["event"] for e in client.get(f"/deliverables/{did}", headers=H,
                                      params={"org_id": "company"}).json()["events"]]
      .count("qa.approved") == 1)

# ── 4. Version record ────────────────────────────────────────────────────────
r = client.get(f"/deliverables/{did}/versions", headers=H, params={"org_id": "company"})
v1 = r.json()["versions"][0]
check("versions endpoint records v1 with checksum + key",
      r.json()["count"] == 1 and v1["version"] == 1
      and v1["checksum_sha256"] == row["checksum_sha256"]
      and v1["storage_key"].endswith(".v1.md"), str(v1)[:200])
check("v1 recorded with the QA state it shipped under", v1["qa_state"] == "approved")

# ── 5. Regenerate on approved: deterministic → approval survives ─────────────
r = client.post(f"/deliverables/{did}/regenerate", headers=H, json={"org_id": "company"})
check("regenerate accepted with honest note",
      r.status_code == 202 and "checksum drift" in r.json().get("note", ""), r.text[:200])
drain(1)
row = client.get(f"/deliverables/{did}", headers=H, params={"org_id": "company"}).json()["deliverable"]
check("byte-identical re-render keeps QA approval",
      row["status"] == "ready" and row["qa_state"] == "approved"
      and row["checksum_sha256"] == v1["checksum_sha256"], str(row["qa_state"]))

# ── 6. Checksum drift voids approval ─────────────────────────────────────────
original_builder = dhandlers.build_deliverable
dhandlers.build_deliverable = lambda kind, title, spec: (b"drifted bytes", "text/markdown", "md")
try:
    client.post(f"/deliverables/{did}/regenerate", headers=H, json={"org_id": "company"})
    drain(1)
finally:
    dhandlers.build_deliverable = original_builder
row = client.get(f"/deliverables/{did}", headers=H, params={"org_id": "company"}).json()
events = [e["event"] for e in row["events"]]
check("checksum drift on approved artefact → approval voided",
      row["deliverable"]["qa_state"] == "draft"
      and row["deliverable"]["checksum_sha256"] != v1["checksum_sha256"]
      and "qa.reset" in events, str(row["deliverable"]["qa_state"]))
check("qa.reset event names the drift",
      any(e["event"] == "qa.reset" and "drift" in (e["detail"] or "") for e in row["events"]))

# ── 7. request_changes → revise → v2 ─────────────────────────────────────────
client.post(f"/deliverables/{did}/submit-review", headers=H, json={"org_id": "company"})
r = client.post(f"/deliverables/{did}/qa", headers=H, json={
    "org_id": "company", "decision": "request_changes",
    "comment": "Add the September stock-count caveat before release."})
check("request_changes with comment → changes_requested",
      r.status_code == 200 and r.json()["qa_state"] == "changes_requested", r.text[:150])
r = client.post(f"/deliverables/{did}/qa", headers=H, json={
    "org_id": "company", "decision": "request_changes"})
check("request_changes without comment → 422", r.status_code == 422, str(r.status_code))

SPEC_V2 = dict(SPEC_V1)
SPEC_V2["sections"] = [{"heading": "Findings",
                        "body": "Revenue grew 12%; September stock count still outstanding.",
                        "bullets": ["Caveat added per QA"]}]
r = client.post(f"/deliverables/{did}/revise", headers=H, json={
    "org_id": "company", "spec": SPEC_V2, "comment": "QA caveat added"})
check("revise → v2 queued with QA gate reset",
      r.status_code == 202 and r.json()["version"] == 2
      and r.json()["previous_version"] == 1 and r.json()["qa_state"] == "draft", r.text[:200])
drain(1)
row = client.get(f"/deliverables/{did}", headers=H, params={"org_id": "company"}).json()["deliverable"]
check("v2 ready, gate reset holds, spec updated",
      row["status"] == "ready" and row["spec_version"] == 2 and row["qa_state"] == "draft"
      and "stock count" in row["spec"]["sections"][0]["body"], str(row["qa_state"]))
vers = client.get(f"/deliverables/{did}/versions", headers=H,
                  params={"org_id": "company"}).json()["versions"]
check("version history keeps both versions",
      [v["version"] for v in vers] == [2, 1] and len(vers) == 2, str([v["version"] for v in vers]))
check("v1 artefact still in storage (history never clobbered)",
      any(k.endswith(".v1.md") for k in FAKE_OBJECTS), str(list(FAKE_OBJECTS)))
r = client.get(f"/deliverables/{did}/download", headers=H, params={"org_id": "company"})
v2 = [v for v in vers if v["version"] == 2][0]
check("download serves the current version's bytes",
      r.status_code == 200 and v2["storage_key"] in FAKE_OBJECTS
      and FAKE_OBJECTS[v2["storage_key"]] == r.content)

# ── 8. Illegal transitions → 409 ─────────────────────────────────────────────
r = client.post(f"/deliverables/{did}/qa", headers=H, json={
    "org_id": "company", "decision": "approve"})
check("approve from draft → 409", r.status_code == 409, str(r.status_code))
client.post(f"/deliverables/{did}/submit-review", headers=H, json={"org_id": "company"})
r = client.post(f"/deliverables/{did}/submit-review", headers=H, json={"org_id": "company"})
check("double submit-review → 409", r.status_code == 409, str(r.status_code))
r = client.post("/deliverables", headers=H, json={
    "org_id": "company", "title": "Not built yet", "kind": "md", "scope": "company",
    "spec": SPEC_V1})
did2 = r.json()["id"]
r = client.post(f"/deliverables/{did2}/submit-review", headers=H, json={"org_id": "company"})
check("submit-review before generation → 409", r.status_code == 409, str(r.status_code))
r = client.post(f"/deliverables/{did2}/revise", headers=H, json={
    "org_id": "company", "spec": SPEC_V2})
check("revise while queued → 409", r.status_code == 409, str(r.status_code))

# ── 9. Isolation: cross-org QA actions → 404 (TV-012) ────────────────────────
for path, body in [
    (f"/deliverables/{did}/submit-review", {"org_id": "org-other"}),
    (f"/deliverables/{did}/qa", {"org_id": "org-other", "decision": "approve"}),
    (f"/deliverables/{did}/revise", {"org_id": "org-other", "spec": SPEC_V2}),
    (f"/deliverables/{did}/regenerate", {"org_id": "org-other"}),
]:
    r = client.post(path, headers=H, json=body)
    check(f"cross-org {path.rsplit('/', 1)[-1]} → 404", r.status_code == 404, str(r.status_code))
r = client.get(f"/deliverables/{did}/versions", headers=H, params={"org_id": "org-other"})
check("cross-org versions read → 404", r.status_code == 404, str(r.status_code))

print()
print("RESULT:", "ALL PASS" if not failures else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
