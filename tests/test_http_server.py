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
async def test_banner_returns_html_for_browser_accept() -> None:
    """When Accept includes text/html (real browsers), GET / returns the
    static homepage instead of the JSON banner. Asserts the page contains
    the load-bearing pieces: title, hero, install matrix, disclaimer."""
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
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    # Load-bearing structural pieces — if any of these break, the page
    # has regressed in a way that hurts UX or removes the disclaimer.
    assert "<!doctype html>" in body.lower()
    assert "RBI Source MCP" in body
    assert "https://rbi-source.harshil.ai/mcp/" in body
    assert "Install in 30s" in body
    assert "NOT LEGAL ADVICE" in body
    assert "rbi_check_compliance" in body  # tools section uses underscored names
    # CSP header present (defense-in-depth on the static page)
    assert "default-src" in response.headers.get("content-security-policy", "")


@pytest.mark.asyncio
async def test_favicon_endpoints_serve_with_correct_content_types() -> None:
    """Each whitelisted favicon URL must serve its file with the right
    Content-Type and a long Cache-Control. Modern browsers fan out across
    multiple <link rel="icon">; if any 404s, the user sees a console warning
    and the browser falls back to /favicon.ico (which is the cheapest probe
    so we want it to work too)."""
    from rbi_source_mcp.server_http import build_asgi_app

    cases = [
        ("/favicon.svg",          "image/svg+xml"),
        ("/favicon-16.png",       "image/png"),
        ("/favicon-32.png",       "image/png"),
        ("/favicon-512.png",      "image/png"),
        ("/apple-touch-icon.png", "image/png"),
        ("/favicon.ico",          "image/x-icon"),
    ]
    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for url, expected_ct in cases:
                r = await client.get(url)
                assert r.status_code == 200, f"{url} → {r.status_code}"
                assert expected_ct in r.headers.get("content-type", ""), (
                    f"{url} content-type={r.headers.get('content-type')}"
                )
                assert "max-age" in r.headers.get("cache-control", ""), (
                    f"{url} missing cache-control"
                )
                assert len(r.content) > 0, f"{url} empty body"


@pytest.mark.asyncio
async def test_static_asset_path_traversal_returns_404() -> None:
    """The static handler keys off an allowlist, not a constructed file
    path — but verify defensively that traversal attempts get a clean 404,
    not a leak. This guards regression if someone ever swaps the allowlist
    for a `_STATIC_DIR / request.path_params['name']` pattern."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Anything not in _STATIC_ALLOWLIST should miss routing entirely
            # (Starlette returns 404). This isn't path traversal in the strict
            # sense, but it's the contract: only listed names are reachable.
            for url in ("/favicon-99.png", "/index.html", "/server.py", "/etc/passwd"):
                r = await client.get(url)
                assert r.status_code == 404, f"{url} returned {r.status_code} (expected 404)"


@pytest.mark.asyncio
async def test_homepage_links_to_all_favicon_files() -> None:
    """The <link rel="icon"> tags in index.html must reference URLs that
    actually exist as routes. Otherwise tabs render the default browser
    fallback and the favicon ships dead."""
    from rbi_source_mcp.server_http import _STATIC_ALLOWLIST, build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/",
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
    body = r.text
    # Every favicon URL declared in <link rel="icon"> must be in the allowlist.
    for url in ("/favicon.svg", "/favicon-32.png", "/favicon-16.png",
                "/apple-touch-icon.png", "/favicon.ico"):
        assert url in body, f"<link> for {url} missing from index.html"
        assert url in _STATIC_ALLOWLIST, f"{url} declared in HTML but not whitelisted"


@pytest.mark.asyncio
async def test_banner_returns_json_when_client_explicitly_wants_json() -> None:
    """A client that includes Accept: application/json (most MCP clients do)
    must get JSON even if it ALSO accepts text/html. The asymmetric rule
    in `_wants_html` exists to keep machine callers on the JSON contract
    by default."""
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
