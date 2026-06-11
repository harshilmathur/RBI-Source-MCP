"""Paragraph-level chunking for RBI Master Direction PDFs.

The goal of chunking here is NOT to produce uniform-length chunks for
embedding (that's a v0.5 concern when sqlite-vec lands). The goal is to
produce **citable** chunks: each chunk corresponds to a numbered paragraph
or sub-paragraph in the MD, so when retrieval surfaces it, the LLM can
attribute it to a specific section.anchor.

RBI MDs use a consistent outline:

    1. Top-level paragraph
    2. Another top-level paragraph
        a. Sub-clause
        b. Another sub-clause
            i. Sub-sub-clause

The chunker walks the layout-preserved pdftotext output line by line, tracks
the current section path (e.g., "8.2.a"), and yields a chunk whenever a new
top-level or second-level number is encountered. Page numbers come from
form-feed boundaries.

Limitations at v0.1.5:
    - Doesn't perfectly handle multi-line headings or table-formatted clauses.
      Acceptable: the chunk text still contains the full content; only the
      paragraph_anchor may be slightly off in <5% of cases.
    - Roman numerals (i, ii, iii) are not parsed as a third level. v0.5.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class Chunk:
    """One citable paragraph from an MD."""

    document_id: str
    md_id: str
    rbi_ref: str | None
    section: str | None
    paragraph_anchor: str | None  # e.g., "8.2.a"
    page: int | None
    text: str
    char_start: int
    char_end: int

    @property
    def chunk_id(self) -> str:
        """Stable ID that survives reindexing as long as paragraph_anchor is stable."""
        anchor = self.paragraph_anchor or hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:8]
        return f"{self.document_id}#p={anchor}@{self.char_start}"


# Patterns matching numbered headings at line start (after optional whitespace).
# - "1." / "12." → top-level section
# - "1.1" / "8.2" → second-level (Note: RBI sometimes uses "1.1" without period;
#   we match either)
# - "a." / "b)" → letter sub-clause
# - "(1)" / "(10)" → parenthesized sub-clause (RBI uses these inside top-level
#   paragraphs to enumerate items, e.g., "5. In these Directions... (1) ... (2) ...")
TOP_LEVEL_PATTERN = re.compile(r"^\s*(\d{1,2})\.\s+(.+)")
SUB_LEVEL_PATTERN = re.compile(r"^\s*(\d{1,2}\.\d{1,2})\.?\s+(.+)")
LETTER_PATTERN = re.compile(r"^\s*([a-z])[.)]\s+(.+)")
PAREN_NUM_PATTERN = re.compile(r"^\s*\((\d{1,3})\)\s+(.+)")


def chunk_md_text(
    text: str,
    *,
    document_id: str,
    md_id: str,
    rbi_ref: str | None = None,
    min_chunk_chars: int = 80,
    max_chunk_chars: int = 2000,
) -> list[Chunk]:
    """Walk pdftotext output and emit one chunk per numbered paragraph.

    `min_chunk_chars` filters out heading-only chunks (e.g., a "Chapter 1"
    line that's its own paragraph). `max_chunk_chars` splits very long
    paragraphs at sentence boundaries to keep retrieval results readable.
    """
    if not text or not text.strip():
        return []

    pages = text.split("\x0c")
    chunks: list[Chunk] = []

    current_section: str | None = None
    current_anchor: str | None = None
    buffer: list[str] = []
    buffer_anchor: str | None = None
    buffer_section: str | None = None
    buffer_page: int | None = None
    buffer_char_start = 0
    char_offset = 0

    def flush() -> None:
        """Emit the buffered paragraph as a chunk if it's substantial."""
        nonlocal buffer, buffer_anchor, buffer_section, buffer_page, buffer_char_start
        if not buffer:
            return
        body = "\n".join(buffer).strip()
        if len(body) < min_chunk_chars:
            buffer = []
            return
        # Long-paragraph split: chunk further on sentences, keeping the same anchor.
        for sub_text, sub_offset in _split_long(body, max_chunk_chars):
            chunks.append(
                Chunk(
                    document_id=document_id,
                    md_id=md_id,
                    rbi_ref=rbi_ref,
                    section=buffer_section,
                    paragraph_anchor=buffer_anchor,
                    page=buffer_page,
                    text=sub_text,
                    char_start=buffer_char_start + sub_offset,
                    char_end=buffer_char_start + sub_offset + len(sub_text),
                )
            )
        buffer = []

    for page_idx, page_text in enumerate(pages, start=1):
        if not page_text.strip():
            char_offset += len(page_text) + 1  # +1 for the form-feed
            continue

        for line in page_text.splitlines(keepends=True):
            stripped = line.strip()
            if not stripped:
                # Blank line. Don't flush; paragraphs span blank lines often.
                char_offset += len(line)
                continue

            # Skip page-number-only lines (e.g., "12" alone).
            if re.match(r"^\s*\d{1,3}\s*$", line):
                char_offset += len(line)
                continue

            # Detect heading transitions.
            new_anchor: str | None = None
            new_section: str | None = current_section
            sub_m = SUB_LEVEL_PATTERN.match(line)
            top_m = TOP_LEVEL_PATTERN.match(line)
            letter_m = LETTER_PATTERN.match(line)
            paren_m = PAREN_NUM_PATTERN.match(line)

            if sub_m:
                # "8.2 ..." — second-level paragraph.
                new_anchor = sub_m.group(1)
                new_section = new_anchor.split(".")[0]
            elif top_m and not letter_m:
                # "8. ..." — top-level paragraph.
                new_anchor = top_m.group(1)
                new_section = new_anchor
            elif paren_m and current_anchor:
                # "(1) ..." — parenthesized sub-clause under the current
                # paragraph. Like letters, these are siblings of each other,
                # NOT children. Strip any existing trailing parenthesized or
                # letter segment before appending.
                base = re.sub(r"\.\(\d+\)$|\.[a-z]$", "", current_anchor)
                new_anchor = f"{base}.({paren_m.group(1)})"
            elif letter_m and current_anchor:
                # "a. ..." — sub-clause under the current paragraph.
                # Letters are siblings of each other (a, b, c), NOT children.
                # If current_anchor already ends in a single-letter segment
                # (e.g., "4.a") OR a parenthesized number (e.g., "4.(2)"),
                # strip that segment before appending the new letter —
                # otherwise we end up with absurd anchors like "4.a.b.c.d"
                # when the actual structure is just "4.b".
                base = re.sub(r"\.[a-z]$|\.\(\d+\)$", "", current_anchor)
                new_anchor = f"{base}.{letter_m.group(1)}"

            if new_anchor and new_anchor != current_anchor:
                flush()
                buffer_anchor = new_anchor
                buffer_section = new_section
                buffer_page = page_idx
                buffer_char_start = char_offset
                current_anchor = new_anchor
                current_section = new_section

            buffer.append(line.rstrip("\n"))
            char_offset += len(line)

        # Account for the form-feed (\x0c) that separated this page from the
        # next — matching the empty-page branch above. Without it, char offsets
        # drift one byte earlier per page and the stored char_start/char_end
        # citation locators stop mapping back to the source text (get_document
        # reconstructs via ORDER BY char_start).
        char_offset += 1

        # Flush at page end ONLY if we hit a new top-level on the next page.
        # Don't flush here — paragraphs commonly span pages.

    flush()  # Emit the last buffered paragraph.

    logger.info("chunk.ok", document_id=document_id, chunks=len(chunks))
    return chunks


def _split_long(body: str, max_chars: int) -> list[tuple[str, int]]:
    """Split a long paragraph at sentence boundaries, keeping (text, offset) pairs.

    Offsets are relative to the start of `body`. If `body` already fits, returns
    a single (body, 0) pair.
    """
    if len(body) <= max_chars:
        return [(body, 0)]
    result: list[tuple[str, int]] = []
    sentences = re.split(r"(?<=[.!?])\s+", body)
    current = ""
    current_offset = 0
    cursor = 0
    for s in sentences:
        if not s:
            cursor += 1
            continue
        if current and len(current) + len(s) + 1 > max_chars:
            result.append((current.strip(), current_offset))
            current = ""
            current_offset = cursor
        if not current:
            current_offset = cursor
        current = (current + " " + s).strip() if current else s
        cursor += len(s) + 1  # +1 for the split whitespace
    if current.strip():
        result.append((current.strip(), current_offset))
    return result
