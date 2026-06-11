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


def test_parenthesized_sub_clauses_are_siblings_not_children() -> None:
    """Parenthesized numbers like (1), (2), (10) are siblings under a paragraph,
    NOT nested children. RBI uses these heavily inside definition paragraphs
    (e.g., "5. In these Directions... (1) ... (2) ... (10) ...")."""
    text = """
5.  In these Directions, unless the context otherwise requires, the following
    shall apply:

    (1) Convenience Fee is a fixed or pro-rata charge on use of card.
        It applies to all transactions executed online via card networks.
    (2) Additional Factor of Authentication means a method of authenticating
        a transaction using a factor independent of the primary credential.
    (10) Most Important Terms and Conditions (MITC) are the standard set of
         terms required to be communicated upfront.
"""
    chunks = chunk_md_text(text, document_id="rbi:md:test", md_id="test")
    anchors = {c.paragraph_anchor for c in chunks}
    # All parenthesized sub-clauses appear as siblings under section 5.
    assert "5.(1)" in anchors
    assert "5.(2)" in anchors
    assert "5.(10)" in anchors
    # No deep concatenation pathology: no anchor like "5.(1).(2)".
    bad = [a for a in anchors if a and a.count("(") > 1]
    assert not bad, f"deep concatenation in: {bad}"


def test_letter_after_paren_resets_correctly() -> None:
    """If a paragraph has both (1) (2) and then a sub-clause a, b, c — letter
    transitions should NOT carry the parenthesized number forward."""
    text = """
4.  Top-level paragraph with substantial content explaining the overall
    rule and what entities it applies to in detail.

    (1) First parenthesized sub-clause with substantial body text that
        passes the chunker minimum threshold for emission.

    a.  Letter sub-clause with substantial content covering one specific
        aspect of the rule and its concrete application.
    b.  Second letter sub-clause with substantial content covering another
        aspect that is parallel to clause a above.
"""
    chunks = chunk_md_text(text, document_id="rbi:md:test", md_id="test")
    anchors = {c.paragraph_anchor for c in chunks}
    # When letters arrive after (1), they should appear as 4.a / 4.b
    # (NOT 4.(1).a / 4.(1).b). Both forms of sibling-reset must work.
    assert "4.a" in anchors
    assert "4.b" in anchors
    assert "4.(1).a" not in anchors


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


def test_char_offsets_account_for_form_feed_across_pages() -> None:
    """char_start/char_end must map back to the source text across page
    boundaries. Regression for the missing form-feed (+1) accounting on the
    non-empty-page branch, which drifted offsets one byte earlier per page."""
    page1 = (
        "1. Capital adequacy. Every regulated entity shall maintain a minimum "
        "capital-to-risk-weighted-assets ratio of nine per cent computed on the "
        "prescribed risk weights specified in the annex hereto for all banks.\n"
    )
    page2 = (
        "2. Customer due diligence. Every regulated entity shall verify the "
        "identity of each customer and carry out periodic KYC updates at the "
        "frequency prescribed for the applicable risk category of the customer.\n"
    )
    text = page1 + "\x0c" + page2  # two pages separated by a form-feed
    chunks = chunk_md_text(text, document_id="d", md_id="1")
    assert len(chunks) >= 2

    # Every chunk's char_start must land exactly on its first character —
    # lstrip would hide a one-byte drift onto the form-feed, so check the raw char.
    for c in chunks:
        assert text[c.char_start] == c.text.strip()[0], (
            f"offset drift: text[{c.char_start}]={text[c.char_start]!r} "
            f"!= chunk first char {c.text.strip()[0]!r}"
        )

    # The page-2 paragraph must start exactly past page 1 + the form-feed byte.
    p2 = [c for c in chunks if c.text.strip().startswith("2.")]
    assert p2, "expected a page-2 chunk"
    assert p2[0].char_start == len(page1) + 1
