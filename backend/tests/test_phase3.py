"""CAIOS Phase 3 unit tests — no database required.

Covers: prompt template validation & rendering (§8.3), the default grounding
template, context formatting, DeepSeek cost estimation math (§20), and the
API contract for AI routes without a DB (auth fail-closed, validation).

Run from backend/:  python3 tests/test_phase3.py
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


from app.ai.prompts import (  # noqa: E402
    DEFAULT_PROMPT_NAME, DEFAULT_SYSTEM_PROMPT, InvalidTemplate,
    format_context, render, validate_template,
)

# ── 1. Template validation (§8.3) ────────────────────────────────────────────
validate_template("Answer from:\n{{context}}")
check("valid template accepted", True)

for bad, why in [
    ("no placeholder here", "missing {{context}}"),
    ("{{context}} and {{question}}", "unknown placeholder rejected"),
    ("{{CONTEXT}}", "case-sensitive placeholder"),
    ("   ", "empty body"),
]:
    try:
        validate_template(bad)
        check(f"rejects: {why}", False, f"accepted {bad!r}")
    except InvalidTemplate:
        check(f"rejects: {why}", True)

rendered = render("Use only:\n{{context}}\nCite as [n].", "[1] doc\nVAT is 15%")
check("render fills context", "VAT is 15%" in rendered and "{{" not in rendered)

try:
    render("{{context}} and {{unknown}}", "x")
    check("render raises on unresolvable placeholder", False)
except InvalidTemplate:
    check("render raises on unresolvable placeholder", True)

# ── 2. Default grounding template (§9) ───────────────────────────────────────
validate_template(DEFAULT_SYSTEM_PROMPT)  # must be valid by construction
check("default template valid", True)
check("default template demands citations", "[1]" in DEFAULT_SYSTEM_PROMPT)
check("default template forbids invention",
      "Never invent" in DEFAULT_SYSTEM_PROMPT)
check("default prompt name", DEFAULT_PROMPT_NAME == "rag.answer")

ctx = format_context([
    {"title": "sample.md", "seq": 2, "distance": 0.42, "content": "VAT registration at R1m"},
    {"title": "sample.md", "seq": 3, "distance": 0.51, "content": "Provisional tax on IRP6"},
])
check("context numbered for citations",
      "[1] sample.md (chunk 2, distance 0.42)" in ctx and "[2] sample.md" in ctx, ctx[:120])
check("empty context renders empty", format_context([]) == "")

# ── 3. Cost estimation math (§20 — TV-007 pending verification) ──────────────
from app.adapters.model_adapter import DeepSeekAdapter  # noqa: E402

adapter = DeepSeekAdapter.__new__(DeepSeekAdapter)  # bypass __init__ (no key needed)
cost = adapter.estimate_cost_usd(1_000_000, 500_000)
check("cost math: 1M in + 0.5M out at peak prices",
      abs(cost - (0.27 + 0.5 * 1.10)) < 1e-9, f"got {cost}")
check("cost rounded to 6 dp", round(cost, 6) == cost)

# ── 4. API contract without DB ───────────────────────────────────────────────
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
paths = client.get("/openapi.json").json()["paths"]

for route in ("/ai/answer", "/ai/usage", "/prompts", "/prompts/{prompt_id}",
              "/prompts/{prompt_id}/versions"):
    check(f"openapi exposes {route}", route in paths)

r = client.post("/ai/answer", json={"question": "VAT?", "org_id": "org-a"})
check("answer without token -> 401", r.status_code == 401, str(r.status_code))
r = client.post("/prompts", json={"org_id": "org-a", "name": "p", "body": "{{context}}"})
check("prompt create without token -> 401", r.status_code == 401, str(r.status_code))
r = client.get("/ai/usage", params={"org_id": "org-a"})
check("usage without token -> 401", r.status_code == 401, str(r.status_code))

print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(1 if failures else 0)
