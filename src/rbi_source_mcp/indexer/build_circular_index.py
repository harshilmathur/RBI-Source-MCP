"""Index a single RBI circular (NotificationUser.aspx?Id=N).

Mirrors build_md_index.py but for the standalone-circular / notification
content family. Key differences:

    - Uses notif_detail.fetch_detail (not md_detail).
    - When the detail page has a PDF, we run the same PDF pipeline as MDs.
    - When the detail page has NO PDF (older or short circulars), we fall
      back to the HTML body text and chunk that.
    - Stores rows under document_family = 'standalone_circular' or
      'notification' depending on the source list.

Run with: rbi-source-index-circular <notif_id> [--family standalone_circular|notification]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import structlog

from ..crawler import notif_detail
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


def index_circular(
    notif_id: str,
    *,
    document_family: str = "standalone_circular",
    rbi_ref: str | None = None,
    title_hint: str | None = None,
    issued_date_hint: str | None = None,
    db_path: Path | None = None,
    pdf_dir: Path | None = None,
) -> int:
    """Index one circular. Returns process exit code (0 = success)."""
    if document_family not in ("standalone_circular", "notification"):
        raise ValueError(f"unsupported document_family: {document_family!r}")

    db_path = Path(db_path or os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)).expanduser().resolve()
    pdf_dir = Path(pdf_dir or db_path.parent / "pdfs").resolve()

    logger.info(
        "circ_index.start",
        notif_id=notif_id,
        family=document_family,
        db=str(db_path),
    )

    # Step 1: fetch detail page.
    try:
        detail = notif_detail.fetch_detail(notif_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("circ_index.detail.fail", notif_id=notif_id, error=str(exc))
        return 1

    # Step 2: pick text source — PDF if attached, else HTML body.
    body_text: str | None = None
    pdf_sha = None
    pdf_bytes = 0
    page_count = 1
    extraction_quality = 1.0
    pdf_url_used: str | None = None
    extracted_rbi_ref: str | None = None
    pdf_issue_date: str | None = None

    if detail.primary_pdf_url:
        pdf_result = fetch_pdf(
            detail.primary_pdf_url,
            referer=detail.detail_url,
            out_dir=pdf_dir,
        )
        if not pdf_result.is_pdf:
            logger.warning(
                "circ_index.pdf_fetch.fail_fallback_html",
                notif_id=notif_id,
                error=pdf_result.error,
            )
            # Fall through to HTML body if available.
        else:
            extraction = extract_text(pdf_result.local_path)
            if extraction.passes_quality_gate:
                body_text = extraction.text
                pdf_sha = pdf_result.pdf_sha256
                pdf_bytes = pdf_result.bytes
                page_count = extraction.page_count
                extraction_quality = extraction.extraction_quality
                pdf_url_used = detail.primary_pdf_url
                extracted_rbi_ref = parse_rbi_ref_from_pdf_text(body_text)
                pdf_issue_date = parse_issue_date_from_pdf_text(body_text)
            else:
                logger.warning(
                    "circ_index.extract.low_quality_fallback_html",
                    notif_id=notif_id,
                    quality=extraction.extraction_quality,
                )

    if body_text is None and detail.html_body_text:
        # HTML-body fallback for circulars without a PDF attachment.
        body_text = detail.html_body_text

    if not body_text:
        logger.error("circ_index.no_body", notif_id=notif_id)
        return 1

    # Step 3+4+5: chunk + persist + audit via the shared helper. The helper
    # owns: chunk_md_text invocation, title-quality fallback (matches the
    # circular rule — prefer the freshly-parsed title unless garbage, else
    # the existing DB title, else a synthesized placeholder), idempotent
    # chunk replacement scoped by document_id, batched embedding, chunks_vec
    # sync, and pdf_artifacts audit (when a real PDF was used).
    document_id = f"rbi:{document_family}:{notif_id}"
    issued_date = pdf_issue_date or issued_date_hint
    title = title_hint or detail.title or f"Circular {rbi_ref or notif_id}"
    pdf_urls = [pdf_url_used] if pdf_url_used else []
    if detail.annexure_pdf_urls:
        pdf_urls.extend(detail.annexure_pdf_urls)

    pdf_artifact: dict | None = None
    if pdf_url_used and pdf_sha:
        pdf_artifact = {
            "pdf_url": pdf_url_used,
            "sha256": pdf_sha,
            "bytes": pdf_bytes,
            "page_count": page_count,
            "quality": extraction_quality,
        }

    init_db(db_path)
    with connect(db_path) as conn:
        n = persist_document_and_chunks(
            conn,
            document_id=document_id,
            md_id=notif_id,
            title=title,
            detail_url=detail.detail_url,
            document_family=document_family,
            body_text=body_text,
            rbi_ref=extracted_rbi_ref or rbi_ref,
            issued_date=issued_date,
            pdf_urls=pdf_urls,
            pdf_artifact=pdf_artifact,
            status="current",
        )
        if n == 0:
            logger.error("circ_index.chunk.empty", notif_id=notif_id)
            return 1

    logger.info(
        "circ_index.ok",
        notif_id=notif_id,
        family=document_family,
        chunks=n,
        source="pdf" if pdf_url_used else "html_body",
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index a single RBI circular (NotificationUser.aspx?Id=N).",
    )
    parser.add_argument("notif_id", help="Numeric notification ID")
    parser.add_argument(
        "--family",
        default="standalone_circular",
        choices=["standalone_circular", "notification"],
        help="document_family to record",
    )
    parser.add_argument("--rbi-ref", default=None, help="RBI reference (hint, parsed from PDF when present)")
    parser.add_argument("--title", default=None, help="Title hint (uses page <h1> if not provided)")
    parser.add_argument("--issued-date", default=None, help="Issued date hint (ISO 8601)")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    sys.exit(
        index_circular(
            args.notif_id,
            document_family=args.family,
            rbi_ref=args.rbi_ref,
            title_hint=args.title,
            issued_date_hint=args.issued_date,
            db_path=Path(args.db) if args.db else None,
        )
    )


if __name__ == "__main__":
    main()
