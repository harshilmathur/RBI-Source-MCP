"""Streamable-HTTP transport for the RBI Source MCP server.

The stdio server in `server.py` is what Claude Code / Claude Desktop spawn
locally. For HOSTED mode (Fly, etc.), we need to speak the streamable-HTTP
transport so any MCP client can connect via `<HOSTED_URL>/mcp` without
running a local subprocess.

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
import sys
import time
from collections.abc import AsyncIterator

import structlog
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import __version__
from .server import _build_server

logger = structlog.get_logger(__name__)

# Banner cache: stats only change on weekly refresh, so a 60-second TTL is
# fine. Without this, every GET / opens a fresh SQLite connection (with
# schema migration + extension load) — wasteful and trivially DOSable.
_BANNER_TTL_SECONDS = 60.0
_banner_cache: dict[str, object] = {"data": None, "ts": 0.0}

# /health cache: Fly's readiness probe hits this every few seconds. The deep
# health check opens a SQLite connection + verifies sqlite-vec loaded + counts
# documents — that's ~10ms but adds up at probe cadence and contends with
# real traffic. 30s cache is short enough that a real outage flips the probe
# within one Fly health-check window. Separate cache from the banner because
# the failure shapes differ (banner returns degraded JSON; health flips 503).
_HEALTH_TTL_SECONDS = 30.0
_health_cache: dict[str, object] = {"data": None, "status_code": 200, "ts": 0.0}


def build_asgi_app(*, stateless: bool = True, json_response: bool = False) -> Starlette:
    """Build the Starlette ASGI app that serves the MCP over streamable HTTP.

    `stateless=True` (default) means each MCP request is independent — no
    server-side session state. Simpler for hosted deployment behind a load
    balancer; loses per-session features (subscriptions, resumable streams),
    none of which we use.

    Mounts:
        /mcp        — MCP streamable-HTTP endpoint
        /health     — liveness probe (Fly readiness check)
        /          — version + family-counts banner (curl-friendly)
    """
    server = _build_server()
    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,           # stateless mode doesn't need replay
        json_response=json_response,
        stateless=stateless,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            logger.info("http.lifespan.start", version=__version__)
            yield
            logger.info("http.lifespan.stop")

    async def handle_mcp(scope, receive, send) -> None:  # noqa: ANN001 — ASGI signature
        """Forward all /mcp requests to the streamable-HTTP session manager."""
        await session_manager.handle_request(scope, receive, send)

    async def health(_: Request) -> JSONResponse:
        """Readiness probe used by Fly + load balancers.

        A bare process-up check isn't enough: the most common deploy failure
        mode is the Fly volume not mounting (or mounting empty), in which
        case the process is happily alive but every real request hits an
        empty SQLite. With only a process check the deploy goes green; users
        only learn it's broken when the first /mcp call returns errors.

        Deep checks:
          1. SQLite opens at the configured path.
          2. The corpus has at least one indexed document (catches missing
             volume, missing weekly refresh, etc.).
          3. sqlite-vec loaded (degraded but not failed if not — dense
             retrieval falls back to FTS5-only).

        Result is cached for 30s so probe cadence doesn't thrash the DB.
        Returns 503 on hard-failure (no DB / no documents) so Fly removes
        the machine from the LB; returns 200 with `degraded: true` if only
        sqlite-vec is missing.
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
                # Volume mounted empty, or weekly refresh hasn't run yet.
                # Either way: we'd fail every real request — flip 503 so Fly
                # doesn't route to us.
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

        _health_cache["data"] = payload
        _health_cache["status_code"] = status_code
        _health_cache["ts"] = now
        return JSONResponse(payload, status_code=status_code)

    async def banner(_: Request) -> JSONResponse:
        """Friendly landing for `curl <HOSTED_URL>/`. Surfaces corpus stats.

        Cached for 60s so /, the most curl-able endpoint, doesn't spawn a
        fresh SQLite connection + schema migration + sqlite-vec extension
        load on every hit.
        """
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
            "license": "Apache-2.0",
            "source": "https://github.com/harshilmathur/RBI-Source-MCP",
        }
        _banner_cache["data"] = payload
        _banner_cache["ts"] = now
        return JSONResponse(payload)

    app = Starlette(
        debug=False,
        routes=[
            Route("/", banner),
            Route("/health", health),
            Mount("/mcp", app=handle_mcp),
        ],
        lifespan=lifespan,
    )

    # Permissive CORS for the public MCP endpoint. Required for browser-based
    # MCP clients (e.g., Claude.ai connecting from a different origin).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve RBI Source MCP over streamable HTTP."
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("RBI_SOURCE_HOST", "127.0.0.1"),
        help="Bind host (default 127.0.0.1; use 0.0.0.0 for hosted/Fly).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", os.environ.get("RBI_SOURCE_PORT", "8080"))),
        help="Bind port (default 8080; honors $PORT for Fly).",
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
