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

# Topic hint → md_id mapping. Mirrors check_compliance.py; keep them in sync.
_TOPIC_TO_MD_ID: dict[str, str | None] = {
    "payment_aggregator": "12896",
    "pa": "12896",
    "pa_pg": "12896",
    "kyc_bank": "13141",
    "kyc_commercial_bank": "13141",
    "bank_kyc": "13141",
    "kyc_nbfc": "12943",
    "nbfc_kyc": "12943",
    "ppi": "12156",
    "prepaid": "12156",
    "prepaid_payment_instrument": "12156",
    "wallet": "12156",
    "cards": "13155",
    "credit_card": "13155",
    "debit_card": "13155",
    "tokenisation": "13155",
    "tokenization": "13155",
    "e_mandate": "13374",
    "emandate": "13374",
    "recurring": "13374",
    "recurring_payment": "13374",
    "digital_payment_security": "12032",
    "payment_security": "12032",
    "afa": "12032",
    "additional_factor_authentication": "12032",
    "kyc": None,  # ambiguous; spans both bank + NBFC KYC MDs
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
