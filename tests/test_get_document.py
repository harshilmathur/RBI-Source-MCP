"""Tests for the rbi_get_document MCP tool.

One of four advertised MCP tools and previously had zero coverage. The
contract: every input returns a structured envelope (no exceptions),
unknown documents return `status="unknown"`, the table-of-contents is
ordered by `char_start`, and `include_text=True` concatenates chunks in
the same order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rbi_source_mcp.db import connect
from rbi_source_mcp.indexer.persist import persist_document_and_chunks
from rbi_source_mcp.mcp.get_document import get_document

_BODY = (
    "1. Definitions\n"
    "These directions shall apply to every regulated entity engaged in the "
    "business of lending under the Reserve Bank of India Act.\n"
    "\n"
    "2. Capital adequacy\n"
    "Every regulated entity shall maintain a minimum capital-to-risk-"
    "weighted-assets ratio of nine per cent.\n"
    "\n"
    "3. Reporting\n"
    "Each entity shall report quarterly to the Reserve Bank in the form "
    "prescribed in the Annex.\n"
)


@pytest.fixture
def seeded_db(tmp_path: Path):
    db_path = tmp_path / "getdoc.sqlite"
    with connect(db_path) as conn:
        persist_document_and_chunks(
            conn,
            document_id="rbi:master_direction:12550",
            md_id="12550",
            title="Master Direction on KYC, 2016",
            detail_url="https://example.invalid/12550",
            document_family="master_direction",
            body_text=_BODY,
            last_updated_at="2026-04-25",
        )
    with connect(db_path) as conn:
        yield conn


def test_happy_path_returns_metadata_and_toc(seeded_db) -> None:
    result = get_document(seeded_db, "rbi:master_direction:12550")
    assert result["status"] == "current"
    assert result["title"] == "Master Direction on KYC, 2016"
    assert result["official_url"] == "https://example.invalid/12550"
    assert result["last_updated_at"] == "2026-04-25"
    assert "table_of_contents" in result
    assert result["chunk_count"] >= 1
    # Default is metadata-only; full text is NOT included unless requested.
    assert "text" not in result


def test_empty_document_id_returns_error_envelope(seeded_db) -> None:
    result = get_document(seeded_db, "")
    assert result["status"] == "error"
    assert result["error"] == "missing_document_id"
    assert result["tool"] == "rbi_get_document"


def test_unknown_document_id_returns_unknown_envelope(seeded_db) -> None:
    result = get_document(seeded_db, "rbi:master_direction:does-not-exist")
    assert result["status"] == "unknown"
    assert result["error"] == "document_not_in_corpus"
    assert "message" in result


def test_table_of_contents_ordered_by_char_start(seeded_db) -> None:
    """The TOC must be in document order so the LLM can rely on the index
    to navigate sections sequentially."""
    result = get_document(seeded_db, "rbi:master_direction:12550")
    starts = []
    for entry in result["table_of_contents"]:
        chunk_id = entry["chunk_id"]
        char_start = seeded_db.execute(
            "SELECT char_start FROM chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()["char_start"]
        starts.append(char_start)
    assert starts == sorted(starts), (
        "table_of_contents must be ordered by char_start ascending"
    )


def test_include_text_assembles_body_in_chunk_order(seeded_db) -> None:
    result = get_document(seeded_db, "rbi:master_direction:12550", include_text=True)
    assert "text" in result
    # Each chunk's text should appear, and the order should match
    # char_start (i.e. document order).
    body = result["text"]
    # The body should contain the section labels from the original body.
    assert "Definitions" in body
    assert "Capital adequacy" in body
    assert "Reporting" in body
    # Section 1 must appear before section 2 in the assembled text.
    assert body.index("Definitions") < body.index("Capital adequacy")
    assert body.index("Capital adequacy") < body.index("Reporting")


def test_as_of_injects_caveat_but_returns_current(seeded_db) -> None:
    """`as_of` is reserved for v1.1; v0.1.5 returns current state plus a
    caveat field so the LLM can surface the limitation to the user."""
    result = get_document(
        seeded_db, "rbi:master_direction:12550", as_of="2025-01-01"
    )
    assert result["status"] == "current"
    assert "caveat" in result
    assert "not honored" in result["caveat"]


def test_response_always_carries_tool_and_as_of(seeded_db) -> None:
    """Every code path emits `tool` and `as_of` for downstream wrapping."""
    inputs = [
        "rbi:master_direction:12550",
        "rbi:master_direction:does-not-exist",
        "",
    ]
    for doc_id in inputs:
        result = get_document(seeded_db, doc_id)
        assert result["tool"] == "rbi_get_document", f"missing tool for {doc_id!r}"
        assert "as_of" in result, f"missing as_of for {doc_id!r}"
