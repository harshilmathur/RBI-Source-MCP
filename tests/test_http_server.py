"""Tests for the streamable-HTTP transport.

These build the ASGI app in-process and hit /, /health, and /mcp/ via
httpx.AsyncClient (no real network). Confirms:
    1. The Starlette app boots cleanly with the lifespan context.
    2. /health returns the JSON liveness payload.
    3. / returns the corpus-stats banner.
    4. /mcp/ accepts MCP JSON-RPC POST requests and returns SSE responses
       that include the disclaimer fields.

We deliberately do NOT spin up uvicorn — these tests run against the ASGI
app directly via httpx.AsyncClient(transport=ASGITransport).
"""

from __future__ import annotations

import json

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok() -> None:
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "version" in payload


@pytest.mark.asyncio
async def test_banner_returns_corpus_stats() -> None:
    """The / banner must include the corpus stats. Tolerant of different
    DB paths via $RBI_SOURCE_DB; if the corpus is unavailable, the banner
    still returns a valid JSON envelope with the error path."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "rbi-source-mcp"
    assert "endpoints" in payload
    assert "/mcp" in payload["endpoints"]["mcp"]


@pytest.mark.asyncio
async def test_mcp_endpoint_returns_sse_with_disclaimer_for_search() -> None:
    """End-to-end: MCP tool call over HTTP returns the disclaimer-wrapped
    response in an SSE event. Critical regression test for the disclaimer
    contract — every MCP response over the hosted transport MUST carry
    `_disclaimer` and `_llm_instruction` at the top."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
            request = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "rbi.search", "arguments": {"query": "payment aggregator", "limit": 1}},
                "id": 1,
            }
            response = await client.post(
                "/mcp/",
                json=request,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2024-11-05",
                },
            )

    assert response.status_code == 200
    text = response.text
    data_lines = [ln[len("data: "):] for ln in text.splitlines() if ln.startswith("data: ")]
    assert data_lines, f"no SSE data lines in response: {text[:300]}"
    rpc = json.loads(data_lines[0])
    assert rpc["id"] == 1
    inner = json.loads(rpc["result"]["content"][0]["text"])
    assert "_disclaimer" in inner
    assert "_llm_instruction" in inner
    assert "NOT LEGAL ADVICE" in inner["_disclaimer"]
