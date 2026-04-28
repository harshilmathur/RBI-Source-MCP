"""Tests for the response disclaimer.

The disclaimer is the project's load-bearing legal posture: every MCP tool
response MUST carry it, and the LLM-instruction field MUST tell consuming
LLMs to surface it. These tests guard that invariant.
"""

from __future__ import annotations

import json

from rbi_source_mcp.disclaimer import DISCLAIMER, DISCLAIMER_SHORT, LLM_INSTRUCTION
from rbi_source_mcp.server import _wrap_response


def test_disclaimer_text_has_load_bearing_phrases() -> None:
    """The disclaimer text must include all five points the project promises."""
    text = DISCLAIMER.lower()
    # 1. Not legal advice — capitalized in the source.
    assert "not legal advice" in text
    # 2. Retrieval-only / source-grounded.
    assert "source-grounded" in text or "retrieval" in text
    # 3. Provisions may have changed since refresh.
    assert "amended" in text or "may have changed" in text or "refresh" in text
    # 4. Verify with a human compliance reviewer.
    assert "verify" in text and "compliance reviewer" in text
    # 5. Affiliation disclaimer.
    assert "not affiliated" in text and "reserve bank of india" in text


def test_disclaimer_lists_all_corpus_families() -> None:
    """v0.5.6+ corpus spans 5 families; the disclaimer must reflect that so
    users aren't misled into thinking we only cover Master Directions."""
    text = DISCLAIMER.lower()
    assert "master directions" in text
    assert "circulars" in text  # covers both 'circulars' and 'master circulars'
    assert "press releases" in text
    assert "faqs" in text


def test_short_disclaimer_keeps_not_legal_advice() -> None:
    """The short form (used in logs / tool descriptions) must still lead with the warning."""
    assert "NOT LEGAL ADVICE" in DISCLAIMER_SHORT
    assert "compliance reviewer" in DISCLAIMER_SHORT.lower()


def test_llm_instruction_names_all_five_points() -> None:
    """LLM_INSTRUCTION enumerates the five points so consuming LLMs can preserve them."""
    text = LLM_INSTRUCTION.lower()
    assert "not legal advice" in text
    assert "retrieval-only" in text or "retrieval" in text
    assert "amended" in text or "withdrawn" in text or "since" in text
    assert "compliance reviewer" in text or "human" in text
    # 5th point: non-affiliation. Previously missing.
    assert "unofficial" in text or "not affiliated" in text


def test_wrap_response_injects_disclaimer_at_top() -> None:
    """The disclaimer fields must appear FIRST in the JSON object so an LLM
    streaming or partially-summarizing the response sees them before the data."""
    payload = {"results": [{"chunk_id": "x"}], "tool": "rbi.search"}
    wrapped = _wrap_response(payload)
    keys = list(wrapped.keys())
    assert keys[0] == "_disclaimer"
    assert keys[1] == "_llm_instruction"
    # Original keys preserved in order after the prepended pair.
    assert "results" in wrapped
    assert "tool" in wrapped
    assert wrapped["_disclaimer"] == DISCLAIMER
    assert wrapped["_llm_instruction"] == LLM_INSTRUCTION


def test_wrap_response_does_not_mutate_input() -> None:
    """_wrap_response should produce a new dict, not modify the caller's payload."""
    payload = {"results": [{"chunk_id": "x"}]}
    snapshot = dict(payload)
    _ = _wrap_response(payload)
    assert payload == snapshot


def test_wrap_response_serializes_to_valid_json() -> None:
    """Round-trip through json.dumps/json.loads — no surprises."""
    payload = {
        "tool": "rbi.check_compliance",
        "relevant_provisions": [{"document_id": "rbi:master_direction:12896"}],
        "low_confidence": False,
    }
    wrapped = _wrap_response(payload)
    text = json.dumps(wrapped)
    decoded = json.loads(text)
    assert decoded["_disclaimer"] == DISCLAIMER
    assert decoded["_llm_instruction"] == LLM_INSTRUCTION
    assert decoded["tool"] == "rbi.check_compliance"


def test_wrap_response_handles_empty_payload() -> None:
    """Even an empty result still carries the disclaimer."""
    wrapped = _wrap_response({})
    assert wrapped["_disclaimer"] == DISCLAIMER
    assert wrapped["_llm_instruction"] == LLM_INSTRUCTION
