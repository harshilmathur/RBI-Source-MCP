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
        """Liveness probe used by Fly + load balancers."""
        return JSONResponse({"status": "ok", "version": __version__})

    async def banner(_: Request) -> JSONResponse:
        """Friendly landing for `curl <HOSTED_URL>/`. Surfaces corpus stats."""
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
            return JSONResponse(
                {
                    "name": "rbi-source-mcp",
                    "version": __version__,
                    "status": "corpus_unavailable",
                    "error": str(exc),
                    "endpoints": {
                        "mcp": "/mcp",
                        "health": "/health",
                    },
                }
            )

        return JSONResponse(
            {
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
        )

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
