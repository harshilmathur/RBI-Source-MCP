"""Tests for the ceremonial OAuth 2.1 endpoints.

Claude.ai's "Custom Connectors" flow walks the MCP authorization spec
(RFC 8414 + RFC 9728 + RFC 7591) before doing the actual MCP handshake.
If the discovery endpoints 404, Claude.ai bails with "Couldn't reach
the MCP server." These tests guard the full discovery → register →
authorize → token → bearer flow.

Backward compat: /mcp/ MUST still accept requests without a Bearer
token — Claude Code, curl, and similar clients connect anonymously.
The OAuth flow is purely for clients that REQUIRE the discovery walk.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from asgi_lifespan import LifespanManager


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) — S256."""
    verifier = _b64url(b"x" * 32)  # deterministic for the test
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


@pytest.mark.asyncio
async def test_protected_resource_metadata_returns_valid_doc() -> None:
    """RFC 9728: /.well-known/oauth-protected-resource must return a doc
    pointing at the authorization server."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    assert "resource" in body
    assert isinstance(body["authorization_servers"], list)
    assert body["authorization_servers"], "authorization_servers must not be empty"
    assert "mcp:read" in body["scopes_supported"]


@pytest.mark.asyncio
async def test_protected_resource_metadata_with_mcp_subpath() -> None:
    """Claude.ai also probes /.well-known/oauth-protected-resource/mcp.
    Must return the same doc to satisfy whichever endpoint they try first."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200, "Claude.ai probes both this path and the bare one"
    body = r.json()
    assert "authorization_servers" in body


@pytest.mark.asyncio
async def test_authorization_server_metadata_advertises_all_endpoints() -> None:
    """RFC 8414: must advertise authorization_endpoint, token_endpoint,
    registration_endpoint, plus PKCE support."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    body = r.json()
    for required in (
        "issuer", "authorization_endpoint", "token_endpoint",
        "registration_endpoint", "response_types_supported",
        "grant_types_supported", "code_challenge_methods_supported",
    ):
        assert required in body, f"missing required field: {required}"
    assert "S256" in body["code_challenge_methods_supported"]
    assert "authorization_code" in body["grant_types_supported"]


@pytest.mark.asyncio
async def test_dynamic_client_registration_returns_client_id() -> None:
    """RFC 7591: POST /register accepts any client metadata and returns a
    fresh client_id. We accept anyone — the data is public."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/register",
                json={
                    "client_name": "test-client",
                    "redirect_uris": ["https://claude.ai/callback"],
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            )
    assert r.status_code == 201
    body = r.json()
    assert body["client_id"].startswith("mcp-")
    assert body["client_name"] == "test-client"
    assert body["redirect_uris"] == ["https://claude.ai/callback"]


@pytest.mark.asyncio
async def test_full_oauth_flow_authcode_to_bearer_token() -> None:
    """End-to-end: register → authorize → token. The output bearer token
    is what Claude.ai will then attach to MCP requests."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app()
    verifier, challenge = _pkce_pair()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Register a fresh client.
            reg = await client.post(
                "/register",
                json={"redirect_uris": ["http://test/cb"], "client_name": "itest"},
            )
            assert reg.status_code == 201
            client_id = reg.json()["client_id"]

            # 2. Authorize — should redirect to redirect_uri with ?code=...
            auth = await client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": "http://test/cb",
                    "state": "xyz",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "scope": "mcp:read",
                },
                follow_redirects=False,
            )
            assert auth.status_code == 302, f"expected 302, got {auth.status_code}: {auth.text[:200]}"
            location = auth.headers["location"]
            qs = parse_qs(urlparse(location).query)
            assert qs["state"] == ["xyz"]
            code = qs["code"][0]

            # 3. Exchange code + verifier for an access token.
            tok = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "http://test/cb",
                    "code_verifier": verifier,
                    "client_id": client_id,
                },
            )
    assert tok.status_code == 200, f"token exchange failed: {tok.status_code} {tok.text[:200]}"
    body = tok.json()
    assert body["token_type"] == "Bearer"
    assert "access_token" in body and len(body["access_token"]) > 20
    assert body["expires_in"] > 0


@pytest.mark.asyncio
async def test_token_exchange_rejects_wrong_pkce_verifier() -> None:
    """PKCE verifier mismatch must fail — otherwise the OAuth ceremony
    is meaningless and any code can be redeemed by anyone who saw it."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app()
    _, challenge = _pkce_pair()  # store the challenge
    wrong_verifier = "different-verifier-totally-wrong"

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            reg = await client.post(
                "/register",
                json={"redirect_uris": ["http://test/cb"], "client_name": "itest"},
            )
            client_id = reg.json()["client_id"]
            auth = await client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": "http://test/cb",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            code = parse_qs(urlparse(auth.headers["location"]).query)["code"][0]
            tok = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "http://test/cb",
                    "code_verifier": wrong_verifier,
                },
            )
    assert tok.status_code == 400
    assert tok.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_authorize_rejects_missing_pkce() -> None:
    """OAuth 2.1 mandates PKCE for public clients — refuse to issue a
    code without code_challenge."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": "anything",
                    "redirect_uri": "http://test/cb",
                    # no code_challenge
                },
                follow_redirects=False,
            )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_authorize_code_is_one_shot() -> None:
    """RFC 6749 §4.1.2: an authorization code MUST only be redeemable
    once. A second token exchange with the same code must fail."""
    from rbi_source_mcp.server_http import build_asgi_app

    app = build_asgi_app()
    verifier, challenge = _pkce_pair()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            reg = await client.post(
                "/register",
                json={"redirect_uris": ["http://test/cb"]},
            )
            client_id = reg.json()["client_id"]
            auth = await client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": "http://test/cb",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            code = parse_qs(urlparse(auth.headers["location"]).query)["code"][0]
            # First redemption succeeds.
            ok = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "http://test/cb",
                    "code_verifier": verifier,
                },
            )
            assert ok.status_code == 200
            # Second attempt with the same code must fail.
            replay = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "http://test/cb",
                    "code_verifier": verifier,
                },
            )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_oauth_endpoints_use_separate_looser_bucket() -> None:
    """Claude.ai's connector hits the OAuth ceremony 5-8x per setup. They
    must NOT count against the main per-IP budget (real MCP traffic right
    after the handshake would 429 immediately). But they also must not be
    fully exempt — codex review caught the unlimited-OAuth DoS gap.

    Behavior under test: OAuth endpoints share their own bucket
    (OAUTH_RATE_LIMIT_PER_WINDOW=30), independent from the main bucket.
    The connector ceremony fits comfortably; real MCP traffic afterwards
    is unaffected; an attacker hammering /token/`/authorize` still gets
    429 once the OAuth bucket fills.
    """
    from rbi_source_mcp.server_http import (
        OAUTH_RATE_LIMIT_PER_WINDOW,
        RATE_LIMIT_PER_WINDOW,
        build_asgi_app,
    )

    app = build_asgi_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Realistic OAuth ceremony (8 hits) succeeds.
            for _ in range(8):
                r = await client.get("/.well-known/oauth-authorization-server")
                assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"

            # 2. Slam past the OAuth bucket cap → 429 eventually arrives.
            saw_429 = False
            for _ in range(OAUTH_RATE_LIMIT_PER_WINDOW + 5):
                r = await client.get("/.well-known/oauth-authorization-server")
                if r.status_code == 429:
                    saw_429 = True
                    break
            assert saw_429, "OAuth endpoints must enforce a separate looser limit"

            # 3. Main bucket is independent — /health stays clean (excluded
            # entirely). Sanity-check the bucket separation.
            assert RATE_LIMIT_PER_WINDOW > OAUTH_RATE_LIMIT_PER_WINDOW
            r = await client.get("/health")
            assert r.status_code in (200, 503)  # excluded from rate limit
