"""rbi.get_document — fetch a Master Direction's metadata + assembled text.

v0.1.5 returns:
    - Document metadata from `documents` table (title, URL, last_updated_at, status)
    - List of chunk-level "table of contents" entries (anchor, section, page)
    - Optionally the full assembled text (concatenated chunks in order) when
      `include_text=True`. Default is metadata-only to keep response size sane
      for MCP consumers; the LLM can request `include_text=True` when it
      wants the full body.

`as_of` parameter is reserved for v1.1 (version selection); at v0.1.5 we
always return current state.

Amendment chain field ships at v1.1.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any


def get_document(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    include_text: bool = False,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Fetch a document by canonical ID. Always returns a structured envelope."""
    now = datetime.utcnow().isoformat() + "Z"
    response: dict[str, Any] = {
        "document_id": document_id,
        "as_of": now,
        "as_of_requested": as_of,
        "tool": "rbi.get_document",
    }

    if not document_id:
        response["status"] = "error"
        response["error"] = "missing_document_id"
        return response

    if as_of:
        response["caveat"] = (
            "as_of parameter accepted but not honored at v0.1.5; ships at v1.1. "
            "Returning current version."
        )

    cur = conn.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,))
    doc = cur.fetchone()
    if doc is None:
        response["status"] = "unknown"
        response["error"] = "document_not_in_corpus"
        response["message"] = (
            f"No document found with id={document_id!r}. v0.1.5 corpus is limited "
            "to indexed Master Directions; broader coverage at v0.5+."
        )
        return response

    response["status"] = doc["status"] or "current"
    response["title"] = doc["title"]
    response["official_url"] = doc["detail_url"]
    response["last_updated_at"] = doc["last_updated_at"]
    response["pdf_urls_json"] = doc["pdf_urls_json"]

    # Pull chunks for the table-of-contents view.
    chunks_cur = conn.execute(
        """
        SELECT chunk_id, section, paragraph_anchor, page, char_start, char_end,
               substr(text, 1, 80) AS preview
        FROM chunks
        WHERE document_id = ?
        ORDER BY char_start ASC
        """,
        (document_id,),
    )
    chunks = chunks_cur.fetchall()
    response["chunk_count"] = len(chunks)
    response["table_of_contents"] = [
        {
            "chunk_id": c["chunk_id"],
            "section": c["section"],
            "paragraph_anchor": c["paragraph_anchor"],
            "page": c["page"],
            "preview": c["preview"],
        }
        for c in chunks
    ]

    if include_text:
        text_cur = conn.execute(
            "SELECT text FROM chunks WHERE document_id = ? ORDER BY char_start ASC",
            (document_id,),
        )
        response["text"] = "\n\n".join(row["text"] for row in text_cur)

    return response
