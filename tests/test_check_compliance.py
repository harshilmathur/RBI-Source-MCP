"""Tests for rbi.check_compliance — the v2 headline tool."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from rbi_source_mcp.db import connect, upsert_md
from rbi_source_mcp.mcp.check_compliance import check_compliance


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "test.sqlite"
    now = datetime.utcnow().isoformat() + "Z"
    with connect(db_path) as conn:
        # Seed: minimal documents row + a few synthetic chunks.
        upsert_md(
            conn,
            md_id="12896",
            title="Master Direction on Regulation of Payment Aggregator (PA)",
            detail_url="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12896",
            last_updated_at="2025-09-15",
            department=None,
            pdf_urls=[],
            status="current",
            now=now,
        )
        # Insert chunks so chunks_fts gets populated by trigger.
        chunks = [
            ("rbi:md:12896#p=6.a", "rbi:master_direction:12896", "12896", "RBI/DPSS/2025-26/141",
             "6", "6.a", 5,
             "An entity seeking authorisation to commence or carry on PA business shall have a minimum net-worth of fifteen crore rupees at the time of tendering application.",
             "abc123", 0, 200,
             "https://example.com/pa.pdf", now),
            ("rbi:md:12896#p=8.2", "rbi:master_direction:12896", "12896", "RBI/DPSS/2025-26/141",
             "8", "8.2", 7,
             "Payment Aggregators shall ensure that merchant data including KYC documents is used solely for the purposes for which it was collected and not shared with affiliates for promotional purposes.",
             "def456", 200, 400,
             "https://example.com/pa.pdf", now),
            ("rbi:md:12896#p=18.a", "rbi:master_direction:12896", "12896", "RBI/DPSS/2025-26/141",
             "18", "18.a", 12,
             "Funds collected from customers shall be transferred to a designated escrow account maintained with a scheduled commercial bank within prescribed timelines.",
             "ghi789", 400, 600,
             "https://example.com/pa.pdf", now),
        ]
        for c in chunks:
            conn.execute(
                """INSERT INTO chunks (
                    chunk_id, document_id, md_id, rbi_ref, section, paragraph_anchor, page,
                    text, text_sha256, char_start, char_end, pdf_url, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                c,
            )
    with connect(db_path) as conn:
        yield conn


def test_net_worth_question_finds_paragraph_6_a(db) -> None:
    """Net-worth query should surface paragraph 6.a in top-3 (the actual rule)."""
    result = check_compliance(
        db,
        "What is the minimum net-worth required for a payment aggregator?",
        topic_hint="payment_aggregator",
    )
    assert result["relevant_provisions"]
    anchors = [p["paragraph_anchor"] for p in result["relevant_provisions"]]
    # Synthetic fixture has only 3 chunks; with the real ~137-chunk PA corpus,
    # 6.a ranks #1 (verified live). The fixture test asserts presence.
    assert "6.a" in anchors
    assert all(p["status"] == "current" for p in result["relevant_provisions"])


def test_kyc_sharing_clause_finds_paragraph_8_2(db) -> None:
    """KYC-sharing clause should surface paragraph 8.2 in top-3."""
    result = check_compliance(
        db,
        "We may share merchant KYC documents with affiliate companies for promotional purposes.",
        topic_hint="payment_aggregator",
    )
    assert result["relevant_provisions"]
    anchors = [p["paragraph_anchor"] for p in result["relevant_provisions"]]
    assert "8.2" in anchors


def test_response_includes_required_envelope_fields(db) -> None:
    result = check_compliance(db, "net-worth requirements")
    for field in (
        "relevant_provisions",
        "withdrawn_provisions_excluded",
        "low_confidence",
        "as_of",
        "caveat",
        "tool",
        "not_legal_advice",
    ):
        assert field in result, f"missing field: {field}"
    assert result["tool"] == "rbi.check_compliance"
    assert result["not_legal_advice"] is True


def test_empty_input_returns_low_confidence_envelope(db) -> None:
    result = check_compliance(db, "")
    assert result["low_confidence"] is True
    assert result["relevant_provisions"] == []
    assert "Empty input" in result["message"]


def test_punctuation_only_input_returns_low_confidence(db) -> None:
    result = check_compliance(db, "...???!!!")
    assert result["low_confidence"] is True
    assert result["relevant_provisions"] == []


def test_out_of_scope_input_flags_low_confidence(db) -> None:
    """Recipe text should not be treated as a confident compliance match."""
    result = check_compliance(
        db,
        "To make pasta carbonara beat eggs with parmesan and pancetta until creamy.",
    )
    # With the synthetic 3-chunk corpus, FTS5 may match nothing or weak matches;
    # either way, low_confidence should fire.
    assert result["low_confidence"] is True


def test_each_provision_carries_full_citation(db) -> None:
    result = check_compliance(db, "net-worth requirements", topic_hint="payment_aggregator")
    p = result["relevant_provisions"][0]
    for field in (
        "document_id",
        "title",
        "rbi_ref",
        "paragraph_anchor",
        "official_url",
        "status",
    ):
        assert p.get(field), f"provision missing field: {field}"


def test_topic_hint_filters_to_correct_md(db) -> None:
    result = check_compliance(
        db,
        "net-worth requirements",
        topic_hint="payment_aggregator",
    )
    assert result["relevant_provisions"]
    assert all(p["document_id"] == "rbi:master_direction:12896" for p in result["relevant_provisions"])


def test_response_never_raises_on_garbage(db) -> None:
    """The DX guarantee: no exception path on any input."""
    for garbage in ["", "   ", "...", "\x00\x01\x02", "🤖🤖🤖", "a" * 100000]:
        result = check_compliance(db, garbage)
        assert isinstance(result, dict)
        assert "tool" in result
