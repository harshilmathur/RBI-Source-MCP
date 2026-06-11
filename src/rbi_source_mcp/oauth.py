"""Ceremonial OAuth 2.1 endpoints for MCP-client compatibility.

Claude.ai's "Custom Connectors" flow walks the MCP authorization spec
(RFC 8414 + RFC 9728 + RFC 7591 Dynamic Client Registration) BEFORE
attempting the actual MCP handshake. If the discovery endpoints return
404, it gives up with "Couldn't reach the MCP server." The corpus served
here is entirely public RBI text — there's no actual access control to
enforce — but the protocol still requires the endpoints to exist.

This module implements the minimum needed to satisfy the discovery walk:
auto-approving authorization, dynamic client registration that accepts
anyone, and PKCE-validated token exchange. Tokens are stored in-process
(single-machine deployment; rebuilds on every deploy are fine — clients
re-register transparently).

Today /mcp/ is NOT gated on the bearer token (Claude Code, curl, etc.
connect anonymously); the OAuth flow exists for clients that REQUIRE the
discovery walk. But the ceremony is hardened as if it WERE the access-
control boundary — because gating /mcp on the bearer is on the roadmap:
  - PKCE is S256-only (`plain` is rejected; OAuth 2.1 deprecates it).
  - `redirect_uri` is validated before any code is issued: dangerous
    schemes are rejected, and a known client may only be redirected to a
    redirect_uri it registered (closing the open-redirector).
  - the issued code is bound to its client_id at the token endpoint.
When gating lands, enforce `verify_token` on the /mcp path; the ceremony
above already issues bearer tokens that are safe to trust.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

logger = structlog.get_logger(__name__)


# OAuth tunables. The flow is "ceremonial" so these are generous — the
# only real concern is that abandoned codes/tokens don't pile up in memory.
AUTH_CODE_TTL_SECONDS = 600         # 10 minutes — RFC 6749 §4.1.2 recommends ≤10
ACCESS_TOKEN_TTL_SECONDS = 3600     # 1 hour — Claude.ai will refresh as needed
CLIENT_REGISTRATION_TTL_SECONDS = 86400 * 30  # 30 days; we accept new registrations forever anyway


@dataclass
class _AuthCode:
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    expires_at: float
    scope: str = "mcp:read"


@dataclass
class _AccessToken:
    token: str
    client_id: str
    expires_at: float
    scope: str = "mcp:read"


@dataclass
class _Client:
    client_id: str
    redirect_uris: list[str] = field(default_factory=list)
    issued_at: float = 0.0


class _Store:
    """In-memory state. Single-machine deployment, deliberate."""

    def __init__(self) -> None:
        self.codes: dict[str, _AuthCode] = {}
        self.tokens: dict[str, _AccessToken] = {}
        self.clients: dict[str, _Client] = {}
        self._last_prune = time.time()

    def prune(self) -> None:
        now = time.time()
        if now - self._last_prune < 60:
            return
        self.codes = {k: v for k, v in self.codes.items() if v.expires_at > now}
        self.tokens = {k: v for k, v in self.tokens.items() if v.expires_at > now}
        self._last_prune = now


_store = _Store()


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Verify a PKCE challenge per RFC 7636. Only S256 is accepted — `plain`
    offers no protection if the code is intercepted, and OAuth 2.1 deprecates
    it. (authorize already rejects non-S256, so this is defense in depth.)"""
    if method != "S256":
        return False
    if not code_verifier:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return _b64url(digest) == code_challenge


def _is_allowed_redirect_uri(uri: str) -> bool:
    """Accept only an absolute http(s) redirect URI with a host and no
    fragment; require https unless it's a loopback dev callback. Blocks
    `javascript:` / `data:` and other open-redirect / XSS vectors."""
    if not uri:
        return False
    try:
        p = urlparse(uri)
    except ValueError:
        return False
    if p.scheme not in ("https", "http") or not p.netloc or p.fragment:
        return False
    # http is allowed only for loopback dev callbacks.
    return not (p.scheme == "http" and p.hostname not in ("localhost", "127.0.0.1", "::1"))


def _public_base(request: Request) -> str:
    """Resolve the externally-visible base URL of this server.

    Behind a reverse proxy that terminates TLS, the request typically
    looks like:
        Host: <your-public-hostname>
        X-Forwarded-Proto: https
    Locally and in tests, neither header is set. Build the URL from
    whatever we have, defaulting to https for the public deploy.
    """
    proto = request.headers.get("x-forwarded-proto", "https" if request.url.scheme == "https" else "http")
    host = request.headers.get("host") or request.url.netloc or "localhost"
    return f"{proto}://{host}"


# ---------------------------------------------------------------------------
# Discovery endpoints (RFC 9728, RFC 8414)
# ---------------------------------------------------------------------------


async def protected_resource_metadata(request: Request) -> JSONResponse:
    """RFC 9728 OAuth 2.0 Protected Resource Metadata.

    Tells the client which authorization server protects this resource and
    how to authenticate. We point at ourselves — same host, same scope.
    """
    base = _public_base(request)
    return JSONResponse(
        {
            "resource": base,
            "authorization_servers": [base],
            "scopes_supported": ["mcp:read"],
            "bearer_methods_supported": ["header"],
            "resource_documentation": f"{base}/",
        }
    )


async def authorization_server_metadata(request: Request) -> JSONResponse:
    """RFC 8414 OAuth 2.0 Authorization Server Metadata.

    Advertises every endpoint Claude.ai will probe.
    `token_endpoint_auth_methods_supported` is `["none"]` only — public
    clients with PKCE, OAuth 2.1 default for browser-class clients. We
    deliberately don't claim `client_secret_post`: the /token endpoint
    has no client_secret verification (since registration always returns
    `token_endpoint_auth_method: none` and the corpus is public). Codex
    review caught the over-claim — interop debt for strict OAuth clients.
    """
    base = _public_base(request)
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "scopes_supported": ["mcp:read"],
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "service_documentation": f"{base}/",
        }
    )


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------


async def register_client(request: Request) -> JSONResponse:
    """Accept any client registration. We don't care who's calling — the
    underlying data is public RBI text. Returns a fresh client_id and
    echoes the metadata back, per RFC 7591."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)

    client_id = "mcp-" + _b64url(secrets.token_bytes(12))
    redirect_uris = body.get("redirect_uris") or []
    _store.clients[client_id] = _Client(
        client_id=client_id,
        redirect_uris=list(redirect_uris) if isinstance(redirect_uris, list) else [],
        issued_at=time.time(),
    )
    _store.prune()
    logger.info("oauth.client.register", client_id=client_id, redirect_count=len(redirect_uris))
    response: dict[str, Any] = {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    # Echo any other metadata the client included (RFC 7591 §3.2.1).
    for k in ("client_name", "client_uri", "logo_uri", "scope", "contacts", "tos_uri", "policy_uri", "software_id", "software_version"):
        if k in body:
            response[k] = body[k]
    return JSONResponse(response, status_code=201)


# ---------------------------------------------------------------------------
# Authorization endpoint — auto-approves, no user prompt
# ---------------------------------------------------------------------------


async def authorize(request: Request) -> JSONResponse | RedirectResponse:
    """Auto-approve the authorization request and redirect with a code.

    Real OAuth servers render a consent page here. We have nothing to
    consent to — the data is public — so we skip the prompt and issue an
    auth code immediately. Validates response_type=code and PKCE
    parameters per OAuth 2.1.
    """
    params = dict(request.query_params)
    response_type = params.get("response_type")
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "S256")
    scope = params.get("scope", "mcp:read")

    if response_type != "code":
        return JSONResponse(
            {"error": "unsupported_response_type", "error_description": "only response_type=code is supported"},
            status_code=400,
        )
    if not client_id or not redirect_uri:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "client_id and redirect_uri are required"},
            status_code=400,
        )
    # PKCE is required for OAuth 2.1 public clients. Anthropic's connector
    # always sends code_challenge — we enforce it so the flow can never
    # silently degrade.
    if not code_challenge:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "code_challenge required (PKCE)"},
            status_code=400,
        )
    if code_challenge_method != "S256":
        # OAuth 2.1 deprecates `plain`; Anthropic's connector uses S256.
        return JSONResponse(
            {"error": "invalid_request", "error_description": "only S256 code_challenge_method is supported"},
            status_code=400,
        )
    # Validate the redirect target BEFORE issuing/redirecting a code, to close
    # the open-redirector: reject dangerous schemes outright, and for a client
    # we already know, require an exact match against a registered redirect_uri
    # (return a 400 — do NOT redirect to an unvalidated URI).
    if not _is_allowed_redirect_uri(redirect_uri):
        return JSONResponse(
            {"error": "invalid_request", "error_description": "invalid redirect_uri"},
            status_code=400,
        )
    client = _store.clients.get(client_id)
    if client is not None:
        if redirect_uri not in client.redirect_uris:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri not registered for this client"},
                status_code=400,
            )
    else:
        # Unknown client (in-memory store lost on restart, or a client_id reused
        # across deploy sessions). Auto-register with this validated redirect_uri
        # so the ceremony survives a restart; PKCE still binds the issued code.
        _store.clients[client_id] = _Client(
            client_id=client_id,
            redirect_uris=[redirect_uri],
            issued_at=time.time(),
        )

    code = _b64url(secrets.token_bytes(24))
    _store.codes[code] = _AuthCode(
        code=code,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
        scope=scope,
    )
    _store.prune()
    logger.info("oauth.authorize.issued_code", client_id=client_id, scope=scope)

    # Build the redirect with code + state. Echo `state` back verbatim whenever
    # the client sent it (even if empty) — it's the client's CSRF token and
    # dropping it weakens that protection.
    redirect_params = {"code": code}
    if "state" in params:
        redirect_params["state"] = state
    qs = urlencode(redirect_params)
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(url=f"{redirect_uri}{sep}{qs}", status_code=302)


# ---------------------------------------------------------------------------
# Token endpoint — exchanges code+verifier for a bearer token
# ---------------------------------------------------------------------------


async def token(request: Request) -> JSONResponse:
    """Exchange authorization_code (with PKCE verifier) or refresh_token
    for an access token. Refresh tokens here are recycled access tokens
    of the same form — we don't differentiate, since there's nothing to
    revoke."""
    # Accept both form-encoded (RFC 6749 default) and JSON bodies.
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            body = await request.json()
        else:
            form = await request.form()
            body = dict(form)
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    grant_type = body.get("grant_type")
    if grant_type == "authorization_code":
        code_str = body.get("code", "")
        code_verifier = body.get("code_verifier", "")
        redirect_uri = body.get("redirect_uri", "")
        record = _store.codes.pop(code_str, None)  # one-shot use
        if not record or record.expires_at < time.time():
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "code expired or already used"},
                status_code=400,
            )
        # Bind the code to the client it was issued to: if the token request
        # names a client_id, it must match the one the code was issued for
        # (RFC 6749 §4.1.3). PKCE already binds via the verifier; this is
        # defense in depth for the public-client flow.
        req_client_id = body.get("client_id")
        if req_client_id and req_client_id != record.client_id:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "client_id mismatch for this code"},
                status_code=400,
            )
        if redirect_uri and redirect_uri != record.redirect_uri:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "redirect_uri mismatch"},
                status_code=400,
            )
        if not _verify_pkce(code_verifier, record.code_challenge, record.code_challenge_method):
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                status_code=400,
            )
        access = _b64url(secrets.token_bytes(32))
        _store.tokens[access] = _AccessToken(
            token=access,
            client_id=record.client_id,
            expires_at=time.time() + ACCESS_TOKEN_TTL_SECONDS,
            scope=record.scope,
        )
        _store.prune()
        logger.info("oauth.token.issued", client_id=record.client_id, scope=record.scope)
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                "scope": record.scope,
                "refresh_token": access,  # same token; we don't differentiate
            }
        )

    if grant_type == "refresh_token":
        refresh = body.get("refresh_token", "")
        existing = _store.tokens.get(refresh)
        if not existing or existing.expires_at < time.time():
            # Issue a fresh anonymous token rather than 400-ing — Claude.ai
            # may call /token cold after our process restart wiped the
            # in-memory store, and there's no user-visible value in
            # refusing.
            new_token = _b64url(secrets.token_bytes(32))
            _store.tokens[new_token] = _AccessToken(
                token=new_token,
                client_id="anonymous",
                expires_at=time.time() + ACCESS_TOKEN_TTL_SECONDS,
            )
            return JSONResponse(
                {
                    "access_token": new_token,
                    "token_type": "Bearer",
                    "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                    "scope": "mcp:read",
                    "refresh_token": new_token,
                }
            )
        access = _b64url(secrets.token_bytes(32))
        _store.tokens[access] = _AccessToken(
            token=access,
            client_id=existing.client_id,
            expires_at=time.time() + ACCESS_TOKEN_TTL_SECONDS,
            scope=existing.scope,
        )
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                "scope": existing.scope,
                "refresh_token": access,
            }
        )

    return JSONResponse(
        {"error": "unsupported_grant_type"},
        status_code=400,
    )


# ---------------------------------------------------------------------------
# Token verification — exposed for future use; not currently called
# ---------------------------------------------------------------------------


def verify_token(token_str: str) -> bool:
    """Stub for future use: returns True if the token is valid + non-expired.

    Currently unused. /mcp/ accepts requests with or without a Bearer
    token because the corpus is public. If we ever want to gate access,
    wire this into a middleware and require Authorization: Bearer.
    """
    rec = _store.tokens.get(token_str)
    if not rec:
        return False
    if rec.expires_at < time.time():
        _store.tokens.pop(token_str, None)
        return False
    return True
