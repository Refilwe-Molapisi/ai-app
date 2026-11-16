"""Text chunking — deterministic, paragraph-aware, with overlap.

Phase 2 (Knowledge & documents, CAIOS-MASTER-001 §9). Chunking is the bridge
between extraction and embedding: chunks are the retrieval unit, so the
algorithm must be deterministic (re-indexing produces identical chunks) and
track source offsets (citations can point back into the document).

Design:
- Paragraph-aware packing: whole paragraphs are packed up to `max_chars`
  (approx. 300–400 tokens per chunk at ~1200 chars — inside the context sweet
  spot for retrieval-then-generation).
- Oversized paragraphs are word-aligned hard splits with `overlap_chars` so
  sentences spanning a boundary stay retrievable.
- Continuation chunks start with the tail of the previous chunk (word-aligned
  overlap), so a fact at a boundary appears completely in at least one chunk.
- char_start/char_end are offsets into the EXTRACTED text; `content` is the
  stripped slice. Union of ranges covers the document — no gaps.
"""

import re
from dataclasses import dataclass

_PARA_SEP = re.compile(r"\n{2,}")


@dataclass
class Chunk:
    seq: int
    content: str
    char_start: int
    char_end: int


def chunk_text(text: str, max_chars: int = 1200, overlap_chars: int = 150) -> list[Chunk]:
    """Split extracted text into deterministic overlapping chunks."""
    if not text or not text.strip():
        return []
    if max_chars < 2 * overlap_chars + 1:
        raise ValueError("max_chars must exceed 2*overlap_chars+1 (room for real content)")

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    ranges = _pack(_paragraph_ranges(text), text, max_chars, overlap_chars)

    chunks: list[Chunk] = []
    for seq, (start, end) in enumerate(ranges):
        slices = [(start, end)] if end - start <= max_chars else _split_long(start, end, text, max_chars, overlap_chars)
        for (s, e) in slices:
            content = text[s:e].strip()
            if content:
                chunks.append(Chunk(seq=len(chunks), content=content, char_start=s, char_end=e))
    return chunks


def _paragraph_ranges(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of non-empty paragraphs, blank lines as separators."""
    ranges: list[tuple[int, int]] = []
    start = 0
    for m in _PARA_SEP.finditer(text):
        end = m.start()
        if text[start:end].strip():
            ranges.append((start, end))
        start = m.end()
    if text[start:].strip():
        ranges.append((start, len(text)))
    return ranges


def _pack(ranges: list[tuple[int, int]], text: str, max_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    """Greedily pack consecutive paragraphs; continuation chunks carry overlap."""
    packed: list[tuple[int, int]] = []
    cur_start: int | None = None
    cur_end = 0
    for ps, pe in ranges:
        if cur_start is None:
            cur_start, cur_end = ps, pe
            continue
        if (pe - cur_start) <= max_chars:
            cur_end = pe  # separator between paragraphs is inside the span
            continue
        packed.append((cur_start, cur_end))
        new_start = _overlap_start(text, cur_start, cur_end, overlap_chars)
        if pe - new_start > max_chars:
            packed.extend(_split_long(new_start, pe, text, max_chars, overlap_chars))
            cur_start = None
        else:
            cur_start, cur_end = new_start, pe
    if cur_start is not None:
        packed.append((cur_start, cur_end))
    return packed


def _split_long(start: int, end: int, text: str, max_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    """Word-aligned windowing of a span longer than max_chars, with overlap."""
    out: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        stop = min(pos + max_chars, end)
        if stop < end:
            # Prefer a word boundary near the limit so words are not cut.
            sp = text.rfind(" ", pos + max_chars // 2, stop)
            if sp > pos:
                stop = sp
        out.append((pos, stop))
        if stop >= end:
            break
        nxt = _overlap_start(text, pos, stop, overlap_chars)
        pos = nxt if nxt > pos else stop
        while pos < end and text[pos].isspace():
            pos += 1
    return out


def _overlap_start(text: str, start: int, end: int, overlap_chars: int) -> int:
    """Start of the next chunk: tail of the previous one, snapped to a word start."""
    pos = max(start, end - overlap_chars)
    while pos < end and not text[pos].isspace():
        pos += 1
    while pos < end and text[pos].isspace():
        pos += 1
    return pos
