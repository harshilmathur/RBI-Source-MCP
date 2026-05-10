"""Tests for the HTTP middleware introduced after /cso review (2026-04-29).

Two middlewares to guard:
    - MaxBodySizeMiddleware: rejects requests with Content-Length > 200 KB
      BEFORE Starlette parses the body. /cso finding #4.
    - RateLimitMiddleware: per-IP fixed window. 60 req per 60 s. Excludes
      /health (Fly probes) and / (banner). /cso finding #2.

These tests boot the ASGI app in-process and hit it via httpx.AsyncClient.
"""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager


@pytest.mark.asyncio
async def test_request_exceeding_body_cap_rejected_with_413() -> None:
    """Content-Length above MAX_REQUEST_BODY_BYTES → 413 before parsing."""
    from rbi_source_mcp.server_http import MAX_REQUEST_BODY_BYTES, build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Send a body just over the cap; we set Content-Length explicitly
            # to make the test deterministic regardless of httpx's chunking.
            oversized = b"x" * (MAX_REQUEST_BODY_BYTES + 1)
            response = await client.post(
                "/mcp/",
                content=oversized,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(oversized)),
                    "Accept": "application/json, text/event-stream",
                },
            )
    assert response.status_code == 413
    payload = response.json()
    assert payload["error"] == "request_too_large"
    assert payload["max_bytes"] == MAX_REQUEST_BODY_BYTES


@pytest.mark.asyncio
async def test_mcp_no_trailing_slash_does_not_redirect() -> None:
    """Claude.ai's connector posts to /mcp (no slash) carrying the bearer
    token AFTER OAuth, but doesn't follow 307 redirects. The path must
    work directly without a redirect, otherwise the token never arrives
    at the handler and Claude.ai bails with 'Authorization with the MCP
    server failed.'"""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/mcp",  # ← no trailing slash
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2024-11-05",
                },
                follow_redirects=False,
            )
    assert r.status_code == 200, f"got {r.status_code}: expected 200 (no redirect)"
    assert "rbi_search" in r.text or "tools" in r.text, "tools/list response not received"


@pytest.mark.asyncio
async def test_request_within_body_cap_passes_middleware() -> None:
    """A small payload under the cap must pass the middleware. Asserts the
    body-size check doesn't accidentally reject normal traffic. We hit
    /health (cheap, doesn't need an MCP session) since the middleware runs
    on every path."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_excludes_health_probe() -> None:
    """Fly hits /health every 30s. The rate limit must NOT count those —
    otherwise the LB-probe traffic would burn the budget for any real
    client sharing an IP (e.g., behind NAT)."""
    from rbi_source_mcp.server_http import RATE_LIMIT_PER_WINDOW, build_asgi_app

    app = build_asgi_app(stateless=True)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Hit /health more than the limit — none should 429.
            for _ in range(RATE_LIMIT_PER_WINDOW + 5):
                r = await client.get("/health")
                assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"


@pytest.mark.asyncio
async def test_rate_limit_kicks_in_for_mcp_endpoint() -> None:
    """Past `RATE_LIMIT_PER_WINDOW` requests on /mcp/ from the same IP must
    return 429 with a Retry-After header.

    We use a temporary app with a tight limit (3 req per 60 s) to avoid the
    cost of 60+ embedder calls in the test. The middleware logic is
    independent of the limit number."""
    # Build a fresh app with a tight rate limit. We can't reconfigure
    # build_asgi_app's limits without parametrizing it, so we inject a
    # standalone Starlette app with just the rate-limit middleware.
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from rbi_source_mcp.server_http import (
        MaxBodySizeMiddleware,
        RateLimitMiddleware,
    )

    async def echo(request):  # noqa: ANN001
        return JSONResponse({"ok": True})

    tight_app = Starlette(routes=[Route("/mcp/{path:path}", echo, methods=["POST"])])
    tight_app.add_middleware(RateLimitMiddleware, limit=3, window_s=60.0)
    tight_app.add_middleware(MaxBodySizeMiddleware)

    transport = httpx.ASGITransport(app=tight_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # First 3 succeed.
        for i in range(3):
            r = await client.post("/mcp/x", json={"i": i})
            assert r.status_code == 200, f"req {i + 1} got {r.status_code}"
        # 4th gets 429.
        r = await client.post("/mcp/x", json={"i": 99})
        assert r.status_code == 429
        body = r.json()
        assert body["error"] == "rate_limit_exceeded"
        assert body["limit"] == 3
        assert "retry_after_seconds" in body
        assert "Retry-After" in r.headers


@pytest.mark.asyncio
async def test_rate_limit_isolates_clients_by_ip() -> None:
    """Two different client IPs must get independent rate-limit buckets.
    Otherwise an attacker on IP A would lock out a legitimate user on IP B.

    We forge `Fly-Client-IP` (which the middleware reads) since both
    requests come from the same in-process ASGITransport."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from rbi_source_mcp.server_http import MaxBodySizeMiddleware, RateLimitMiddleware

    async def echo(request):  # noqa: ANN001
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp/{path:path}", echo, methods=["POST"])])
    # Explicitly opt into the Fly-Client-IP header. The middleware default
    # is empty (peer IP only) so any caller can't spoof XFF; tests that
    # exercise proxy-header behavior pass the trusted set explicitly,
    # mirroring the production Dockerfile env config.
    app.add_middleware(
        RateLimitMiddleware,
        limit=2,
        window_s=60.0,
        trusted_proxy_headers=("fly-client-ip",),
    )
    app.add_middleware(MaxBodySizeMiddleware)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # IP A burns its budget.
        for i in range(2):
            r = await client.post("/mcp/x", json={"i": i}, headers={"Fly-Client-IP": "10.0.0.1"})
            assert r.status_code == 200
        r = await client.post("/mcp/x", json={"i": 99}, headers={"Fly-Client-IP": "10.0.0.1"})
        assert r.status_code == 429

        # IP B is unaffected.
        r = await client.post("/mcp/x", json={"i": 0}, headers={"Fly-Client-IP": "10.0.0.2"})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_ignores_untrusted_proxy_headers() -> None:
    """Without an opt-in trusted-proxy-header list, the middleware must
    fall back to peer IP. Otherwise any client could spoof X-Forwarded-For
    to reset its bucket per-request. Codex review #M1 — this is the
    spoofability guarantee.
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from rbi_source_mcp.server_http import RateLimitMiddleware

    async def echo(request):  # noqa: ANN001
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp/{path:path}", echo, methods=["POST"])])
    # No trusted_proxy_headers passed — middleware uses peer IP only.
    app.add_middleware(RateLimitMiddleware, limit=2, window_s=60.0, trusted_proxy_headers=())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Spoof a different XFF on every request — same peer (127.0.0.1),
        # so all three requests must share the same bucket and the third
        # one must be rate-limited.
        for i in range(2):
            r = await client.post(
                "/mcp/x",
                json={"i": i},
                headers={"X-Forwarded-For": f"10.0.0.{i}", "Fly-Client-IP": f"10.0.0.{i}"},
            )
            assert r.status_code == 200
        r = await client.post(
            "/mcp/x",
            json={"i": 99},
            headers={"X-Forwarded-For": "10.0.0.99", "Fly-Client-IP": "10.0.0.99"},
        )
        assert r.status_code == 429
