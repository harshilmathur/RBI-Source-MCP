"""Shared 'write a document + chunks + embeddings' helper.

Extracted so the per-family indexers (MD, circular, press-release, FAQ,
master-circular) don't each re-implement the same upsert + embed + insert
pattern. Each family-specific indexer just supplies:
    - body_text (assembled from PDF or HTML)
    - title, document_id, md_id, document_family, detail_url, etc.
    - optional pdf_url + audit metadata

Returns the count of chunks inserted.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Any

import structlog

from .chunk import chunk_md_text
from .embed import embed_texts, to_sqlite_vec_bytes

logger = structlog.get_logger(__name__)

_DATE_TITLE_PATTERN = re.compile(
    r"^\s*([A-Z][a-z]+\s+\d{1,2},?\s*\d{4}|\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}-\d{1,2}-\d{4})\s*$"
)
_HEADER_LABELS = {
    "circular name/title", "subject", "notifications", "circular number", "ref no.",
}


def looks_like_date_or_header(s: str | None) -> bool:
    if not s:
        return True
    if s.lower().strip() in _HEADER_LABELS:
        return True
    return bool(_DATE_TITLE_PATTERN.match(s.strip()))


def persist_document_and_chunks(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    md_id: str,           # numeric or hash-derived stable ID
    title: str,
    detail_url: str,
    document_family: str,
    body_text: str,
    rbi_ref: str | None = None,
    issued_date: str | None = None,
    last_updated_at: str | None = None,
    department: str | None = None,
    pdf_urls: list[str] | None = None,
    pdf_artifact: dict[str, Any] | None = None,  # {pdf_url, sha256, bytes, page_count, quality}
    status: str = "current",
    force: bool = False,
) -> int:
    """Upsert a documents row, replace its chunks, and insert embeddings.

    Returns the number of chunks inserted. Caller is responsible for the
    surrounding `with connect()` context.

    When `force=False` (the default), a content-hash skip-fast path runs
    BEFORE the delete + re-embed loop: if `pdf_artifacts.text_sha256`
    already matches `sha256(body_text)` for this document AND chunks
    exist, we touch `documents.last_seen_at` and return the existing
    chunk count without re-embedding. This saves the ~50-200ms-per-chunk
    embedder cost on every unchanged document during the daily refresh.
    Set `force=True` to bypass (used by the monthly full-rebuild).
    """
    now = datetime.utcnow().isoformat() + "Z"
    pdf_urls = pdf_urls or []

    # Content-hash skip-fast: if body_text hasn't changed since last index,
    # avoid the chunk delete + embed + insert loop entirely. We key on
    # text_sha256 in pdf_artifacts because that's where the prior text
    # hash is stamped; falls back through gracefully when no prior row.
    if not force:
        body_sha = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        prior_row = conn.execute(
            """
            SELECT text_sha256, (
                SELECT COUNT(*) FROM chunks WHERE document_id = ?
            ) AS chunk_count
            FROM pdf_artifacts
            WHERE document_id = ?
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (document_id, document_id),
        ).fetchone()
        if (
            prior_row is not None
            and prior_row["text_sha256"] == body_sha
            and prior_row["chunk_count"] > 0
        ):
            # Touch last_seen_at so corpus-meta freshness checks still update.
            conn.execute(
                "UPDATE documents SET last_seen_at = ? WHERE document_id = ?",
                (now, document_id),
            )
            logger.info(
                "persist.skip.content_unchanged",
                document_id=document_id,
                chunks=prior_row["chunk_count"],
            )
            return int(prior_row["chunk_count"])

    chunks = chunk_md_text(body_text, document_id=document_id, md_id=md_id, rbi_ref=rbi_ref)
    if not chunks:
        logger.error("persist.chunk.empty", document_id=document_id)
        return 0

    # Title quality fallback: prefer non-garbage new title; else keep existing
    # if also non-garbage; else use a placeholder. Look up by (md_id, family)
    # since md_id alone isn't unique across families.
    existing = conn.execute(
        "SELECT title FROM documents WHERE md_id = ? AND document_family = ?",
        (md_id, document_family),
    ).fetchone()
    if not looks_like_date_or_header(title):
        kept_title = title
    elif existing and existing["title"] and not looks_like_date_or_header(existing["title"]):
        kept_title = existing["title"]
    else:
        kept_title = title or f"{document_family.replace('_', ' ').title()} {rbi_ref or md_id}"

    conn.execute(
        """
        INSERT INTO documents (
            document_id, md_id, title, detail_url, last_updated_at,
            issued_date, rbi_ref, department, document_family,
            pdf_urls_json, status, first_seen_at, last_seen_at, raw_list_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(md_id, document_family) DO UPDATE SET
            document_id = excluded.document_id,
            title = excluded.title,
            detail_url = excluded.detail_url,
            last_updated_at = COALESCE(excluded.last_updated_at, documents.last_updated_at),
            issued_date = COALESCE(excluded.issued_date, documents.issued_date),
            rbi_ref = COALESCE(excluded.rbi_ref, documents.rbi_ref),
            department = COALESCE(excluded.department, documents.department),
            document_family = excluded.document_family,
            pdf_urls_json = excluded.pdf_urls_json,
            status = excluded.status,
            last_seen_at = excluded.last_seen_at
        """,
        (
            document_id,
            md_id,
            kept_title,
            detail_url,
            last_updated_at,
            issued_date,
            rbi_ref,
            department,
            document_family,
            json.dumps(pdf_urls),
            status,
            now,
            now,
            None,
        ),
    )

    # Idempotent chunk + vector replacement. Scope by document_id rather than
    # md_id alone — md_id is unique only within a family, so deleting by
    # md_id would wipe a sibling family's chunks if they share the same
    # numeric ID.
    old_rowids = [
        r[0] for r in conn.execute(
            "SELECT rowid FROM chunks WHERE document_id = ?", (document_id,)
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
            logger.warning("persist.chunks_vec.delete_skip", error=str(exc))
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))

    try:
        embeddings = embed_texts([c.text for c in chunks])
        embeddings_ok = embeddings.shape[0] == len(chunks)
    except Exception as exc:  # noqa: BLE001
        logger.warning("persist.embed.fail_skip_dense", error=str(exc))
        embeddings = None
        embeddings_ok = False

    pdf_url_for_chunks = pdf_artifact.get("pdf_url") if pdf_artifact else None
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
                pdf_url_for_chunks,
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
                logger.warning("persist.chunks_vec.insert_fail", error=str(exc))
                embeddings_ok = False

    if pdf_artifact:
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
                pdf_artifact["pdf_url"],
                document_id,
                md_id,
                pdf_artifact.get("sha256"),
                hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
                pdf_artifact.get("bytes", 0),
                pdf_artifact.get("page_count", 1),
                pdf_artifact.get("quality", 1.0),
                now,
                now,
            ),
        )

    return len(chunks)
