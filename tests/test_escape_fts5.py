"""Tests for db.escape_fts5_query — the FTS5 query-rewrite safety boundary.

FTS5 has its own query language (AND, OR, NOT, NEAR, column-filters,
parentheses); raw user input would either trip syntax errors OR
silently get reinterpreted in ways the caller did not intend (think:
the LLM submitting a clause that contains the word `NOT`).

`escape_fts5_query` is the single safety boundary. It is called by
`mcp/search.py` and `mcp/check_compliance.py` BEFORE every FTS5
invocation. A regression — failing to neutralize an operator, lifting
the 32-token cap, or leaking column-filter syntax — would silently
change the semantics of every search.

These tests pin the contract directly so a regression turns a test red
instead of corrupting search results.
"""

from __future__ import annotations

from rbi_source_mcp.db import escape_fts5_query


def test_operators_are_quoted_not_interpreted() -> None:
    """`AND`, `NOT`, `NEAR` must be returned as quoted phrase tokens, so
    FTS5 treats them as literal terms rather than boolean operators.

    Without escaping, `kyc NOT verification` would EXCLUDE rows matching
    `verification` — almost certainly not what the user intended. The
    quote wrapping demotes the operators back to plain search terms.

    Note: the implementation joins tokens with an explicit ` OR `, which
    is an intentional permissive semantics (any-token-matches). Bare AND
    / NOT / NEAR must NEVER appear unquoted.
    """
    out = escape_fts5_query("kyc AND verification NOT customer NEAR diligence")
    for tok in ("kyc", "AND", "verification", "NOT", "customer", "NEAR", "diligence"):
        assert f'"{tok}"' in out, f"token {tok!r} not quoted in {out!r}"
    # The dangerous bare operators must NOT appear (the OR is intentional).
    for op in (" AND ", " NOT ", " NEAR "):
        assert op not in out, f"bare operator {op!r} leaked into {out!r}"


def test_column_filter_syntax_is_neutralized() -> None:
    """FTS5 supports `column:term` to constrain a query to a single column.
    User input that happens to contain a colon must NOT silently rewrite
    the search to a narrower column."""
    out = escape_fts5_query("text:kyc customer")
    # The colon survives only INSIDE the quoted token, never as an operator.
    # Each word is its own token after `\w+` extraction so `text` and `kyc`
    # end up as separate quoted tokens.
    assert '"text"' in out
    assert '"kyc"' in out
    assert '"customer"' in out
    # And the dangerous `text:kyc` pattern is gone.
    assert "text:kyc" not in out


def test_parens_are_stripped() -> None:
    """FTS5 uses parens for grouping. User input parens must not group."""
    out = escape_fts5_query("(kyc OR customer) AND verification")
    assert "(" not in out
    assert ")" not in out


def test_token_cap_enforced() -> None:
    """The implementation caps at 32 tokens to bound pathological inputs."""
    big = " ".join(f"tok{i}" for i in range(100))
    out = escape_fts5_query(big)
    # Count quoted tokens.
    quoted_count = out.count('"') // 2
    assert quoted_count == 32, (
        f"expected 32-token cap, got {quoted_count} (output: {out[:200]!r})"
    )


def test_pure_punctuation_returns_safe_empty() -> None:
    """A query that contains nothing but punctuation must NOT produce a
    bare empty MATCH (which is an FTS5 syntax error). It should return a
    sentinel that FTS5 accepts but matches zero rows."""
    out = escape_fts5_query("!@#$%^&*()")
    # `''` (FTS5 empty string literal) is the safe form documented in the
    # source — yields zero hits, no syntax error.
    assert out == '""'


def test_empty_input_returns_safe_empty() -> None:
    assert escape_fts5_query("") == '""'
    assert escape_fts5_query("   \t\n  ") == '""'


def test_unicode_tokens_preserved() -> None:
    """`\\w+` in re.UNICODE mode keeps non-ASCII letter tokens.

    Devanagari combining-vowel codepoints split the source word into
    multiple `\\w+` matches (this is how Python's regex sees the script),
    so we only assert that the ASCII token survives intact and at least
    one Devanagari subtoken makes it through. That's the safety contract:
    non-ASCII input is not silently dropped.
    """
    out = escape_fts5_query("KYC नियम")  # 'rules' in Devanagari
    assert '"KYC"' in out
    # At least one Devanagari character must survive as a quoted token.
    assert any(c in out for c in "नियम"), f"no Devanagari token in {out!r}"


def test_already_quoted_input_stays_safe() -> None:
    """Input that contains FTS5 quote chars must be re-tokenized cleanly,
    not double-quoted into invalid syntax."""
    out = escape_fts5_query('"kyc" "verification"')
    # Bare double-quote chars must not appear outside a single tokenized pair.
    # Output is just two quoted tokens separated by space.
    assert out.count('"') == 4
    assert '"kyc"' in out
    assert '"verification"' in out
