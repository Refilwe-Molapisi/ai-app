"""Deliverable builders — deterministic rendering of a structured spec into
client artefacts (Phase 6, CAIOS-DEL-001).

Four formats, one spec:

    {"meta":     {"prepared_for": ..., "period": ..., "author": ...},
     "sections": [{"heading": ..., "body": ..., "bullets": [...]}, ...],
     "tables":   [{"title": ..., "columns": [...], "rows": [[...]]}, ...]}

    md    → Markdown brief (dependency-free, also the AI-console export target)
    docx  → Word report via python-docx (already a Phase 2 dependency)
    xlsx  → Excel workbook via openpyxl (Summary + one sheet per table)
    pdf   → PDF report via reportlab (platypus; latin-1 sanitised)

Design rules:

- Deterministic: no model calls here — a spec renders identically every time.
  AI-sourced content enters through the *spec* (e.g. an AI-console answer),
  and that generation was already ledgered at the AI layer (§20).
- Honest provenance: every artefact carries a governance footer naming CAIOS,
  the UTC timestamp and a verify-before-external-use note.
- Defensive rendering: spreadsheet formula-injection guard (cells starting
  with = + - @ are stored as text, never formulas); PDF text is transliterated
  to latin-1 with NFKD so standard fonts can never render tofu boxes.

Limits mirror the route schema; the builders re-validate so direct library
use gets the same guarantees as the API.
"""

import hashlib
import html
import re
import unicodedata
from datetime import datetime, timezone

# ── limits (duplicated deliberately: the builders are the last line of defence)
MAX_SECTIONS = 50
MAX_BULLETS = 50
MAX_TABLES = 10
MAX_COLS = 30
MAX_ROWS = 5000
MAX_BODY = 20_000
MAX_TEXT = 500

MIME_BY_KIND = {
    "md": "text/markdown",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


class SpecError(ValueError):
    """Raised for structurally invalid specs — honest 422-class failures."""


def _clean_text(value, field: str, *, allow_empty: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SpecError(f"{field} must be text (got {type(value).__name__})")
    if len(value) > MAX_TEXT * 40:
        raise SpecError(f"{field} exceeds the length cap")
    return value


def validate_spec(spec: dict) -> dict:
    """Normalise + validate a spec dict; raises SpecError with a precise message."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be a JSON object")
    meta = spec.get("meta") or {}
    if not isinstance(meta, dict):
        raise SpecError("spec.meta must be an object")
    meta = {k: _clean_text(v, f"meta.{k}")[:200] for k, v in meta.items()}

    sections = spec.get("sections", [])
    if not isinstance(sections, list) or len(sections) > MAX_SECTIONS:
        raise SpecError(f"spec.sections must be a list of at most {MAX_SECTIONS} entries")
    clean_sections = []
    for i, s in enumerate(sections):
        if not isinstance(s, dict):
            raise SpecError(f"section {i} must be an object")
        heading = _clean_text(s.get("heading"), f"section {i}.heading")[:200]
        body = _clean_text(s.get("body"), f"section {i}.body")
        if len(body) > MAX_BODY:
            raise SpecError(f"section {i}.body exceeds {MAX_BODY} characters")
        bullets = s.get("bullets", [])
        if not isinstance(bullets, list) or len(bullets) > MAX_BULLETS:
            raise SpecError(f"section {i}.bullets must be a list of at most {MAX_BULLETS}")
        bullets = [_clean_text(b, f"section {i}.bullet")[:MAX_TEXT] for b in bullets]
        if not heading and not body.strip() and not any(b.strip() for b in bullets):
            continue  # skip empty sections rather than emitting blank pages
        clean_sections.append({"heading": heading, "body": body, "bullets": bullets})

    tables = spec.get("tables", [])
    if not isinstance(tables, list) or len(tables) > MAX_TABLES:
        raise SpecError(f"spec.tables must be a list of at most {MAX_TABLES} tables")
    clean_tables = []
    for i, t in enumerate(tables):
        if not isinstance(t, dict):
            raise SpecError(f"table {i} must be an object")
        title = _clean_text(t.get("title"), f"table {i}.title")[:120] or f"Table {i + 1}"
        columns = t.get("columns", [])
        if not isinstance(columns, list) or not (1 <= len(columns) <= MAX_COLS):
            raise SpecError(f"table {i} needs 1–{MAX_COLS} columns")
        columns = [_clean_text(c, f"table {i}.column")[:80] or f"Column {j + 1}"
                   for j, c in enumerate(columns)]
        rows = t.get("rows", [])
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise SpecError(f"table {i} may have at most {MAX_ROWS} rows")
        clean_rows = []
        for r, row in enumerate(rows):
            if not isinstance(row, list):
                raise SpecError(f"table {i} row {r} must be a list")
            if len(row) != len(columns):
                raise SpecError(
                    f"table {i} row {r} has {len(row)} cells; expected {len(columns)}")
            clean_rows.append([
                cell if isinstance(cell, (int, float)) and not isinstance(cell, bool)
                else ("" if cell is None else str(cell))
                for cell in row])
        clean_tables.append({"title": title, "columns": columns, "rows": clean_rows})

    if not clean_sections and not clean_tables:
        raise SpecError("spec must contain at least one non-empty section or table")
    return {"meta": meta, "sections": clean_sections, "tables": clean_tables}


def _footer(kind_label: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"Generated by CAIOS · {kind_label} · {ts} · "
            "Governed AI operations — verify figures before external use.")


def _meta_lines(meta: dict) -> list[str]:
    order = [("Prepared for", "prepared_for"), ("Period", "period"), ("Author", "author")]
    return [f"{label}: {meta[key]}" for label, key in order if meta.get(key)]


def _formula_guard(cell: str) -> str:
    """Spreadsheet formula-injection guard: a cell that starts like a formula is
    stored as literal text (apostrophe prefix) — never executed on open."""
    if isinstance(cell, str) and cell[:1] in ("=", "+", "-", "@"):
        return "'" + cell
    return cell


def _latin_safe(text: str) -> str:
    """NFKD-transliterate to latin-1 so reportlab's standard fonts can render
    every character (no tofu, no exceptions). Combining marks are stripped
    after decomposition (café → cafe); characters with no latin-1 form degrade
    to a placeholder — visible honesty beats an invisible crash."""
    if not isinstance(text, str):
        return str(text)
    decomposed = "".join(c for c in unicodedata.normalize("NFKD", text)
                         if unicodedata.category(c) != "Mn")
    return decomposed.encode("latin-1", "replace").decode("latin-1")


def _sheet_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[\\/*?:\[\]]", "_", name).strip() or "Table"
    candidate, n = base[:31], 2
    while candidate.lower() in used:
        suffix = f" ({n})"
        candidate = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(candidate.lower())
    return candidate


# ── markdown ────────────────────────────────────────────────────────────────
def build_md(title: str, spec: dict) -> bytes:
    parts: list[str] = [f"# {title}", ""]
    meta = _meta_lines(spec["meta"])
    if meta:
        parts += [f"*{html.escape(m)}*" for m in meta] + [""]
    for s in spec["sections"]:
        if s["heading"]:
            parts += [f"## {s['heading']}", ""]
        if s["body"].strip():
            parts += [s["body"].strip(), ""]
        for b in s["bullets"]:
            parts.append(f"- {b}")
        if s["bullets"]:
            parts.append("")
    for t in spec["tables"]:
        parts += [f"## {t['title']}", ""]
        esc = [str(c).replace("|", "\\|").replace("\n", " ") for c in t["columns"]]
        parts += ["| " + " | ".join(esc) + " |",
                  "|" + "|".join("---" for _ in esc) + "|"]
        for row in t["rows"]:
            cells = [str(c).replace("|", "\\|").replace("\n", " ") for c in row]
            parts.append("| " + " | ".join(cells) + " |")
        parts.append("")
    parts += ["---", "", f"_{_footer('Markdown brief')}_", ""]
    return "\n".join(parts).encode("utf-8")


# ── docx ────────────────────────────────────────────────────────────────────
def build_docx(title: str, spec: dict) -> bytes:
    import io

    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.add_heading(title, level=0)
    meta = _meta_lines(spec["meta"])
    if meta:
        p = doc.add_paragraph()
        run = p.add_run(" · ".join(meta))
        run.italic = True
        run.font.size = Pt(10)
    for s in spec["sections"]:
        if s["heading"]:
            doc.add_heading(s["heading"], level=1)
        if s["body"].strip():
            for para in s["body"].split("\n\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())
        for b in s["bullets"]:
            doc.add_paragraph(b, style="List Bullet")
    for t in spec["tables"]:
        doc.add_heading(t["title"], level=1)
        table = doc.add_table(rows=1, cols=len(t["columns"]))
        table.style = "Table Grid"
        hdr = table.rows[0].cells
        for j, col in enumerate(t["columns"]):
            hdr[j].text = ""
            run = hdr[j].paragraphs[0].add_run(col)
            run.bold = True
        for row in t["rows"]:
            cells = table.add_row().cells
            for j, cell in enumerate(row):
                cells[j].text = str(cell)
    doc.add_paragraph()
    p = doc.add_paragraph()
    run = p.add_run(_footer("Word report"))
    run.italic = True
    run.font.size = Pt(8.5)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── xlsx ────────────────────────────────────────────────────────────────────
def build_xlsx(title: str, spec: dict) -> bytes:
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary["A1"] = title
    summary["A1"].font = Font(bold=True, size=14)
    row = 3
    for line in _meta_lines(spec["meta"]):
        summary.cell(row=row, column=1, value=line).font = Font(italic=True, size=10)
        row += 1
    row += 1
    for s in spec["sections"]:
        if s["heading"]:
            summary.cell(row=row, column=1, value=s["heading"]).font = Font(bold=True, size=12)
            row += 1
        if s["body"].strip():
            cell = summary.cell(row=row, column=1, value=s["body"].strip())
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            summary.row_dimensions[row].height = min(300, 14 * (1 + s["body"].count("\n")))
            row += 1
        for b in s["bullets"]:
            summary.cell(row=row, column=1, value="• " + b)
            row += 1
        row += 1
    summary.column_dimensions["A"].width = 110
    footer_cell = summary.cell(row=row, column=1, value=_footer("Excel workbook"))
    footer_cell.font = Font(italic=True, size=9, color="555555")

    used: set[str] = set()
    for t in spec["tables"]:
        ws = wb.create_sheet(_sheet_name(t["title"], used))
        for j, col in enumerate(t["columns"], start=1):
            c = ws.cell(row=1, column=j, value=_formula_guard(col))
            c.font = Font(bold=True)
            width = max(len(col), *(len(str(r[j - 1])) for r in t["rows"])) if t["rows"] else len(col)
            ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = \
                min(40, max(10, width + 2))
        for i, cells in enumerate(t["rows"], start=2):
            for j, value in enumerate(cells, start=1):
                ws.cell(row=i, column=j, value=_formula_guard(value))
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── pdf ─────────────────────────────────────────────────────────────────────
def build_pdf(title: str, spec: dict) -> bytes:
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    styles = getSampleStyleSheet()
    body = ParagraphStyle("caios-body", parent=styles["BodyText"], leading=14)
    heading = ParagraphStyle("caios-h", parent=styles["Heading2"], spaceBefore=10)
    meta_style = ParagraphStyle("caios-meta", parent=body, fontName="Helvetica-Oblique",
                                fontSize=9)
    story: list = [Paragraph(html.escape(_latin_safe(title)), styles["Title"]), Spacer(1, 4 * mm)]
    meta = _meta_lines(spec["meta"])
    if meta:
        story.append(Paragraph(html.escape(_latin_safe(" · ".join(meta))), meta_style))
        story.append(Spacer(1, 3 * mm))
    for s in spec["sections"]:
        if s["heading"]:
            story.append(Paragraph(html.escape(_latin_safe(s["heading"])), heading))
        if s["body"].strip():
            for para in s["body"].split("\n\n"):
                if para.strip():
                    story.append(Paragraph(html.escape(_latin_safe(para.strip())).replace("\n", "<br/>"), body))
        for b in s["bullets"]:
            story.append(Paragraph("• " + html.escape(_latin_safe(b)), body))
    table_style = TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, "#9AA9BF"),
        ("BACKGROUND", (0, 0), (-1, 0), "#E8F4FC"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ])
    for t in spec["tables"]:
        story.append(Paragraph(html.escape(_latin_safe(t["title"])), heading))
        data = [[Paragraph(html.escape(_latin_safe(str(c))),
                           ParagraphStyle("th", parent=body, fontSize=8.5, fontName="Helvetica-Bold"))
                 for c in t["columns"]]]
        for row in t["rows"]:
            data.append([Paragraph(html.escape(_latin_safe(str(c))), body) for c in row])
        story.append(Table(data, repeatRows=1, style=table_style, colWidths=None))
        story.append(Spacer(1, 4 * mm))
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(html.escape(_latin_safe(_footer("PDF report"))),
                           ParagraphStyle("caios-foot", parent=body, fontName="Helvetica-Oblique",
                                          fontSize=7.5)))

    def _draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.drawCentredString(A4[0] / 2, 10 * mm, f"CAIOS · {title[:80]} · page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, title=title[:120],
                      leftMargin=18 * mm, rightMargin=18 * mm,
                      topMargin=16 * mm, bottomMargin=16 * mm).build(
        story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return buf.getvalue()


BUILDERS = {"md": build_md, "docx": build_docx, "xlsx": build_xlsx, "pdf": build_pdf}


def build_deliverable(kind: str, title: str, spec: dict) -> tuple[bytes, str, str]:
    """Validate + render. Returns (bytes, mime_type, extension)."""
    if kind not in BUILDERS:
        raise SpecError(f"unsupported deliverable kind: {kind!r} (expected one of {sorted(BUILDERS)})")
    clean = validate_spec(spec)
    data = BUILDERS[kind](title, clean)
    return data, MIME_BY_KIND[kind], kind


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
