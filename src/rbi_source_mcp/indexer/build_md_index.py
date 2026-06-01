"""End-to-end indexer for a single Master Direction.

For an md_id, this script:
    1. Fetches the detail page (BS_ViewMasDirections.aspx?id=<md_id>).
    2. Locates the primary PDF URL.
    3. Fetches the PDF (with browser headers + Referer to bypass rbidocs WAF).
    4. Extracts text via pdftotext.
    5. Applies the v0.1.5 quality gate.
    6. Chunks the text by numbered paragraph.
    7. Inserts chunks into the SQLite (which auto-populates the FTS5 index).
    8. Records audit rows in pdf_artifacts.

Run with: `python -m rbi_source_mcp.indexer.build_md_index <md_id>`
or:       `rbi-source-index <md_id>` (after `pip install -e .`)

Subsequent runs are idempotent (chunks are re-inserted with the same chunk_id).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import structlog

from ..crawler import md_detail
from ..crawler.pdf_fetch import fetch_pdf
from ..db import connect, init_db
from ..extractor.pdf import (
    extract_text,
    parse_issue_date_from_pdf_text,
    parse_rbi_ref_from_pdf_text,
)
from .persist import persist_document_and_chunks

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"


def index_md(
    md_id: str,
    *,
    db_path: Path | None = None,
    pdf_dir: Path | None = None,
) -> int:
    """Index one Master Direction. Returns process exit code (0 = success)."""
    db_path = Path(db_path or os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)).expanduser().resolve()
    pdf_dir = Path(pdf_dir or db_path.parent / "pdfs").resolve()

    logger.info("index.start", md_id=md_id, db=str(db_path), pdf_dir=str(pdf_dir))

    # Step 1+2: detail page and primary PDF URL.
    try:
        detail = md_detail.fetch_detail(md_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("index.detail.fail", md_id=md_id, error=str(exc))
        return 1
    if not detail.primary_pdf_url:
        logger.error("index.no_primary_pdf", md_id=md_id)
        return 1

    # Step 3: PDF fetch with WAF bypass.
    pdf_result = fetch_pdf(
        detail.primary_pdf_url,
        referer=detail.detail_url,
        out_dir=pdf_dir,
    )
    if not pdf_result.is_pdf:
        logger.error("index.pdf_fetch.fail", md_id=md_id, error=pdf_result.error)
        return 1

    # Step 4+5: text extraction + quality gate.
    extraction = extract_text(pdf_result.local_path)
    if not extraction.passes_quality_gate:
        logger.error(
            "index.extract.fail_quality",
            md_id=md_id,
            quality=extraction.extraction_quality,
            error=extraction.error,
            pages=extraction.page_count,
        )
        return 1

    rbi_ref = parse_rbi_ref_from_pdf_text(extraction.text)
    pdf_issue_date = parse_issue_date_from_pdf_text(extraction.text)
    document_id = f"rbi:master_direction:{md_id}"

    # Date precedence: detail-page "Updated as on" stamp > PDF body issue
    # date > whatever the list crawler captured. The PDF date is the most
    # reliable signal when the detail page doesn't carry an explicit
    # "Updated as on" (common for never-amended MDs).
    last_updated_at = detail.updated_as_on or pdf_issue_date

    # Step 6+7+8: chunk + persist + audit. The shared helper handles
    # idempotent upsert (compound key (md_id, document_family)), batched
    # embedding, chunks_vec sync, and pdf_artifacts audit. We pre-resolve
    # the title here because MDs need a specific fallback: keep the
    # list-crawler title if it exists (it carries the "Master Direction
    # on X (Updated as on Y)" string), else strip from the detail page's
    # raw HTML (the static H1 "Master Directions" is useless on its own).
    init_db(db_path)
    with connect(db_path) as conn:
        existing = conn.execute(
            "SELECT title FROM documents WHERE md_id = ? AND document_family = 'master_direction'",
            (md_id,),
        ).fetchone()
        title = (
            existing["title"]
            if existing and existing["title"]
            else _strip_title_html(detail.raw_html)
        )

        n = persist_document_and_chunks(
            conn,
            document_id=document_id,
            md_id=md_id,
            title=title,
            detail_url=detail.detail_url,
            document_family="master_direction",
            body_text=extraction.text,
            rbi_ref=rbi_ref,
            last_updated_at=last_updated_at,
            pdf_urls=[detail.primary_pdf_url] + detail.annexure_pdf_urls,
            pdf_artifact={
                "pdf_url": detail.primary_pdf_url,
                "sha256": pdf_result.pdf_sha256,
                "bytes": pdf_result.bytes,
                "page_count": extraction.page_count,
                "quality": extraction.extraction_quality,
            },
            status="current",
        )
        if n == 0:
            logger.error("index.chunk.empty", md_id=md_id)
            return 1

    logger.info(
        "index.ok",
        md_id=md_id,
        chunks=n,
        pages=extraction.page_count,
        quality=round(extraction.extraction_quality, 3),
    )
    return 0


def _strip_title_html(html: str) -> str:
    """Best-effort title from the detail page <h1> or <title>."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in ("h1", "title"):
        el = soup.find(tag)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)[:300]
    return "Master Direction"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index a single Master Direction (fetch PDF, extract, chunk, store).",
    )
    parser.add_argument("md_id", help="Numeric MD ID (from BS_ViewMasDirections.aspx?id=)")
    parser.add_argument("--db", default=None, help="SQLite path (default: $RBI_SOURCE_DB or ./data/db.sqlite)")
    args = parser.parse_args()
    sys.exit(index_md(args.md_id, db_path=Path(args.db) if args.db else None))


if __name__ == "__main__":
    main()
