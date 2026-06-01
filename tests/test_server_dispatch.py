"""Tests for the call_tool dispatch boundary in server._build_server.

The dispatch handler is a closure registered via the MCP SDK's
`@server.call_tool()` decorator. End-to-end tests would spawn a stdio
subprocess; here we invoke the registered handler directly through the
SDK's request-handler registry so the tests stay fast.

What we pin:
- **Unknown tool**: returns a wrapped error envelope with `_disclaimer`,
  status="error", reason="unknown_tool". No exception escapes.
- **Oversize input**: triggers the `input_too_large` envelope for the
  two tools that document a `MAX_INPUT_LEN` cap (rbi_check_compliance
  and rbi_search). The envelope carries the disclaimer.
- **DB unavailable**: when the DB path points at an unreadable location,
  the dispatcher returns the `db_unavailable` envelope rather than
  raising — telemetry status flips to `db_unavailable`, the disclaimer
  is preserved on the response.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from rbi_source_mcp.server import MAX_INPUT_LEN, _build_server


def _call_tool(name: str, arguments: dict) -> dict:
    """Invoke the registered call_tool handler and return the parsed JSON.

    The MCP SDK wraps the user's call_tool function inside an internal
    handler stored at `server.request_handlers[CallToolRequest]`. We
    construct a minimal CallToolRequest and run the handler to recover
    the same dict the LLM would see. Returns the first TextContent's
    parsed JSON body.
    """
    from mcp import types

    server = _build_server()
    handler = server.request_handlers[types.CallToolRequest]

    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    server_result = asyncio.run(handler(request))
    call_result = server_result.root  # CallToolResult inside ServerResult
    # call_result.content is a list of ContentBlocks (TextContent, etc.)
    first = call_result.content[0]
    return json.loads(first.text)


def test_unknown_tool_returns_wrapped_error(tmp_path: Path, monkeypatch) -> None:
    """An unknown tool name must NOT raise; it must come back as a wrapped
    error envelope with the disclaimer attached."""
    monkeypatch.setenv("RBI_SOURCE_DB", str(tmp_path / "ut.sqlite"))

    body = _call_tool("rbi_does_not_exist", {})
    assert body["status"] == "error"
    assert body["reason"] == "unknown_tool"
    assert body["tool"] == "rbi_does_not_exist"
    # Disclaimer contract: every response carries it.
    assert "_disclaimer" in body
    assert "_llm_instruction" in body


@pytest.mark.parametrize(
    "tool_name,field",
    [
        ("rbi_check_compliance", "text"),
        ("rbi_search", "query"),
    ],
)
def test_input_too_large_envelope(
    tool_name: str, field: str, tmp_path: Path, monkeypatch
) -> None:
    """`text` (compliance) and `query` (search) over MAX_INPUT_LEN trip a
    dedicated `input_too_large` envelope — never the generic tool_failed."""
    monkeypatch.setenv("RBI_SOURCE_DB", str(tmp_path / "big.sqlite"))

    big = "x" * (MAX_INPUT_LEN + 1)
    body = _call_tool(tool_name, {field: big})

    # The input_too_large helper produces a specific reason code.
    assert body.get("status") == "error"
    assert body.get("reason") == "input_too_large"
    # The envelope explains the limit so the LLM can apologize sensibly.
    assert "max_length" in body
    assert body["max_length"] == MAX_INPUT_LEN
    # Disclaimer present even on the error path.
    assert "_disclaimer" in body


def test_db_unavailable_envelope(tmp_path: Path, monkeypatch) -> None:
    """When init_db fails (e.g. directory unreadable), the dispatcher
    routes to `_error_response("db_unavailable", ...)` — pin the status
    name and the disclaimer presence."""
    # Point the DB env at a path inside a non-existent directory whose
    # parent is also non-writable so init_db raises.
    bad = tmp_path / "does" / "not" / "exist" / "db.sqlite"
    monkeypatch.setenv("RBI_SOURCE_DB", str(bad))

    # Cause init_db to fail by making the chain unreachable AND swap
    # `connect` to raise. The simpler reliable approach: monkeypatch
    # `init_db` to raise.
    import rbi_source_mcp.server as server_mod

    def _explode(_path: str) -> None:
        raise RuntimeError("simulated db init failure")

    monkeypatch.setattr(server_mod, "init_db", _explode)

    body = _call_tool("rbi_check_compliance", {"text": "what's the kyc rule?"})
    # The dispatcher catches and emits db_unavailable as the reason code.
    assert body.get("status") == "error"
    assert body.get("reason") == "db_unavailable"
    # The exception class name is surfaced (no traceback / no internal paths).
    assert body.get("error_class") == "RuntimeError"
    assert "_disclaimer" in body


def test_response_always_wrapped_with_disclaimer_on_error(
    tmp_path: Path, monkeypatch
) -> None:
    """Sweep through the error paths and confirm none of them ever skip
    the disclaimer wrap. The legal posture is load-bearing on every code
    path including failure modes."""
    monkeypatch.setenv("RBI_SOURCE_DB", str(tmp_path / "sweep.sqlite"))

    cases = [
        ("rbi_unknown", {}),
        ("rbi_check_compliance", {"text": "x" * (MAX_INPUT_LEN + 1)}),
        ("rbi_search", {"query": "x" * (MAX_INPUT_LEN + 1)}),
    ]
    for name, args in cases:
        body = _call_tool(name, args)
        assert "_disclaimer" in body, f"missing _disclaimer in {name} response"
        assert "_llm_instruction" in body, f"missing _llm_instruction in {name}"
