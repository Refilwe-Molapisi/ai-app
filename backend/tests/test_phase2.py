"""CAIOS Phase 2 unit tests — no database required.

Covers: extraction (all supported types + failure paths), chunking
(determinism, bounds, coverage, paragraph integrity, overlap), the
deterministic hash embedder contract (ADR-012), the pgvector literal format,
and the job-kind registry.

Run from backend/:  python3 tests/test_phase2.py
Database-dependent end-to-end verification: tests/integration_phase2.py
"""

import io
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import os
os.environ.setdefault("CAIOS_ENV", "development")
os.environ.setdefault("JOB_RUN_SECRET", "test-secret")

FIX = Path(__file__).resolve().parent / "fixtures"

from app.adapters.embedding_adapter import DeterministicHashEmbedder  # noqa: E402
from app.jobs.runner import default_registry  # noqa: E402
from app.knowledge.chunking import chunk_text  # noqa: E402
from app.knowledge.extraction import (  # noqa: E402
    EncryptedDocument, extract_document, UnsupportedDocumentType,
)
from app.knowledge.store import to_pgvector  # noqa: E402

failures: list[str] = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── 1. Extraction ────────────────────────────────────────────────────────────
md = extract_document(FIX.joinpath("sample.md").read_bytes(), filename="sample.md")
check("md extracted", len(md.text) > 1000 and "R1 million" in md.text, md.extractor_id)
check("md extractor id", md.extractor_id == "text-utf8@1", md.extractor_id)

txt = extract_document(b"plain text body\nsecond line", filename="notes.txt")
check("txt extracted", "second line" in txt.text, txt.extractor_id)

import json as _json
js = extract_document(_json.dumps({"a": 1, "b": [2, 3]}).encode(), filename="data.json")
check("json pretty-printed for chunking", "\n" in js.text and '"a"' in js.text, js.text[:80])

html = extract_document(
    b"<html><head><style>x{}</style><script>ignore()</script></head>"
    b"<body><p>First paragraph.</p><p>Second paragraph.</p></body></html>",
    filename="page.htm")
check("html strips script/style, keeps paragraphs",
      "First paragraph." in html.text and "ignore()" not in html.text, html.text[:120])

pdf = extract_document(FIX.joinpath("sample.pdf").read_bytes(), filename="sample.pdf")
check("pdf extracted (pypdf)", "VAT" in pdf.text and pdf.extractor_id.startswith("pypdf@"),
      f"{len(pdf.text)} chars, {pdf.extractor_id}")

docx = extract_document(FIX.joinpath("sample.docx").read_bytes(), filename="sample.docx")
check("docx paragraphs + table (python-docx)",
      "Scope of work" in docx.text and "3,500" in docx.text
      and docx.extractor_id.startswith("python-docx@"), docx.extractor_id)

try:
    extract_document(FIX.joinpath("sample_encrypted.pdf").read_bytes(), filename="locked.pdf")
    check("encrypted pdf raises EncryptedDocument", False)
except EncryptedDocument:
    check("encrypted pdf raises EncryptedDocument", True)
except Exception as exc:
    check("encrypted pdf raises EncryptedDocument", False, f"{type(exc).__name__}: {exc}")

try:
    extract_document(b"\x00\x01binary", filename="payload.exe")
    check("unsupported type raises UnsupportedDocumentType", False)
except UnsupportedDocumentType:
    check("unsupported type raises UnsupportedDocumentType", True)

# extension vs mime: generic mimetype but known extension must still extract
pdf2 = extract_document(FIX.joinpath("sample.pdf").read_bytes(),
                        filename="statement.pdf", mime_type="application/octet-stream")
check("dispatch by extension survives generic mimetype", "VAT" in pdf2.text)

# ── 2. Chunking ──────────────────────────────────────────────────────────────
text = md.text
chunks = chunk_text(text, max_chars=1200, overlap_chars=150)
check("chunks produced", len(chunks) > 1, str(len(chunks)))
check("deterministic", [c.content for c in chunks] ==
      [c.content for c in chunk_text(text, max_chars=1200, overlap_chars=150)])
check("all chunks within max_chars", all(len(c.content) <= 1200 for c in chunks),
      str(max(len(c.content) for c in chunks)))
check("seq contiguous", [c.seq for c in chunks] == list(range(len(chunks))))
check("coverage: starts at first paragraph, ends at document end",
      chunks[0].char_start < 60 and chunks[-1].char_end >= len(text) - 2,
      f"{chunks[0].char_start}..{chunks[-1].char_end} of {len(text)}")

# overlap: continuation chunks start before the previous chunk ends
overlaps = [chunks[i].char_start < chunks[i - 1].char_end for i in range(1, len(chunks))]
check("word-aligned overlap between continuation chunks", any(overlaps), str(overlaps))

# paragraph integrity: every paragraph survives whole inside some chunk
paras = [p.strip() for p in text.split("\n\n") if p.strip()]
missing = [p[:40] for p in paras if not any(p in c.content for c in chunks)]
check("paragraphs never split mid-paragraph", not missing, str(missing[:3]))

tiny = chunk_text("one line", 1200, 150)
check("single short text → one chunk", len(tiny) == 1 and tiny[0].seq == 0)
check("empty text → no chunks", chunk_text("   \n\n  ", 1200, 150) == [])
try:
    chunk_text("abc", 10, 8)
    check("absurd overlap rejected", False)
except ValueError:
    check("absurd overlap rejected", True)

# oversized single paragraph is hard-split with overlap
wall = " ".join(f"word{i}" for i in range(400))  # ~2800 chars, one paragraph
long_chunks = chunk_text(wall, 600, 100)
check("oversized paragraph word-split", 1 < len(long_chunks) <= 8
      and all(len(c.content) <= 600 for c in long_chunks),
      f"{len(long_chunks)} chunks, max {max(len(c.content) for c in long_chunks)}")

# ── 3. Deterministic hash embedder (ADR-012 dev implementation) ──────────────
import asyncio

emb = DeterministicHashEmbedder()
batch = asyncio.run(emb.embed(["VAT registration threshold", "provisional tax"]))
check("dims fixed at 256", batch.dimensions == 256 and
      all(len(v) == 256 for v in batch.vectors), str(batch.dimensions))
check("deterministic", batch.vectors == asyncio.run(emb.embed(
    ["VAT registration threshold", "provisional tax"])).vectors)
norm = sum(v * v for v in batch.vectors[0]) ** 0.5
check("vectors normalised", abs(norm - 1.0) < 1e-6, f"{norm:.8f}")
check("different texts → different vectors", batch.vectors[0] != batch.vectors[1])

# ── 4. pgvector literal + registry wiring ────────────────────────────────────
lit = to_pgvector([0.5, 1e-07, -2.5])
check("pgvector literal format", lit == "[0.5,1e-07,-2.5]", lit)

registry = default_registry()
check("registry has knowledge.extract", "knowledge.extract" in registry)
check("registry has knowledge.embed", "knowledge.embed" in registry)
check("registry has demo.echo (regression)", "demo.echo" in registry)

print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(1 if failures else 0)
