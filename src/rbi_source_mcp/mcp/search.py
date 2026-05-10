"""rbi_search — direct hybrid retrieval over MD chunks.

This is the same engine `check_compliance` uses internally, exposed for the
"what rules apply to <topic>" workflow where the user has a clean keyword
query, not a paste-the-clause flow.

v0.5: hybrid (FTS5 + sqlite-vec dense, RRF fusion). Falls back to FTS5-only
when the embedder is unavailable.

Differences from check_compliance:
    - Accepts a `query` instead of free text.
    - No low_confidence signal — direct queries are assumed intentional.
    - Filters: topic, regulated_entity (reserved), include_withdrawn, as_of_date.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import structlog

from ..db import escape_fts5_query, hybrid_search

logger = structlog.get_logger(__name__)


def _classify_embed_error(exc: BaseException) -> str:
    """Map an embedder failure to a short, non-leaky reason code.

    Returned codes are stable and free of any path / account / proxy info
    that exception messages typically carry. The full exception (with
    `str(exc)`) is logged via structlog for the operator; only the code
    flows into the LLM-visible response. Codex/red-team review caught the
    raw-`str(exc)` echo as an info-disclosure path.
    """
    name = type(exc).__name__
    if isinstance(exc, ImportError):
        return "embedder_unavailable"
    if isinstance(exc, (FileNotFoundError, IsADirectoryError)):
        return "model_not_downloaded"
    if isinstance(exc, ValueError):
        # to_sqlite_vec_bytes raises ValueError on dim mismatch
        return "dim_mismatch"
    if name in {
        "ConnectionError", "ConnectError", "ReadTimeout", "WriteTimeout",
        "TimeoutException", "HTTPError", "HTTPStatusError",
    }:
        return "cf_api_error"
    if isinstance(exc, OSError):
        # Covers PermissionError, model-cache I/O, etc. that aren't caught above
        return "model_io_error"
    return "embedder_error"


def _embed_query_safe(raw: str) -> tuple[bytes | None, str | None]:
    """Compute the dense query embedding, returning (bytes, None) on success
    or (None, short_reason_code) on failure.

    The reason code is one of a fixed set (see `_classify_embed_error`) so the
    LLM-visible `_retrieval_warning` field never carries filesystem paths,
    account slugs, or proxy URLs from raw exception messages. Full diagnostic
    detail goes to structlog for the operator to read in logs.
    """
    try:
        from ..indexer.embed import embed_query, to_sqlite_vec_bytes

        return to_sqlite_vec_bytes(embed_query(raw)), None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "embed_query.failed",
            exc_type=type(exc).__name__,
            error=str(exc),
        )
        return None, _classify_embed_error(exc)

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
            "v0.5 hybrid retrieval (FTS5 sparse + sqlite-vec dense, RRF fused). "
            "Corpus limited to indexed Master Directions."
        ),
        "tool": "rbi_search",
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

    # Best-effort dense embedding for hybrid fusion. If this fails, capture
    # the reason and surface it to the caller via _retrieval_warning so the
    # LLM knows recall is degraded — without this, a misconfigured corpus
    # (dim mismatch, missing model, no internet for HF download) silently
    # degrades to FTS5-only and the user has no signal that anything is
    # wrong. Codex review caught the silent-degrade gap.
    query_embedding, embed_error = _embed_query_safe(raw)

    rows = hybrid_search(
        conn,
        fts_query,
        query_embedding,
        limit=limit,
        md_id=md_id_filter,
        include_withdrawn=include_withdrawn,
    )

    response["retrieval"] = "hybrid" if query_embedding is not None else "sparse_only"
    if embed_error is not None:
        response["_retrieval_warning"] = (
            "Dense retrieval is unavailable — falling back to FTS5 keyword "
            f"matching only. Reason: {embed_error}. Recall may be degraded; "
            "run `rbi-source-doctor` to diagnose. Full error logged on the server."
        )
        response["_retrieval_warning_code"] = embed_error
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
            "bm25_score": r.get("bm25_score"),
            "vec_distance": r.get("vec_distance"),
            "fusion_score": r.get("fusion_score"),
        }
        for r in rows
    ]
    return response
