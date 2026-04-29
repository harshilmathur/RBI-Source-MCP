"""Tests for the disclaimer-on-error contract.

The load-bearing legal posture: EVERY MCP response carries `_disclaimer`
and `_llm_instruction`, including the error path. If a tool handler
raises, the wrapped error envelope must still include the disclaimer
fields. These tests guard that invariant.
"""

from __future__ import annotations

import json

from rbi_source_mcp.server import _error_response, _wrap_response


class _FakeError(Exception):
    pass


def test_error_response_carries_disclaimer_at_top() -> None:
    """The error envelope must have `_disclaimer` and `_llm_instruction`
    at the top of the JSON object — same as success envelope."""
    tc = _error_response(
        tool_name="rbi_search",
        reason="tool_failed",
        exc=_FakeError("boom"),
    )
    payload = json.loads(tc.text)
    keys = list(payload.keys())
    assert keys[0] == "_disclaimer"
    assert keys[1] == "_llm_instruction"
    assert "NOT LEGAL ADVICE" in payload["_disclaimer"]
    assert payload["status"] == "error"
    assert payload["tool"] == "rbi_search"
    assert payload["reason"] == "tool_failed"


def test_error_response_does_not_leak_traceback() -> None:
    """Public error responses must NOT contain internal paths, full
    exception messages, or tracebacks. Only the exception class name +
    a generic message."""
    tc = _error_response(
        tool_name="rbi_check_compliance",
        reason="db_unavailable",
        exc=FileNotFoundError("/secret/path/to/db.sqlite not found"),
    )
    payload = json.loads(tc.text)
    text = json.dumps(payload)
    # The class name is fine to expose; the path must not be.
    assert "FileNotFoundError" in text
    assert "/secret/path" not in text


def test_error_response_class_name_passes_through() -> None:
    """The exception class name flows through `error_class` so consuming
    LLMs/clients can do basic categorization."""
    tc = _error_response(
        tool_name="rbi_check_current",
        reason="tool_failed",
        exc=ValueError("bad input"),
    )
    payload = json.loads(tc.text)
    assert payload["error_class"] == "ValueError"


def test_wrap_response_and_error_response_share_disclaimer_field_order() -> None:
    """Both success and error envelopes must agree on field ordering so
    LLMs see a consistent shape regardless of outcome."""
    success = _wrap_response({"status": "ok", "results": []})
    error = json.loads(_error_response("rbi_search", "tool_failed", _FakeError("x")).text)
    assert list(success.keys())[:2] == list(error.keys())[:2]
    assert success["_disclaimer"] == error["_disclaimer"]
    assert success["_llm_instruction"] == error["_llm_instruction"]
