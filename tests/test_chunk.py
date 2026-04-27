"""Tests for the paragraph chunker."""

from __future__ import annotations

from rbi_source_mcp.indexer.chunk import chunk_md_text


def test_top_level_paragraphs_become_separate_chunks() -> None:
    text = """
1.  This is the first top-level paragraph. It contains several sentences
    explaining the regulatory framework in some detail.
2.  This is the second top-level paragraph. It also has substantial content
    so the chunker doesn't drop it.
"""
    chunks = chunk_md_text(text, document_id="rbi:md:test", md_id="test")
    assert len(chunks) == 2
    assert chunks[0].paragraph_anchor == "1"
    assert chunks[1].paragraph_anchor == "2"


def test_letter_sub_clauses_are_siblings_not_children() -> None:
    """Letters under a paragraph are a, b, c — NOT a → a.b → a.b.c."""
    text = """
4.  This paragraph has sub-clauses with substantial content explaining
    each requirement. Each sub-clause also has substantial detail.

    a.  The first sub-clause goes here with enough text to pass the minimum
        chunk size threshold for the chunker to emit it as a real chunk.
    b.  The second sub-clause goes here with enough detail to pass the chunker
        threshold and make it into the database as a separate citable chunk.
    c.  The third sub-clause has its own substantial content with more than
        eighty characters so it gets emitted as a real chunk.
"""
    chunks = chunk_md_text(text, document_id="rbi:md:test", md_id="test")
    anchors = [c.paragraph_anchor for c in chunks]
    # Anchors must be 4, 4.a, 4.b, 4.c — not 4, 4.a, 4.a.b, 4.a.b.c.
    assert "4.a" in anchors
    assert "4.b" in anchors
    assert "4.c" in anchors
    # No deep concatenation pathology.
    assert not any(a and a.count(".") > 1 for a in anchors)


def test_chunker_handles_empty_input() -> None:
    assert chunk_md_text("", document_id="rbi:md:test", md_id="test") == []
    assert chunk_md_text("   ", document_id="rbi:md:test", md_id="test") == []


def test_chunker_drops_tiny_heading_only_paragraphs() -> None:
    """A line like '5. Definitions' alone should NOT become a chunk."""
    text = """
5. Definitions
6.  This is a substantial paragraph with enough content to pass the eighty-
    character minimum chunk threshold and make it into the database for
    retrieval testing purposes.
"""
    chunks = chunk_md_text(text, document_id="rbi:md:test", md_id="test")
    anchors = [c.paragraph_anchor for c in chunks]
    assert "5" not in anchors  # heading-only, dropped
    assert "6" in anchors


def test_chunk_id_is_stable_across_runs() -> None:
    """Same input → same chunk IDs (idempotency for hash-gated refresh)."""
    text = """
1.  Substantial paragraph content for the test, with enough characters to
    pass the minimum chunk threshold required by the chunker module.
"""
    chunks_a = chunk_md_text(text, document_id="rbi:md:test", md_id="test")
    chunks_b = chunk_md_text(text, document_id="rbi:md:test", md_id="test")
    assert [c.chunk_id for c in chunks_a] == [c.chunk_id for c in chunks_b]


def test_form_feed_separates_pages() -> None:
    """Page numbers should track form-feed boundaries."""
    text = (
        "1.  Substantial first-page paragraph content "
        "going on for many words to pass the chunk threshold.\n"
        "\x0c"  # page break
        "2.  Substantial second-page paragraph content "
        "going on for many words to pass the chunk threshold.\n"
    )
    chunks = chunk_md_text(text, document_id="rbi:md:test", md_id="test")
    by_anchor = {c.paragraph_anchor: c for c in chunks}
    assert by_anchor["1"].page == 1
    assert by_anchor["2"].page == 2
