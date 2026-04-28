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
import hashlib
import json
import os
import re
import sys
from datetime import datetime
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
from .chunk import chunk_md_text
from .embed import embed_texts, to_sqlite_vec_bytes

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

    # Step 3: chunk.
    document_id = f"rbi:{document_family}:{notif_id}"
    chunks = chunk_md_text(
        body_text,
        document_id=document_id,
        md_id=notif_id,
        rbi_ref=extracted_rbi_ref or rbi_ref,
    )
    if not chunks:
        logger.error("circ_index.chunk.empty", notif_id=notif_id)
        return 1

    # Step 4+5: persist.
    init_db(db_path)
    now = datetime.utcnow().isoformat() + "Z"
    issued_date = pdf_issue_date or issued_date_hint
    title = title_hint or detail.title or f"Circular {rbi_ref or notif_id}"

    with connect(db_path) as conn:
        # Upsert documents row. Title preference:
        #   1. The fresh title we just parsed (title_hint or detail.title) wins
        #      if it's non-empty and not garbage (date string, header label).
        #   2. Otherwise fall back to whatever's in the DB.
        # This is the inverse of the MD-indexer's preservation rule because
        # for circulars, the list-page title is the AUTHORITATIVE source —
        # the detail page's H1 is often just "Notifications".
        existing = conn.execute(
            "SELECT title FROM documents WHERE md_id = ?", (notif_id,)
        ).fetchone()
        new_title_is_garbage = (
            not title
            or _looks_like_date_or_header(title)
        )
        if not new_title_is_garbage:
            kept_title = title
        elif existing and existing["title"] and not _looks_like_date_or_header(existing["title"]):
            kept_title = existing["title"]
        else:
            kept_title = title or f"Circular {rbi_ref or notif_id}"
        pdf_urls = [pdf_url_used] if pdf_url_used else []
        if detail.annexure_pdf_urls:
            pdf_urls.extend(detail.annexure_pdf_urls)

        conn.execute(
            """
            INSERT INTO documents (
                document_id, md_id, title, detail_url, last_updated_at,
                issued_date, rbi_ref, department, document_family,
                pdf_urls_json, status, first_seen_at, last_seen_at, raw_list_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(md_id) DO UPDATE SET
                document_id = excluded.document_id,
                title = excluded.title,
                detail_url = excluded.detail_url,
                last_updated_at = COALESCE(excluded.last_updated_at, documents.last_updated_at),
                issued_date = COALESCE(excluded.issued_date, documents.issued_date),
                rbi_ref = COALESCE(excluded.rbi_ref, documents.rbi_ref),
                document_family = excluded.document_family,
                pdf_urls_json = excluded.pdf_urls_json,
                status = excluded.status,
                last_seen_at = excluded.last_seen_at
            """,
            (
                document_id,
                notif_id,
                kept_title,
                detail.detail_url,
                None,  # last_updated_at — not applicable for circulars usually
                issued_date,
                extracted_rbi_ref or rbi_ref,
                None,  # department
                document_family,
                json.dumps(pdf_urls),
                "current",
                now,
                now,
                None,
            ),
        )

        # Idempotent chunk replacement.
        old_rowids = [
            r[0]
            for r in conn.execute(
                "SELECT rowid FROM chunks WHERE md_id = ?", (notif_id,)
            )
        ]
        if old_rowids:
            placeholders = ",".join("?" for _ in old_rowids)
            try:
                conn.execute(
                    f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})",
                    old_rowids,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("chunks_vec.delete_skip", error=str(exc))
        conn.execute("DELETE FROM chunks WHERE md_id = ?", (notif_id,))

        # Embed.
        try:
            embeddings = embed_texts([c.text for c in chunks])
            embeddings_ok = embeddings.shape[0] == len(chunks)
        except Exception as exc:  # noqa: BLE001
            logger.warning("embed.fail_skip_dense", error=str(exc))
            embeddings = None
            embeddings_ok = False

        for i, c in enumerate(chunks):
            text_sha = hashlib.sha256(c.text.encode("utf-8")).hexdigest()
            cur = conn.execute(
                """
                INSERT INTO chunks (
                    chunk_id, document_id, md_id, rbi_ref, section, paragraph_anchor,
                    page, text, text_sha256, char_start, char_end, pdf_url, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    c.chunk_id,
                    c.document_id,
                    c.md_id,
                    c.rbi_ref,
                    c.section,
                    c.paragraph_anchor,
                    c.page,
                    c.text,
                    text_sha,
                    c.char_start,
                    c.char_end,
                    pdf_url_used,
                    now,
                ),
            )
            if embeddings_ok and embeddings is not None:
                rowid = cur.lastrowid
                try:
                    conn.execute(
                        "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                        (rowid, to_sqlite_vec_bytes(embeddings[i])),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("chunks_vec.insert_fail", error=str(exc))
                    embeddings_ok = False

        # Audit row (only for PDFs; HTML-body extractions skip this).
        if pdf_url_used and pdf_sha:
            conn.execute(
                """
                INSERT INTO pdf_artifacts (
                    pdf_url, document_id, md_id, pdf_sha256, text_sha256,
                    bytes, page_count, extraction_quality, is_indexed,
                    fetched_at, extracted_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
                ON CONFLICT(pdf_url) DO UPDATE SET
                    pdf_sha256 = excluded.pdf_sha256,
                    text_sha256 = excluded.text_sha256,
                    bytes = excluded.bytes,
                    page_count = excluded.page_count,
                    extraction_quality = excluded.extraction_quality,
                    is_indexed = 1,
                    fetched_at = excluded.fetched_at,
                    extracted_at = excluded.extracted_at,
                    error = NULL
                """,
                (
                    pdf_url_used,
                    document_id,
                    notif_id,
                    pdf_sha,
                    hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
                    pdf_bytes,
                    page_count,
                    extraction_quality,
                    now,
                    now,
                ),
            )

    logger.info(
        "circ_index.ok",
        notif_id=notif_id,
        family=document_family,
        chunks=len(chunks),
        source="pdf" if pdf_url_used else "html_body",
    )
    return 0


_DATE_TITLE_PATTERN = re.compile(
    r"^\s*([A-Z][a-z]+\s+\d{1,2},?\s*\d{4}|\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}-\d{1,2}-\d{4})\s*$"
)
_HEADER_LABELS = {
    "circular name/title", "subject", "notifications", "circular number", "ref no.",
}


def _looks_like_date_or_header(s: str | None) -> bool:
    """Heuristic: is this string a date or a column-header label, not a real title?"""
    if not s:
        return True
    if s.lower().strip() in _HEADER_LABELS:
        return True
    return bool(_DATE_TITLE_PATTERN.match(s.strip()))


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
