"""MCP server for RBI Source MCP.

Exposes three tools at v0.1:

    rbi.check_current(url_or_ref)      -- the headline tool, fully implemented
    rbi.search(query, filters?)        -- stubbed (returns a "coming in v1.0" envelope)
    rbi.get_document(document_id, ...) -- stubbed (returns a "coming in v1.0" envelope)

Run locally:
    python -m rbi_source_mcp.server

The server reads the corpus DB from $RBI_SOURCE_DB (defaults to ./data/db.sqlite).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import __version__
from .check_current import check_current
from .db import connect, init_db

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"


def _resolve_db_path() -> Path:
    return Path(os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)).expanduser().resolve()


def _build_server() -> Server:
    """Build the MCP Server instance and register tool handlers."""
    server: Server = Server("rbi-source-mcp")

    @server.list_tools()  # type: ignore[no-untyped-call,misc]
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="rbi.check_current",
                description=(
                    "Given an RBI URL (Master Directions or withdrawn-circulars page), return whether "
                    "the document is current, withdrawn, or unknown — plus the replacement reference "
                    "when known. Two URL patterns supported at v0.1; other RBI URL types and textual "
                    "references return a structured 'unsupported_at_v0.1' response with a clear reason."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url_or_ref": {
                            "type": "string",
                            "description": "An RBI URL (preferred) or RBI reference number.",
                        }
                    },
                    "required": ["url_or_ref"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="rbi.search",
                description=(
                    "Hybrid search (FTS5 + sqlite-vec) over RBI Master Direction chunks. "
                    "Ships at v1.0 once the indexer + PDF extractor land. v0.1 returns a "
                    "structured 'coming_in_v1.0' response."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "filters": {
                            "type": "object",
                            "properties": {
                                "topic": {"type": "string"},
                                "regulated_entity": {"type": "string"},
                                "include_withdrawn": {"type": "boolean", "default": False},
                                "as_of_date": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="rbi.get_document",
                description=(
                    "Fetch a Master Direction's full text + version metadata. Ships at v1.0; "
                    "v0.1 returns a structured 'coming_in_v1.0' response."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "document_id": {
                            "type": "string",
                            "description": "Canonical document ID like 'rbi:master_direction:12550'.",
                        },
                        "as_of": {
                            "type": "string",
                            "description": "ISO 8601 date; resolves to the version current on that date.",
                        },
                    },
                    "required": ["document_id"],
                    "additionalProperties": False,
                },
            ),
        ]

    @server.call_tool()  # type: ignore[no-untyped-call,misc]
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        db_path = _resolve_db_path()
        # Ensure schema exists so the server can start before the first crawl.
        init_db(db_path)

        if name == "rbi.check_current":
            url_or_ref = (arguments or {}).get("url_or_ref", "")
            with connect(db_path) as conn:
                result = check_current(conn, url_or_ref)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == "rbi.search":
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "coming_in_v1.0",
                            "message": (
                                "Hybrid search ships at v1.0 once the PDF extractor and "
                                "sqlite-vec index land. v0.1 only exposes check_current."
                            ),
                            "tool": "rbi.search",
                        },
                        indent=2,
                    ),
                )
            ]

        if name == "rbi.get_document":
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "coming_in_v1.0",
                            "message": (
                                "Full-document fetch ships at v1.0. v0.1 only exposes check_current."
                            ),
                            "tool": "rbi.get_document",
                        },
                        indent=2,
                    ),
                )
            ]

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "status": "error",
                        "reason": "unknown_tool",
                        "tool": name,
                    },
                    indent=2,
                ),
            )
        ]

    return server


async def _run_stdio() -> None:
    """Run the server over stdio (for local Claude Desktop / Claude Code use)."""
    server = _build_server()
    logger.info("server.start", version=__version__, db=str(_resolve_db_path()))
    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)


def main() -> None:
    """Entry point for the `rbi-source-mcp` console script."""
    import asyncio

    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
