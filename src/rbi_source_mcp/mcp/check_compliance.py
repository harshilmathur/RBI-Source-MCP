"""rbi.check_compliance — the v2 headline tool.

Takes free text (a clause, PRD section, draft policy paragraph) and returns
ranked relevant Master Direction provisions with citations. **Retrieval-only
by design** — never emits a heuristic compliance verdict. The LLM consuming
this synthesizes the call; the user makes the decision.

This separation is locked in the design doc's "Why retrieval-only" section.
The MCP stays defensibly on the source-layer side of the line; we are not
an unauthorized regulatory advisor.

v0.1.5: FTS5-only retrieval (sparse). Hybrid (FTS5 + sqlite-vec) ships at
v0.5 once the corpus expands beyond a single MD.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from ..db import escape_fts5_query, search_chunks_fts


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
    now = datetime.utcnow().isoformat() + "Z"
    raw = (text or "").strip()

    response: dict[str, Any] = {
        "relevant_provisions": [],
        "withdrawn_provisions_excluded": 0,
        "low_confidence": False,
        "as_of": now,
        "caveat": _CAVEAT,
        "topic_hint": topic_hint,
        "input_text_preview": raw[:120],
        "tool": "rbi.check_compliance",
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
    rows = search_chunks_fts(
        conn,
        fts_query,
        limit=limit,
        md_id=md_id_filter,
        include_withdrawn=False,
    )

    provisions = [_row_to_provision(r) for r in rows]
    response["relevant_provisions"] = provisions

    # Low-confidence signal: SQLite's bm25() returns negative scores; more
    # negative = better match. Legitimate compliance queries against the PA
    # corpus score around -8 to -13. Out-of-scope text (recipes, etc.) tops
    # out around -4. Threshold at -5.0 gives a conservative cue for the
    # consuming LLM to decline synthesis on weak matches.
    #
    # When the corpus expands at v0.5, this threshold may need re-tuning;
    # for v0.1.5 single-MD corpus, -5.0 holds up against real queries.
    if not provisions:
        response["low_confidence"] = True
        response["message"] = "No provisions matched. Either the topic is out of corpus or the input doesn't contain searchable terms."
    else:
        top_score = provisions[0]["bm25_score"]
        if top_score is None or top_score > -5.0:
            response["low_confidence"] = True

    return response


_CAVEAT = (
    "v0.1.5 corpus is limited (single Master Direction at this stage). Hybrid "
    "(BM25 + dense vector) retrieval ships at v0.5 once the corpus expands. "
    "Provisions returned here are source material only; this MCP does not "
    "issue legal opinions or compliance verdicts. Verify with a human "
    "compliance reviewer before acting."
)


def _row_to_provision(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "document_id": row["document_id"],
        "title": row["document_title"],
        "rbi_ref": row["rbi_ref"],
        "section": row["section"],
        "paragraph_anchor": row["paragraph_anchor"],
        "page": row["page"],
        "quoted_text": row["text"],
        "official_url": row["document_url"],
        "pdf_url": row["pdf_url"],
        "status": row["status"] or "current",
        "last_updated_at": row["last_updated_at"],
        "bm25_score": row["bm25_score"],
    }


# Topic hint → md_id mapping. Hand-curated; expands as more MDs land.
_TOPIC_TO_MD_ID: dict[str, str] = {
    "payment_aggregator": "12896",
    "pa": "12896",
    "pa_pg": "12896",
}


def _md_id_for_topic_hint(hint: str | None) -> str | None:
    if not hint:
        return None
    return _TOPIC_TO_MD_ID.get(hint.lower())
