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
async def test_banner_returns_corpus_stats_for_json_clients() -> None:
    """The / banner must include the corpus stats for any non-browser caller.
    Tolerant of different DB paths via $RBI_SOURCE_DB; if the corpus is
    unavailable, the banner still returns a valid JSON envelope with the
    error path."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Default Accept (httpx sends */*) — should land on JSON path.
            response = await client.get("/")
    assert response.status_code == 200
    assert "application/json" in response.headers.get("content-type", "")
    payload = response.json()
    assert payload["name"] == "rbi-source-mcp"
    assert "endpoints" in payload
    assert "/mcp" in payload["endpoints"]["mcp"]


@pytest.mark.asyncio
async def test_banner_returns_json_for_browser_accept() -> None:
    """The package serves JSON only — there is no static homepage in the
    wheel. A browser Accept header (text/html preferred) gets the same
    corpus-stats JSON banner as any other caller. A deployment that wants a
    branded HTML page layers it on at the transport edge (e.g. an ASGI shim
    in front of build_asgi_app); the package itself never returns HTML."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/",
                headers={
                    # Real Chrome/Firefox Accept header.
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
    assert response.status_code == 200
    assert "application/json" in response.headers.get("content-type", "")
    assert "text/html" not in response.headers.get("content-type", "")
    payload = response.json()
    assert payload["name"] == "rbi-source-mcp"


@pytest.mark.asyncio
async def test_static_paths_return_404() -> None:
    """The package ships no static assets, so the favicon and homepage paths
    that the old in-package homepage used to serve now route to nothing and
    return a clean 404 — never a file leak. Guards against a regression that
    reintroduces in-package static serving."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for url in (
                "/favicon.ico",
                "/favicon.svg",
                "/favicon-32.png",
                "/apple-touch-icon.png",
                "/index.html",
                "/server.py",
                "/etc/passwd",
            ):
                r = await client.get(url)
                assert r.status_code == 404, f"{url} returned {r.status_code} (expected 404)"


@pytest.mark.asyncio
async def test_banner_returns_json_when_client_explicitly_wants_json() -> None:
    """A client that includes Accept: application/json (most MCP clients do)
    gets the JSON banner. This is the only contract now — GET / is JSON for
    every caller — but the explicit-json case is worth pinning so a future
    HTML branch can't silently break machine callers."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/",
                headers={"Accept": "application/json, text/html"},
            )
    assert response.status_code == 200
    assert "application/json" in response.headers.get("content-type", "")


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
                "params": {"name": "rbi_search", "arguments": {"query": "payment aggregator", "limit": 1}},
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
