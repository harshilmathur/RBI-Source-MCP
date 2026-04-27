"""rbi.search — direct hybrid (FTS5-only at v0.1.5) retrieval over MD chunks.

This is the same engine `check_compliance` uses internally, exposed for the
"what rules apply to <topic>" workflow where the user has a clean keyword
query, not a paste-the-clause flow.

Differences from check_compliance:
    - Accepts a `query` instead of free text. Query is treated as keyword-y;
      we still escape it but don't aggressively quote.
    - Returns chunks ranked by BM25 only (no low_confidence signal).
    - Filters: topic, regulated_entity (reserved), include_withdrawn, as_of_date.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from ..db import escape_fts5_query, search_chunks_fts

_TOPIC_TO_MD_ID: dict[str, str] = {
    "payment_aggregator": "12896",
    "pa": "12896",
    "pa_pg": "12896",
}


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    filters: dict[str, Any] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Hybrid (FTS5-only at v0.1.5) retrieval. Always returns a structured envelope."""
    now = datetime.utcnow().isoformat() + "Z"
    filters = filters or {}
    raw = (query or "").strip()

    response: dict[str, Any] = {
        "results": [],
        "query": raw,
        "filters": filters,
        "as_of": now,
        "caveat": (
            "v0.1.5: FTS5-only sparse retrieval over a single Master Direction. "
            "Dense (sqlite-vec) hybrid ships at v0.5."
        ),
        "tool": "rbi.search",
    }

    if not raw:
        response["message"] = "Empty query."
        return response

    fts_query = escape_fts5_query(raw)
    if fts_query == '""':
        response["message"] = "Query has no searchable tokens."
        return response

    topic = filters.get("topic")
    md_id_filter = _TOPIC_TO_MD_ID.get(topic.lower()) if isinstance(topic, str) else None
    include_withdrawn = bool(filters.get("include_withdrawn", False))

    rows = search_chunks_fts(
        conn,
        fts_query,
        limit=limit,
        md_id=md_id_filter,
        include_withdrawn=include_withdrawn,
    )

    response["results"] = [
        {
            "document_id": r["document_id"],
            "title": r["document_title"],
            "rbi_ref": r["rbi_ref"],
            "section": r["section"],
            "paragraph_anchor": r["paragraph_anchor"],
            "page": r["page"],
            "text": r["text"],
            "official_url": r["document_url"],
            "pdf_url": r["pdf_url"],
            "status": r["status"] or "current",
            "last_updated_at": r["last_updated_at"],
            "bm25_score": r["bm25_score"],
        }
        for r in rows
    ]
    return response
