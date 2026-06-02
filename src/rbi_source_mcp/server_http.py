"""Streamable-HTTP transport for the RBI Source MCP server.

The stdio server in `server.py` is what Claude Code / Claude Desktop spawn
locally. For HOSTED mode (cloud platforms behind a reverse proxy), we need
to speak the streamable-HTTP transport so any MCP client can connect via
`<HOSTED_URL>/mcp` without running a local subprocess.

This module reuses `_build_server()` from server.py — the tool definitions
and call dispatch are identical. We only swap the transport.

Run locally:
    rbi-source-mcp-http --host 0.0.0.0 --port 8080

Then a client can connect with:
    claude mcp add rbi-source --transport http http://localhost:8080/mcp
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import structlog
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from . import __version__
from . import oauth as _oauth
from .disclaimer import DISCLAIMER, LLM_INSTRUCTION
from .server import _build_server


def _wrap_transport_error(payload: dict) -> dict:
    """Inject the disclaimer + LLM instruction into a transport-layer error.

    Mirrors `server._wrap_response` so 413 (body too large) and 429
    (rate limited) responses carry the same legal-posture fields the
    rest of the API does. Previously these middleware paths emitted
    bare JSON without `_disclaimer`, which leaked past the documented
    "every response carries the disclaimer" contract.
    """
    return {
        "_disclaimer": DISCLAIMER,
        "_llm_instruction": LLM_INSTRUCTION,
        **payload,
    }

# Static homepage. Loaded once at import time and held in memory; mtime is
# checked on each request to support local dev (edit the file, refresh).
# Browsers see this; curl + MCP clients still get the JSON banner via
# Accept-header detection on the / route. See banner() below.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_PATH = _STATIC_DIR / "index.html"
_index_cache: dict[str, object] = {"bytes": None, "mtime": 0.0}


def _load_index_html() -> bytes | None:
    """Return the homepage HTML bytes (cached, mtime-aware). None on miss."""
    try:
        st = _INDEX_PATH.stat()
    except FileNotFoundError:
        return None
    if _index_cache["bytes"] is None or st.st_mtime != _index_cache["mtime"]:
        _index_cache["bytes"] = _INDEX_PATH.read_bytes()
        _index_cache["mtime"] = st.st_mtime
    return _index_cache["bytes"]  # type: ignore[return-value]


# Whitelist of static files served from /<filename>. Restricting to an
# explicit allowlist prevents path-traversal nonsense — only these exact
# filenames map to files under _STATIC_DIR. Add a new entry here when
# adding a new static asset; never construct the path from request input.
_STATIC_ALLOWLIST: dict[str, tuple[str, str]] = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon-16.png": ("favicon-16.png", "image/png"),
    "/favicon-32.png": ("favicon-32.png", "image/png"),
    "/favicon-512.png": ("favicon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}


def _validate_db_at_startup() -> None:
    """Open the corpus DB at startup and confirm it has documents.

    A misconfigured deploy (volume not mounted, corpus never installed,
    wrong RBI_SOURCE_DB path) can otherwise pass the process-up check
    and serve `/` and the OAuth metadata endpoints fine, while every
    `/mcp` tool call 500s. Crashing in lifespan startup makes the bad
    deploy visible to the orchestrator instead of running degraded.

    Failure handling, by error class:
    - `sqlite3.DatabaseError` (file is not a valid SQLite, malformed schema,
      I/O failure mid-read) → re-raise. The DB is fundamentally unusable;
      the orchestrator should crash-loop the machine, not route traffic.
    - Empty corpus (`doc_count == 0`) → log warning, continue. /health
      will return 503 and the orchestrator pulls the machine from rotation.
      This is a transient state during a corpus refresh / first deploy.
    - Other exceptions (path missing, permissions, transient sqlite locks)
      → log warning, continue. /health is the source of truth.

    Codex review #M3 / red-team: the previous "swallow everything" version
    contradicted its own docstring. Re-raising on DatabaseError aligns
    behavior with stated intent.
    """
    from .db import connect

    db_path = os.environ.get("RBI_SOURCE_DB", "./data/db.sqlite")
    try:
        with connect(db_path) as conn:
            doc_count = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        logger.error(
            "startup.corpus_corrupt",
            error=str(exc),
            exc_type=type(exc).__name__,
            db=db_path,
            note="DB file is unreadable / not a valid SQLite — failing startup",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "startup.db_validation_failed",
            error=str(exc),
            exc_type=type(exc).__name__,
            db=db_path,
            note="server still starts; /health will surface the failure",
        )
        return

    if doc_count == 0:
        logger.warning(
            "startup.corpus_empty",
            db=db_path,
            note=(
                "DB opens but has zero documents — /health will return 503. "
                "Run rbi-source-fetch-corpus or check the volume mount."
            ),
        )
    else:
        logger.info("startup.corpus_ok", db=db_path, documents=int(doc_count))


def _prewarm_embedder_sync() -> None:
    """Run the bge-small embedder once at lifespan startup so the first
    user request doesn't pay the cold-start tax.

    Two important behaviors:

    1. Synchronous (no asyncio.to_thread). Earlier version used
       `asyncio.create_task(_prewarm_embedder())` which scheduled a
       to_thread call concurrent with module-level imports — under
       NumPy 2.x that races into 'cannot import name NDArray from
       partially initialized module numpy._typing' (circular import
       across threads). Running synchronously in the lifespan startup
       eliminates the race; the asyncio loop is single-threaded at this
       point and no requests are being served yet (any platform's
       readiness grace_period covers the brief load window).

    2. Imports happen inside the function body (after lifespan startup
       has begun) so `rbi-source-mcp-http --help`, --version, etc. don't
       eat the full sentence-transformers + torch import cost.

    Failures are logged and swallowed: the embedder is best-effort. If
    the load fails, the server still serves — rbi_search and
    rbi_check_compliance fall back to FTS5-only retrieval via the
    existing try/except in mcp/search.py and mcp/check_compliance.py.
    """
    try:
        t0 = time.time()
        from .indexer.embed import embed_query

        logger.info("embedder.prewarm.start")
        embed_query("warmup query")
        logger.info("embedder.prewarm.done", duration_s=round(time.time() - t0, 2))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "embedder.prewarm.fail",
            error=str(exc),
            note="server still serves; first real call will retry the load",
        )


def _wants_html(request: Request) -> bool:
    """Decide whether GET / should return the HTML page or the JSON banner.

    Rule: HTML if Accept contains `text/html` AND does NOT contain
    `application/json`. This routes:
      - Browsers (Accept: text/html,...) → HTML
      - curl with no -H              (Accept: */*) → JSON (default)
      - MCP clients                  (Accept: application/json,...) → JSON
      - Probes that explicitly want JSON via Accept header → JSON

    The asymmetry is intentional: JSON is the safe default for unknown
    callers (machine-readable, identical to existing /banner contract).
    HTML is opt-in via a real browser's Accept header.
    """
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept and "application/json" not in accept


logger = structlog.get_logger(__name__)

# Banner cache: stats only change on daily refresh, so a 60-second TTL is
# fine. Without this, every GET / opens a fresh SQLite connection (with
# schema migration + extension load) — wasteful and trivially DOSable.
_BANNER_TTL_SECONDS = 60.0
_banner_cache: dict[str, object] = {"data": None, "ts": 0.0}

# /health cache: a load balancer / orchestrator readiness probe hits this every
# few seconds. The deep health check opens a SQLite connection + verifies
# sqlite-vec loaded + counts documents — that's ~10ms but adds up at probe
# cadence and contends with real traffic. 30s cache is short enough that a
# real outage flips the probe within one health-check window. Separate cache
# from the banner because the failure shapes differ (banner returns degraded
# JSON; health flips 503).
_HEALTH_TTL_SECONDS = 30.0
_health_cache: dict[str, object] = {"data": None, "status_code": 200, "ts": 0.0}

# HTTP-level request body cap. The application-layer cap on `text` and
# `query` (server.MAX_INPUT_LEN = 32_000) is enforced AFTER Starlette has
# parsed the request body, so an attacker can already burn memory by POSTing
# multi-MB bodies. 200 KB is plenty for the largest legitimate MCP call
# (32 KB text + JSON-RPC envelope + a few headers); reject everything bigger
# at the middleware layer before parsing. Caught by /cso review (#4, 2026-04-29).
MAX_REQUEST_BODY_BYTES = 200_000

# Per-IP rate limit (fixed window). Public unauthenticated endpoint, no auth,
# no captcha, single-machine deployment — without this, anyone can DoS or
# cost-amplify the embedder. 60 requests per 60 seconds per IP is generous
# for legitimate MCP clients (which batch tool calls in conversation), tight
# enough to make a tight-loop attacker visible. /health and / are excluded
# (orchestrator probes hit /health every 30s; banner is curl-friendly).
# Caught by /cso review (#2, 2026-04-29).
RATE_LIMIT_PER_WINDOW = 60
RATE_LIMIT_WINDOW_SECONDS = 60.0

# OAuth-ceremony rate limit (separate bucket). Claude.ai's connector flow
# hits the discovery+register+authorize+token sequence ~5-8 times per setup;
# 30 req/min per IP is generous for a real connector handshake but still
# catches a flood. Was previously a full exemption — codex review caught
# it as a DoS vector against /token / /authorize.
OAUTH_RATE_LIMIT_PER_WINDOW = 30
OAUTH_RATE_LIMIT_WINDOW_SECONDS = 60.0

# Trusted proxy headers — comma-separated, lowercased. If you run this
# behind a reverse proxy that injects a client-IP header (e.g.
# `cf-connecting-ip`, `fly-client-ip`, `x-forwarded-for`), set this env
# var to the comma-separated header names. Self-host installs that bind
# directly to a port leave this empty and fall back to the peer IP,
# which is correct when there is no proxy in front. Without this gate,
# any client can bypass the rate limit by sending its own
# `X-Forwarded-For` header.
#
# When honoring proxy headers in production, also set
# `RBI_TRUSTED_PROXY_CIDRS` to the proxy's egress range so a direct-
# connect client can't spoof the trusted header. See the
# `_TRUSTED_PROXY_CIDRS` block below.
_TRUSTED_PROXY_HEADERS: tuple[str, ...] = tuple(
    h.strip().lower()
    for h in os.environ.get("RBI_TRUSTED_PROXY_HEADERS", "").split(",")
    if h.strip()
)


def _parse_cidrs(value: str) -> tuple:
    """Parse a comma-separated list of CIDR blocks into ip_network objects.

    Empty / unset value returns an empty tuple — the default-deny posture
    described under `_TRUSTED_PROXY_CIDRS` below.
    """
    import ipaddress

    out = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            logger.warning("trusted_proxy_cidr.invalid", value=chunk)
    return tuple(out)


# Trusted proxy CIDR allowlist — comma-separated. Even when
# `RBI_TRUSTED_PROXY_HEADERS` is set, the rate-limit middleware will only
# honor those headers when the request's peer IP falls inside one of
# these CIDR blocks. Operators running behind a proxy should set this
# to the proxy's egress range (for Cloudflare, see their published
# IPv4/IPv6 ranges; for Fly internal traffic add `fdaa::/16`).
# Self-host installs leave it empty (and `_TRUSTED_PROXY_HEADERS` empty
# too) so the peer IP is always used.
#
# Without this CIDR gate, an operator who only set _PROXY_HEADERS (but
# whose deployment doesn't actually force traffic through the proxy)
# would let any client spoof a rate-limit bucket via the trusted header.
_TRUSTED_PROXY_CIDRS = _parse_cidrs(os.environ.get("RBI_TRUSTED_PROXY_CIDRS", ""))


class _NormalizeMcpPath:
    """ASGI-level middleware: rewrite /mcp to /mcp/ in the scope.

    Why ASGI-level instead of Starlette HTTP middleware: this needs to
    run BEFORE Starlette's routing, since the goal is to make Starlette
    see /mcp/ rather than redirect /mcp → /mcp/. Starlette's
    BaseHTTPMiddleware runs AFTER routing.

    Why this exists: Claude.ai's connector posts to /mcp (no trailing
    slash) carrying the bearer token after OAuth. It does not follow
    307 redirects, so the token never reaches the handler. We rewrite
    the path in the scope, transparently, so the request lands on the
    streamable-HTTP session manager directly with the bearer intact.

    Side benefit: any other client that drops the trailing slash also
    works without redirect, e.g., curl pipes that don't use -L.
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Reject requests with Content-Length above the cap before the body is
    parsed. Two paths:

    1. `Content-Length` header present → check it directly. Cheap, common.
    2. No `Content-Length` (chunked Transfer-Encoding, etc.) → consume the
       stream up to the cap; if it exceeds, abort. Slightly more complex.

    For our MCP traffic, all clients send Content-Length (it's a JSON-RPC
    POST with a known body), so path 1 covers ~all cases. Path 2 is a
    safety net.
    """

    def __init__(self, app, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:  # noqa: ANN001
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > self.max_bytes:
                    return JSONResponse(
                        _wrap_transport_error(
                            {
                                "status": "error",
                                "reason": "request_too_large",
                                "max_bytes": self.max_bytes,
                                "received_content_length": int(cl),
                            }
                        ),
                        status_code=413,
                    )
            except ValueError:
                # Malformed Content-Length — let the handler reject it.
                pass
        return await call_next(request)


_OAUTH_PATHS: frozenset[str] = frozenset(
    {
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/register",
        "/authorize",
        "/token",
    }
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP fixed-window rate limit. In-memory only — fine on a
    single-machine deployment. If we ever scale to >1 machine, swap to
    a Redis-backed limiter (or push the rate-limit decision to a CDN edge).

    Two separate buckets:
      - main bucket: 60 req/window for `/mcp` and any non-exempt path.
      - oauth bucket: 30 req/window for the OAuth ceremony endpoints
        (discovery + register + authorize + token). Claude.ai's connector
        hits this sequence 5-8x per setup; a separate looser bucket lets
        the ceremony complete without blowing through the main quota,
        while still catching abuse. Codex review caught the previous
        full exemption.

    Excludes `/health` (probes hit it every 30s) and `/` (banner is
    curl-friendly and should not count against a user's MCP quota).

    Real client IP is taken from `_TRUSTED_PROXY_HEADERS` — only headers
    the operator explicitly opted into are read. Hosted (Fly behind CF)
    sets `RBI_TRUSTED_PROXY_HEADERS=cf-connecting-ip,fly-client-ip`.
    Self-host installs leave it unset and use the peer IP. Without this
    gate, any client could send `X-Forwarded-For: 1.2.3.4` and reset
    the bucket per-request.
    """

    def __init__(
        self,
        app,  # noqa: ANN001
        *,
        limit: int = RATE_LIMIT_PER_WINDOW,
        window_s: float = RATE_LIMIT_WINDOW_SECONDS,
        oauth_limit: int = OAUTH_RATE_LIMIT_PER_WINDOW,
        oauth_window_s: float = OAUTH_RATE_LIMIT_WINDOW_SECONDS,
        excluded_paths: tuple[str, ...] = ("/health", "/"),
        trusted_proxy_headers: tuple[str, ...] | None = None,
        trusted_proxy_cidrs: tuple | None = None,
    ) -> None:
        super().__init__(app)
        self.limit = limit
        self.window_s = window_s
        self.oauth_limit = oauth_limit
        self.oauth_window_s = oauth_window_s
        self.excluded_paths = excluded_paths
        # When None, fall back to the module-level env-driven default.
        # Tests pass an explicit tuple to control behavior without touching
        # process env.
        self.trusted_proxy_headers = (
            trusted_proxy_headers if trusted_proxy_headers is not None else _TRUSTED_PROXY_HEADERS
        )
        self.trusted_proxy_cidrs = (
            trusted_proxy_cidrs if trusted_proxy_cidrs is not None else _TRUSTED_PROXY_CIDRS
        )
        # ip -> (window_start_ts, count). Periodically pruned in dispatch.
        self._buckets: dict[str, tuple[float, int]] = {}
        self._oauth_buckets: dict[str, tuple[float, int]] = {}
        self._last_prune = time.time()

    def _peer_in_trusted_cidr(self, peer: str) -> bool:
        """Is the peer IP one of the configured trusted-proxy ranges?

        Empty CIDR set (the default) returns False — meaning even if
        `_TRUSTED_PROXY_HEADERS` is set, we still won't honor those
        headers. Operators must opt in to BOTH the header list AND a
        CIDR allowlist to enable header-based client-IP extraction.
        """
        if not self.trusted_proxy_cidrs:
            return False
        import ipaddress

        try:
            ip_obj = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(ip_obj in net for net in self.trusted_proxy_cidrs)

    def _client_ip(self, request: Request) -> str:
        # Header trust is gated on:
        #   1. The operator opted in to a header list via env, AND
        #   2. Either the operator also opted in to a CIDR allowlist
        #      and the request peer is in it (strict mode), OR no CIDR
        #      allowlist is configured (back-compat mode — preserves
        #      existing deployments that set HEADERS but never set
        #      CIDRS). In back-compat mode we log a one-time warning
        #      at the call site to nudge operators toward strict mode.
        #
        # The default for both env vars is empty → fall through to peer
        # IP, which is the right answer for self-host installs running
        # on localhost or bare VMs without a reverse proxy.
        peer = request.client.host if request.client else "unknown"
        if not self.trusted_proxy_headers:
            return peer
        # Strict mode: CIDR configured → enforce peer-in-CIDR.
        if self.trusted_proxy_cidrs and not self._peer_in_trusted_cidr(peer):
            return peer
        # Back-compat mode: no CIDR configured, but operator set the
        # header list — read it but warn (rate-limited so we don't spam).
        if not self.trusted_proxy_cidrs and not getattr(
            self, "_proxy_cidr_warned", False
        ):
            logger.warning(
                "trusted_proxy.no_cidr_gate",
                hint=(
                    "RBI_TRUSTED_PROXY_HEADERS is set but RBI_TRUSTED_PROXY_CIDRS"
                    " is unset. Header values will be honored without a CIDR check,"
                    " which lets any client spoof the rate-limit bucket. Set"
                    " RBI_TRUSTED_PROXY_CIDRS to the proxy egress range to harden."
                ),
            )
            self._proxy_cidr_warned = True
        for header in self.trusted_proxy_headers:
            v = request.headers.get(header)
            if v:
                # XFF may be comma-separated; trusted upstream proxies
                # *append* to it, so the first hop is the real client.
                return v.split(",")[0].strip()
        return peer

    def _is_oauth_path(self, path: str) -> bool:
        # Claude.ai also probes /.well-known/oauth-protected-resource/mcp,
        # so prefix-match the well-known namespace.
        return path in _OAUTH_PATHS or path.startswith("/.well-known/")

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        path = request.url.path
        if path in self.excluded_paths:
            return await call_next(request)

        is_oauth = self._is_oauth_path(path)
        bucket = self._oauth_buckets if is_oauth else self._buckets
        limit = self.oauth_limit if is_oauth else self.limit
        window_s = self.oauth_window_s if is_oauth else self.window_s

        ip = self._client_ip(request)
        now = time.time()

        # Periodic prune (every ~main window) to bound memory under churn.
        if now - self._last_prune > self.window_s:
            cutoff = now - self.window_s
            self._buckets = {k: v for k, v in self._buckets.items() if v[0] > cutoff}
            cutoff_oauth = now - self.oauth_window_s
            self._oauth_buckets = {
                k: v for k, v in self._oauth_buckets.items() if v[0] > cutoff_oauth
            }
            self._last_prune = now

        window_start, count = bucket.get(ip, (now, 0))
        if now - window_start >= window_s:
            window_start = now
            count = 0
        count += 1
        bucket[ip] = (window_start, count)

        if count > limit:
            retry_after = max(1, int(window_s - (now - window_start)))
            logger.warning(
                "rate_limit.exceeded",
                ip=ip,
                count=count,
                limit=limit,
                window_s=window_s,
                bucket="oauth" if is_oauth else "main",
                path=path,
            )
            return JSONResponse(
                _wrap_transport_error(
                    {
                        "status": "error",
                        "reason": "rate_limit_exceeded",
                        "limit": limit,
                        "window_seconds": window_s,
                        "retry_after_seconds": retry_after,
                    }
                ),
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)


def build_asgi_app(*, stateless: bool = True, json_response: bool = False) -> Starlette:
    """Build the Starlette ASGI app that serves the MCP over streamable HTTP.

    `stateless=True` (default) means each MCP request is independent — no
    server-side session state. Simpler when running behind a load balancer
    or with rolling deploys; loses per-session features (subscriptions,
    resumable streams), none of which we use.

    Mounts:
        /mcp        — MCP streamable-HTTP endpoint
        /health     — readiness probe for any orchestrator
        /          — version + family-counts banner (curl-friendly)
    """
    server = _build_server()
    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,  # stateless mode doesn't need replay
        json_response=json_response,
        stateless=stateless,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            logger.info("http.lifespan.start", version=__version__)
            # Eager DB validation. /health does the same checks lazily on
            # first probe and returns 503 if the corpus is missing/empty,
            # but a misconfigured deploy that never gets a /health probe
            # could serve clean status pages while every /mcp call 500s.
            # Failing in lifespan startup makes the server crash visibly
            # so the orchestrator (Fly, k8s, systemd) reports the bad
            # deploy instead of running degraded. Best-effort: if the
            # check itself raises an unexpected error, log it but don't
            # block startup — /health remains the source of truth.
            _validate_db_at_startup()
            # Pre-warm the embedder so the first user request doesn't pay
            # the cold-start tax. With the bge-small model pre-cached in
            # HF_HOME (~/.cache/huggingface or whatever the operator points
            # the env var at), this is a disk read + load — ~3-8s, not the
            # 75-200s HF download we used to pay. Synchronous on the asyncio
            # main thread to dodge a NumPy 2.x circular-import race that hit
            # the previous to_thread version. Any orchestrator's readiness
            # grace_period (>=10s) covers the brief block.
            _prewarm_embedder_sync()
            yield
            # Flush queued PostHog events before shutdown so the last
            # batch on a deploy doesn't get dropped. No-op when telemetry
            # is disabled (the default for every self-host install).
            try:
                from . import telemetry

                telemetry.shutdown()
            except Exception:  # noqa: BLE001
                pass
            logger.info("http.lifespan.stop")

    async def handle_mcp(scope, receive, send) -> None:  # noqa: ANN001 — ASGI signature
        """Forward all /mcp requests to the streamable-HTTP session manager."""
        await session_manager.handle_request(scope, receive, send)

    async def health(_: Request) -> JSONResponse:
        """Readiness probe used by load balancers + orchestrators.

        A bare process-up check isn't enough: the most common deploy failure
        mode is the data volume not mounting (or mounting empty), in which
        case the process is happily alive but every real request hits an
        empty SQLite. With only a process check the deploy goes green; users
        only learn it's broken when the first /mcp call returns errors.

        Deep checks:
          1. SQLite opens at the configured path.
          2. The corpus has at least one indexed document (catches missing
             volume, missing daily refresh, etc.).
          3. sqlite-vec loaded (degraded but not failed if not — dense
             retrieval falls back to FTS5-only).

        Result is cached for 30s so probe cadence doesn't thrash the DB.
        Returns 503 on hard-failure (no DB / no documents) so the
        orchestrator removes the machine from the LB; returns 200 with
        `degraded: true` if only sqlite-vec is missing.
        """
        now = time.time()
        cached_data = _health_cache["data"]
        cached_ts = float(_health_cache["ts"])  # type: ignore[arg-type]
        if cached_data is not None and (now - cached_ts) < _HEALTH_TTL_SECONDS:
            status_code = int(_health_cache["status_code"])  # type: ignore[arg-type]
            return JSONResponse(cached_data, status_code=status_code)  # type: ignore[arg-type]

        from .db import _load_sqlite_vec, connect

        db_path = os.environ.get("RBI_SOURCE_DB", "./data/db.sqlite")
        payload: dict[str, object] = {"version": __version__}
        status_code = 200
        try:
            with connect(db_path) as conn:
                doc_count = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
                # _load_sqlite_vec is idempotent and best-effort. connect()
                # already attempted it; we re-check here so /health surfaces
                # whether the running process has dense retrieval available.
                vec_ok = _load_sqlite_vec(conn)
        except Exception as exc:  # noqa: BLE001
            logger.error("health.db_unavailable", error=str(exc), exc_type=type(exc).__name__)
            payload.update({"status": "unhealthy", "reason": "db_unavailable"})
            status_code = 503
        else:
            if doc_count == 0:
                # Volume mounted empty, or the daily refresh hasn't run
                # yet. Either way: we'd fail every real request — flip 503
                # so the orchestrator doesn't route to us.
                payload.update(
                    {
                        "status": "unhealthy",
                        "reason": "corpus_empty",
                        "documents": 0,
                    }
                )
                status_code = 503
            elif not vec_ok:
                # Dense retrieval unavailable but FTS5 sparse still works.
                # Degraded, not down — stay in the LB.
                payload.update(
                    {
                        "status": "ok",
                        "degraded": True,
                        "reason": "sqlite_vec_unavailable",
                        "documents": int(doc_count),
                    }
                )
            else:
                payload.update({"status": "ok", "documents": int(doc_count)})

        # Surface the query-embedding cache stats so external monitors
        # can verify cache effectiveness. Cheap to compute (just a tuple
        # read on functools.lru_cache); not gated on db health since the
        # cache is process-local.
        try:
            from .indexer.embed import embed_query_cache_info

            payload["query_cache"] = embed_query_cache_info()
        except Exception:  # noqa: BLE001
            # Don't let observability break the readiness probe.
            pass

        # Surface telemetry on/off so a quick curl of /health confirms
        # whether PostHog is wired up. Returns False whenever
        # POSTHOG_API_KEY is unset (the default for every install).
        try:
            from . import telemetry

            payload["telemetry"] = telemetry.is_enabled()
        except Exception:  # noqa: BLE001
            pass

        _health_cache["data"] = payload
        _health_cache["status_code"] = status_code
        _health_cache["ts"] = now
        return JSONResponse(payload, status_code=status_code)

    async def banner(request: Request) -> JSONResponse | HTMLResponse:
        """Dual-purpose landing.

        - Browsers (Accept includes text/html) get the static HTML homepage,
          loaded from src/rbi_source_mcp/static/index.html. The HTML page is
          self-contained (inline CSS, ~10 lines of inline JS for copy
          buttons) and ships in the wheel.
        - Everything else (curl with default Accept */*, MCP clients sending
          Accept: application/json) gets the corpus-stats JSON banner. Same
          contract as before — the JSON shape did not change.

        The JSON path is cached for 60s; the HTML path is cached in memory
        with mtime-based invalidation, so editing the file in dev refreshes
        without restart.
        """
        if _wants_html(request):
            html = _load_index_html()
            if html is not None:
                return HTMLResponse(
                    content=html,
                    headers={
                        "Cache-Control": "public, max-age=300",
                        "Content-Security-Policy": (
                            # Strict-ish CSP: inline styles + inline scripts
                            # are required for the single-file homepage; no
                            # external origins permitted.
                            "default-src 'none'; "
                            "style-src 'self' 'unsafe-inline'; "
                            "script-src 'self' 'unsafe-inline'; "
                            "img-src 'self' data:; "
                            "connect-src 'self'; "
                            "form-action 'self'; "
                            "base-uri 'self'; "
                            "frame-ancestors 'none'"
                        ),
                        "Referrer-Policy": "strict-origin-when-cross-origin",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            # Static file missing in this build — fall through to JSON path
            # rather than 500. Logged once for visibility.
            logger.warning("static.index_missing", path=str(_INDEX_PATH))

        now = time.time()
        cached = _banner_cache["data"]
        cached_ts = float(_banner_cache["ts"])  # type: ignore[arg-type]
        if cached is not None and (now - cached_ts) < _BANNER_TTL_SECONDS:
            return JSONResponse(cached)  # type: ignore[arg-type]

        from .db import connect

        db_path = os.environ.get("RBI_SOURCE_DB", "./data/db.sqlite")
        try:
            with connect(db_path) as conn:
                families = {
                    row["document_family"]: row["docs"]
                    for row in conn.execute(
                        "SELECT document_family, count(*) AS docs "
                        "FROM documents GROUP BY document_family "
                        "ORDER BY docs DESC"
                    )
                }
                total_docs = sum(families.values())
                total_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            # Log the real exception locally; return a generic message to the
            # public client (no internal paths or stack details leaked).
            logger.warning("banner.corpus_unavailable", error=str(exc), exc_type=type(exc).__name__)
            return JSONResponse(
                {
                    "name": "rbi-source-mcp",
                    "version": __version__,
                    "status": "corpus_unavailable",
                    "endpoints": {
                        "mcp": "/mcp",
                        "health": "/health",
                    },
                }
            )

        payload = {
            "name": "rbi-source-mcp",
            "version": __version__,
            "tagline": "RBI source-grounded retrieval, citation-first, retrieval-only.",
            "endpoints": {
                "mcp": "/mcp  (use this URL with your MCP client)",
                "health": "/health",
            },
            "corpus": {
                "total_documents": total_docs,
                "total_chunks": total_chunks,
                "by_family": families,
            },
            "client_install": {
                "claude_code": "claude mcp add rbi-source --transport http <THIS_URL>/mcp",
                "claude_ai": "Settings > Connectors > Add Integration > paste <THIS_URL>/mcp",
            },
            "disclaimer": (
                "Unofficial open-source tool. Not affiliated with the Reserve "
                "Bank of India. Returns source-grounded retrieval; does not "
                "issue legal opinions. Verify every citation with a qualified "
                "compliance reviewer before acting."
            ),
            "license": "MIT",
            "source": "https://github.com/harshilmathur/RBI-Source-MCP",
        }
        _banner_cache["data"] = payload
        _banner_cache["ts"] = now
        return JSONResponse(payload)

    async def static_asset(request: Request) -> Response:
        """Serve a whitelisted static file from src/rbi_source_mcp/static/.

        Looks the request path up in `_STATIC_ALLOWLIST` (path traversal is
        impossible because the filename is keyed off the URL, not constructed
        from it). Returns 404 for anything not in the list. Cached for 24h
        because favicons rarely change and browsers retry on 404.
        """
        entry = _STATIC_ALLOWLIST.get(request.url.path)
        if entry is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        filename, content_type = entry
        path = _STATIC_DIR / filename
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            logger.warning("static.asset_missing", path=str(path))
            return JSONResponse({"error": "not_found"}, status_code=404)
        return Response(
            content=data,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=86400, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    app = Starlette(
        debug=False,
        routes=[
            Route("/", banner),
            Route("/health", health),
            # Favicon set — declared in index.html via <link>. Each is a
            # whitelisted entry in _STATIC_ALLOWLIST.
            *[Route(p, static_asset, methods=["GET"]) for p in _STATIC_ALLOWLIST],
            # OAuth 2.1 ceremonial endpoints. Claude.ai's connector flow
            # walks RFC 8414 + RFC 9728 + RFC 7591 BEFORE attempting the
            # MCP handshake; if these 404, the connector says "Couldn't
            # reach the MCP server" even though /mcp/ itself works fine.
            # See src/rbi_source_mcp/oauth.py for the full rationale.
            Route(
                "/.well-known/oauth-protected-resource",
                _oauth.protected_resource_metadata,
                methods=["GET"],
            ),
            Route(
                "/.well-known/oauth-protected-resource/mcp",
                _oauth.protected_resource_metadata,
                methods=["GET"],
            ),
            Route(
                "/.well-known/oauth-authorization-server",
                _oauth.authorization_server_metadata,
                methods=["GET"],
            ),
            Route("/register", _oauth.register_client, methods=["POST"]),
            Route("/authorize", _oauth.authorize, methods=["GET"]),
            Route("/token", _oauth.token, methods=["POST"]),
            Mount("/mcp", app=handle_mcp),
        ],
        lifespan=lifespan,
    )

    # Claude.ai's connector POSTs to /mcp (no trailing slash) WITH the
    # bearer token AFTER the OAuth ceremony completes, but doesn't follow
    # Starlette's 307 redirect to /mcp/. Effect: the bearer never reaches
    # the MCP handler, Claude.ai retries the OAuth flow ~3-4x, then bails
    # with "Authorization with the MCP server failed."
    #
    # Just disabling redirect_slashes isn't enough — Starlette's Mount
    # strips its prefix, and a request to /mcp leaves scope.path="" which
    # the streamable-HTTP session manager 404s on. The fix is an
    # ASGI-level path rewrite: turn /mcp into /mcp/ in the scope BEFORE
    # routing sees it. No redirect, no 307, the bearer flows through.
    app.add_middleware(_NormalizeMcpPath)

    # Middleware order (Starlette runs these in REVERSE order of add_middleware
    # — last added = outermost = runs first). We want:
    #   1. Body-size cap   (outermost — reject huge bodies before any work)
    #   2. Rate limit      (next — drop floods cheaply)
    #   3. CORS            (innermost — sets headers on the actual response)
    # So we add them in opposite order:
    # CORS: wildcard origin is correct for an unauthenticated, read-only
    # public MCP. The MCP spec doesn't define a fixed origin allow-list
    # (Inspector runs on localhost; Cursor/Windsurf/Cline all run from
    # arbitrary file:// or app:// origins; future browser MCP hosts will
    # too). The wildcard is only safe because:
    #   - allow_credentials=False — no cookies, no Authorization is read
    #     against a session, so a victim's browser can't be tricked into
    #     leaking per-user state (there is none).
    #   - body-size cap + per-IP rate limit guard the operator's bill.
    # The CORS spec rejects `allow_origins=["*"]` combined with
    # `allow_credentials=True`, but pin it explicitly so a future edit
    # can't introduce the dangerous combination.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(MaxBodySizeMiddleware)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve RBI Source MCP over streamable HTTP.")
    parser.add_argument(
        "--host",
        default=os.environ.get("RBI_SOURCE_HOST", "127.0.0.1"),
        help="Bind host (default 127.0.0.1; use 0.0.0.0 for any cloud/reverse-proxy deployment).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", os.environ.get("RBI_SOURCE_PORT", "8080"))),
        help="Bind port (default 8080; honors $PORT — common convention on cloud platforms).",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Auto-reload on code change (dev only).",
    )
    args = parser.parse_args()

    print(
        f"\nrbi-source-mcp HTTP server v{__version__}\n"
        f"  binding:    http://{args.host}:{args.port}\n"
        f"  endpoints:  /  /health  /mcp\n"
        f"  corpus:     {os.environ.get('RBI_SOURCE_DB', './data/db.sqlite')}\n",
        file=sys.stderr,
    )

    uvicorn.run(
        "rbi_source_mcp.server_http:build_asgi_app" if args.reload else build_asgi_app(),
        factory=args.reload,
        host=args.host,
        port=args.port,
        log_level="info",
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
