"""PDF text extraction via `pdftotext` (poppler-utils).

v0.1.5 ships with the simplest possible quality gate: extraction succeeded
if the resulting text is non-empty and at least 80% of pages produced text.
The richer quality gate from /plan-eng-review (≥90% pages text-extractable
+ ≥3 paragraphs/page) lives in v0.5 alongside the multi-MD pipeline.

Output is a `PdfExtraction` carrying the full text, a sha256 hash, the page
count, and the extraction quality score. Chunking happens in `indexer.chunk`,
not here.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

PDFTOTEXT = shutil.which("pdftotext")


@dataclass(slots=True)
class PdfExtraction:
    """Result of extracting text from a PDF."""

    pdf_path: Path
    text: str
    text_sha256: str
    page_count: int
    pages_with_text: int
    extraction_quality: float  # pages_with_text / page_count, 0.0-1.0
    error: str | None = None

    @property
    def passes_quality_gate(self) -> bool:
        # v0.1.5 minimum: non-empty AND ≥80% pages have text.
        return self.error is None and self.text.strip() != "" and self.extraction_quality >= 0.8


def extract_text(pdf_path: Path | str, *, layout: bool = True) -> PdfExtraction:
    """Run pdftotext and return the extracted text + quality metrics.

    `layout=True` preserves the visual layout (column structure, indentation),
    which the chunker uses to detect numbered section headings. Set False for
    titled-only docs where layout-preservation produces awkward line breaks.
    """
    pdf_path = Path(pdf_path)
    if not PDFTOTEXT:
        return PdfExtraction(
            pdf_path=pdf_path,
            text="",
            text_sha256="",
            page_count=0,
            pages_with_text=0,
            extraction_quality=0.0,
            error="pdftotext binary not found on PATH (install poppler-utils)",
        )
    if not pdf_path.exists():
        return PdfExtraction(
            pdf_path=pdf_path,
            text="",
            text_sha256="",
            page_count=0,
            pages_with_text=0,
            extraction_quality=0.0,
            error=f"PDF not found at {pdf_path}",
        )

    args = [PDFTOTEXT]
    if layout:
        args.append("-layout")
    args.extend([str(pdf_path), "-"])
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return PdfExtraction(
            pdf_path=pdf_path,
            text="",
            text_sha256="",
            page_count=0,
            pages_with_text=0,
            extraction_quality=0.0,
            error="pdftotext timed out after 120s",
        )

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        return PdfExtraction(
            pdf_path=pdf_path,
            text="",
            text_sha256="",
            page_count=0,
            pages_with_text=0,
            extraction_quality=0.0,
            error=f"pdftotext exit {result.returncode}: {stderr}",
        )

    text = result.stdout.decode("utf-8", errors="replace")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # Page count: pdftotext emits a form-feed (\x0c) between pages.
    pages = text.split("\x0c")
    page_count = max(1, len(pages) - 1)  # The trailing split element is empty.
    pages_with_text = sum(1 for p in pages if p.strip())
    quality = pages_with_text / page_count if page_count else 0.0

    logger.info(
        "pdf_extract.ok",
        path=str(pdf_path),
        bytes=len(text),
        page_count=page_count,
        pages_with_text=pages_with_text,
        quality=round(quality, 3),
    )
    return PdfExtraction(
        pdf_path=pdf_path,
        text=text,
        text_sha256=sha,
        page_count=page_count,
        pages_with_text=pages_with_text,
        extraction_quality=quality,
    )


# ---------------------------------------------------------------------------
# Helpers (used by the chunker; here so they can be tested in isolation)
# ---------------------------------------------------------------------------


def parse_rbi_ref_from_pdf_text(text: str) -> str | None:
    """Find the RBI reference at the top of an MD PDF.

    Format: "RBI/<DEPT>/<YEAR-YEAR>/<NUMBER>" or "RBI/<YEAR-YEAR>/<NUMBER>".
    Example: "RBI/DPSS/2025-26/141" or "RBI/2024-25/108".
    """
    # Look in the first ~3000 chars (header area).
    head = text[:3000]
    m = re.search(r"\bRBI\s*/\s*[A-Za-z]*\s*/?\s*\d{4}\s*[-–]\s*\d{2,4}\s*/\s*\d+\b", head)
    if not m:
        return None
    return re.sub(r"\s+", "", m.group(0))


from datetime import datetime  # noqa: E402  (kept module-local for date parsing)


def parse_issue_date_from_pdf_text(text: str) -> str | None:
    """Find the issue date in the header of an MD PDF.

    RBI MDs lay out the header like:

        RBI/DPSS/2025-26/141
        CO.DPSS.POLC.No.S-633/02-14-008/2025-26       September 15, 2025

        All Payment System Providers ...

    The date appears on the same line as (or immediately after) the secondary
    reference number, in one of several formats. Strategy:
        1. Look only in the first ~2000 chars (the header band).
        2. Scan for known date formats; first hit wins.
        3. Skip dates that look like part of a reference number (e.g.,
           "2025-26" on its own).

    Returns ISO 8601 (YYYY-MM-DD) or None.
    """
    head = text[:2000]
    pairs: list[tuple[str, list[str]]] = [
        # "September 15, 2025" / "Sep 15, 2025"
        (r"\b([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})\b", ["%B %d, %Y", "%b %d, %Y"]),
        # "15 September 2025" / "15 Sep 2025"
        (r"\b(\d{1,2})\s+([A-Z][a-z]+)\s+(\d{4})\b", ["%d %B %Y", "%d %b %Y"]),
        # "15/09/2025" / "15-09-2025"
        (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", ["%d/%m/%Y"]),
        (r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", ["%d-%m-%Y"]),
    ]
    for pattern, formats in pairs:
        for m in re.finditer(pattern, head):
            for fmt in formats:
                try:
                    dt = datetime.strptime(m.group(0), fmt)
                except ValueError:
                    continue
                # Sanity: RBI MDs are dated 1990-onwards; reject obvious
                # noise (a "2026" embedded in a phrase like "annexure 2026"
                # plus a stray "1" elsewhere wouldn't form a valid date,
                # but range-check just in case).
                if 1990 <= dt.year <= datetime.now().year + 1:
                    return dt.date().isoformat()
                break
    return None


def split_pages(text: str) -> list[str]:
    """Split pdftotext-with-layout output by form-feed page separators."""
    return text.split("\x0c")
