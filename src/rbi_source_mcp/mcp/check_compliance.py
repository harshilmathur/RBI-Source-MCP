"""rbi_check_compliance — the v2 headline tool.

Takes free text (a clause, PRD section, draft policy paragraph) and returns
ranked relevant Master Direction provisions with citations. **Retrieval-only
by design** — never emits a heuristic compliance verdict. The LLM consuming
this synthesizes the call; the user makes the decision.

This separation is locked in the design doc's "Why retrieval-only" section.
The MCP stays defensibly on the source-layer side of the line; we are not
an unauthorized regulatory advisor.

v0.5: hybrid retrieval (FTS5 sparse + sqlite-vec dense via bge-small-en-v1.5,
fused with RRF). Falls back to FTS5-only when dense embeddings are
unavailable for a given query (e.g., model load failure).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .._time import iso_utc_now

# Reuse the shared safe-embed helper from mcp/search.py — both tools need
# identical reason-code mapping so a degraded retrieval path looks the
# same regardless of which tool the LLM called. The previous duplication
# rationale ("circular import") was wrong; mcp/search → indexer.embed,
# this module → mcp/search → indexer.embed has no cycle.
from ..db import escape_fts5_query, hybrid_search
from .search import _embed_query_safe

# Topic-hint table lives in mcp/topics.py so search.py and check_compliance.py
# share one source. The legacy private alias is preserved for any external
# imports that touched it.
from .topics import md_id_for_topic_hint as _md_id_for_topic_hint


def check_compliance(
    conn: sqlite3.Connection,
    text: str,
    *,
    topic_hint: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Return ranked relevant provisions for `text`.

    Response shape:

        {
            "relevant_provisions": [
                {
                    "document_id": "rbi:master_direction:12896",
                    "title": "Master Direction on Regulation of Payment Aggregator (PA)",
                    "rbi_ref": "RBI/DPSS/2025-26/141",
                    "paragraph_anchor": "8.2",
                    "section": "8",
                    "page": 6,
                    "quoted_text": "...",
                    "official_url": "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12896",
                    "pdf_url": "https://rbidocs.rbi.org.in/...",
                    "status": "current",
                    "last_updated_at": "2025-09-15",
                    "bm25_score": -3.2,           # FTS5: more negative = more relevant
                },
                ...
            ],
            "withdrawn_provisions_excluded": 0,
            "low_confidence": false,
            "as_of": "2026-04-27T...",
            "caveat": "v0.1.5 corpus = single MD. ...",
            "topic_hint": "payment_aggregator" or null,
            "input_text_preview": "..." (first 120 chars)
        }

    The function NEVER raises and NEVER silently fails. Empty/garbage input
    returns a well-formed envelope with `relevant_provisions: []` and
    `low_confidence: true`.
    """
    now = iso_utc_now()
    raw = (text or "").strip()

    response: dict[str, Any] = {
        "relevant_provisions": [],
        "withdrawn_provisions_excluded": 0,
        "low_confidence": False,
        "as_of": now,
        "caveat": _CAVEAT,
        "topic_hint": topic_hint,
        "input_text_preview": raw[:120],
        "tool": "rbi_check_compliance",
        "not_legal_advice": True,
    }

    if not raw:
        response["low_confidence"] = True
        response["message"] = "Empty input. Pass a clause, PRD section, or policy text."
        return response

    fts_query = escape_fts5_query(raw)
    if fts_query == '""':
        response["low_confidence"] = True
        response["message"] = "Input has no searchable tokens (only punctuation?)."
        return response

    md_id_filter = _md_id_for_topic_hint(topic_hint)

    # Compute the dense query embedding. Best-effort: if the embedder fails
    # (model not downloaded, dim mismatch, no internet), capture the reason
    # and surface it via _retrieval_warning so the LLM knows recall is
    # degraded. Codex review caught the silent-degrade gap.
    query_embedding, embed_error = _embed_query_safe(raw)

    rows = hybrid_search(
        conn,
        fts_query,
        query_embedding,
        limit=limit,
        md_id=md_id_filter,
        include_withdrawn=False,
    )

    provisions = [_row_to_provision(r) for r in rows]
    response["relevant_provisions"] = provisions
    response["retrieval"] = "hybrid" if query_embedding is not None else "sparse_only"
    if embed_error is not None:
        response["_retrieval_warning"] = (
            "Dense retrieval is unavailable — falling back to FTS5 keyword "
            f"matching only. Reason: {embed_error}. Recall may be degraded; "
            "run `rbi-source-doctor` to diagnose. Full error logged on the server."
        )
        response["_retrieval_warning_code"] = embed_error

    # Low-confidence signal — calibrated empirically against the corpus:
    #
    # Hybrid retrieval (RRF, k=60):
    #   - Both rankers at rank 1: fusion = 2/61 ≈ 0.0328 (max)
    #   - One ranker at rank 1, other absent: fusion = 1/61 ≈ 0.0164
    #   - Both rankers at rank 5: fusion ≈ 0.0308
    #
    # Real compliance queries against the v0.5 corpus consistently score
    # ≥0.025 with BOTH rankers contributing (sparse + dense agreeing). Out-
    # of-scope inputs (recipes, sports articles) score weaker because dense
    # finds spurious neighbors but sparse does not, OR sparse hits an
    # unrelated keyword while dense misses entirely. We require:
    #   (a) top fusion >= 0.022, AND
    #   (b) BOTH rankers contributed (the top chunk appears in both sparse
    #       AND dense rankings — the strongest possible signal).
    # When dense is unavailable, fall back to the v0.1.5 BM25 threshold.
    if not provisions:
        response["low_confidence"] = True
        response["message"] = (
            "No provisions matched. Either the topic is out of corpus or the "
            "input doesn't contain searchable terms."
        )
    else:
        top = provisions[0]
        if response["retrieval"] == "hybrid":
            top_fusion = top.get("fusion_score") or 0.0
            sparse_rank = top.get("sparse_rank")
            dense_rank = top.get("dense_rank")
            both_rankers_agree = sparse_rank is not None and dense_rank is not None
            if top_fusion < 0.022 or not both_rankers_agree:
                response["low_confidence"] = True
        else:
            bm25 = top.get("bm25_score")
            if bm25 is None or bm25 > -5.0:
                response["low_confidence"] = True

    return response


_CAVEAT = (
    "Hybrid retrieval (FTS5 sparse + sqlite-vec dense embeddings, RRF fusion) "
    "over indexed RBI source material: Master Directions, Standalone Circulars, "
    "Master Circulars, Press Releases, and FAQs. Provisions returned here are "
    "source material only; this MCP does not issue legal opinions or compliance "
    "verdicts. Verify with a human compliance reviewer before acting."
)


def _row_to_provision(row: dict | sqlite3.Row) -> dict[str, Any]:
    """Hybrid_search returns dicts; sparse-only returns sqlite3.Row. Normalize to dict."""
    if not isinstance(row, dict):
        # sqlite3.Row → dict so the rest of this function uses uniform .get().
        # row.keys() is the documented way to enumerate sqlite3.Row columns;
        # iterating the Row yields values, not keys, hence the noqa.
        row = {k: row[k] for k in row.keys()}  # noqa: SIM118
    return {
        "document_id": row.get("document_id"),
        "title": row.get("document_title"),
        "rbi_ref": row.get("rbi_ref"),
        "section": row.get("section"),
        "paragraph_anchor": row.get("paragraph_anchor"),
        "page": row.get("page"),
        "quoted_text": row.get("text"),
        "official_url": row.get("document_url"),
        "pdf_url": row.get("pdf_url"),
        "status": row.get("status") or "current",
        "last_updated_at": row.get("last_updated_at"),
        "bm25_score": row.get("bm25_score"),
        "vec_distance": row.get("vec_distance"),
        "sparse_rank": row.get("sparse_rank"),
        "dense_rank": row.get("dense_rank"),
        "fusion_score": row.get("fusion_score"),
    }


