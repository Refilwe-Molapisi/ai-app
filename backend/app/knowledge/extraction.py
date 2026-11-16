"""Document text extraction — pluggable, provenance-tracked (§9.4, §7.2).

Phase 2 supports the document types a small consultancy actually receives:
PDF (statements, SARS letters), DOCX (management accounts, board packs),
and plain text formats (TXT/MD/CSV/JSON/HTML exports from Xero/Sage/SARS
eFiling). Everything is pure-Python (pypdf, python-docx) so the container
stays slim and runs identically on a laptop and on Render.

Every extraction returns its extractor identity — recorded on the document
row (§9.4 provenance) so a bad extractor version can be re-run selectively.

Heavy bulk extraction at scale is routed to GitHub Actions via the
maintenance tick (blueprint §4.1); per-document extraction inside a /jobs/run
batch stays within the 25 s budget.
"""

import io
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath

SUPPORTED_EXTENSIONS = {
    ".txt": "text", ".md": "text", ".markdown": "text", ".csv": "text",
    ".json": "text", ".log": "text",
    ".html": "html", ".htm": "html",
    ".pdf": "pdf",
    ".docx": "docx",
}


@dataclass
class ExtractedDocument:
    text: str
    extractor_id: str
    kind: str  # text | html | pdf | docx


class UnsupportedDocumentType(ValueError):
    """Raised for file types Phase 2 does not extract. The document row keeps
    status='failed' with this message; the binary is safely stored and can be
    re-extracted once a handler exists."""


class EncryptedDocument(ValueError):
    """Password-protected PDF/DOCX — never guess credentials; fail the job and
    let a human unlock the original."""


class _TextHTMLParser(HTMLParser):
    """Minimal tag-stripping text extractor (stdlib, no dependencies)."""

    _SKIP = {"script", "style", "head", "title", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in ("p", "br", "tr", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "table"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        # Collapse runs of blank lines into paragraph separators.
        raw = "".join(self._parts)
        lines = [ln.strip() for ln in raw.splitlines()]
        return "\n\n".join(ln for ln in lines if ln)


def extract_document(data: bytes, *, filename: str, mime_type: str = "") -> ExtractedDocument:
    """Extract text from document bytes. Dispatch is by extension first, mime
    second (SARS/bank portals often send generic mimetypes)."""
    ext = PurePosixPath((filename or "").replace("\\", "/")).suffix.lower()
    kind = SUPPORTED_EXTENSIONS.get(ext)
    if kind is None:
        kind = _kind_from_mime(mime_type)
    if kind is None:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise UnsupportedDocumentType(
            f"unsupported document type '{ext or mime_type or 'unknown'}'; supported: {supported}"
        )

    if kind == "text":
        return _extract_text(data)
    if kind == "html":
        parser = _TextHTMLParser()
        parser.feed(_decode(data))
        return ExtractedDocument(parser.text(), "html-stdlib@1", "html")
    if kind == "pdf":
        return _extract_pdf(data)
    return _extract_docx(data)


def _kind_from_mime(mime_type: str) -> str | None:
    mime = (mime_type or "").split(";")[0].strip().lower()
    return {
        "text/plain": "text", "text/markdown": "text", "text/csv": "text",
        "application/json": "text",
        "text/html": "html",
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    }.get(mime)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")  # legacy exports; never raises


def _extract_text(data: bytes) -> ExtractedDocument:
    text = _decode(data)
    if text.lstrip().startswith("{") or text.lstrip().startswith("["):
        try:  # pretty-print JSON so keys stay greppable in chunks
            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pass
    return ExtractedDocument(text, "text-utf8@1", "text")


def _extract_pdf(data: bytes) -> ExtractedDocument:
    import pypdf  # lazy: optional dependency

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise EncryptedDocument("PDF is password-protected; unlock the original and re-upload")
        pages = [(page.extract_text() or "") for page in reader.pages]
    except EncryptedDocument:
        raise
    except Exception as exc:  # noqa: BLE001 — malformed PDFs surface as job failures
        raise UnsupportedDocumentType(f"unreadable PDF ({type(exc).__name__})") from exc
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    version = getattr(pypdf, "__version__", "unknown")
    return ExtractedDocument(text, f"pypdf@{version}", "pdf")


def _extract_docx(data: bytes) -> ExtractedDocument:
    import docx  # python-docx, lazy import

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise UnsupportedDocumentType(f"unreadable DOCX ({type(exc).__name__})") from exc
    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:  # management-accounts tables matter most
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    version = getattr(docx, "__version__", "unknown")
    return ExtractedDocument("\n\n".join(parts), f"python-docx@{version}", "docx")
