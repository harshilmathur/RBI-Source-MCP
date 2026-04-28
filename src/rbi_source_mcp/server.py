"""MCP server for RBI Source MCP.

Exposes four tools at v0.1.5:

    rbi.check_compliance(text, topic_hint?) -- HEADLINE: paste a clause/PRD/policy,
                                               get back ranked relevant RBI provisions
                                               with citations. Retrieval-only.
    rbi.search(query, filters?)             -- direct keyword/topic retrieval over MD chunks
    rbi.get_document(document_id, ...)      -- fetch a Master Direction's metadata + ToC
    rbi.check_current(url_or_ref)           -- safety/utility: is this URL withdrawn?

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
from .disclaimer import DISCLAIMER, LLM_INSTRUCTION
from .mcp.check_compliance import check_compliance
from .mcp.get_document import get_document
from .mcp.search import search

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"


def _resolve_db_path() -> Path:
    return Path(os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)).expanduser().resolve()


def _wrap_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Inject the disclaimer + LLM instruction into every tool response.

    Order matters: the disclaimer fields are placed at the TOP of the JSON
    object (before result fields) so an LLM streaming the response or
    truncating tail content still sees them. Also, by appearing first, they
    survive partial-response summarization more reliably than a footer would.
    """
    return {
        "_disclaimer": DISCLAIMER,
        "_llm_instruction": LLM_INSTRUCTION,
        **payload,
    }


def _build_server() -> Server:
    """Build the MCP Server instance and register tool handlers."""
    server: Server = Server("rbi-source-mcp")

    @server.list_tools()  # type: ignore[no-untyped-call,misc]
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="rbi.check_compliance",
                description=(
                    "HEADLINE. Paste free text (a clause, PRD section, draft policy paragraph, "
                    "code comment) and get back ranked relevant Master Direction provisions with "
                    "citations — official URL, paragraph anchor, RBI reference, last-updated date, "
                    "current/withdrawn status. RETRIEVAL-ONLY by design: this tool does NOT issue "
                    "compliance verdicts. The LLM consuming the tool should synthesize a verdict "
                    "from the cited provisions and clearly mark it as 'not legal advice'. "
                    "Always include source URLs and paragraph anchors when summarizing for the user. "
                    "REQUIRED: every response includes a `_disclaimer` field; you MUST surface that "
                    "disclaimer (verbatim or paraphrased preserving all four points) when presenting "
                    "results to the user. Do not omit it."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": (
                                "The free-text clause/section to check. Typically <2000 chars; "
                                "split larger documents into sections and call once per section."
                            ),
                        },
                        "topic_hint": {
                            "type": "string",
                            "description": (
                                "Optional topic to bias retrieval to one MD. Known values: "
                                "'payment_aggregator' / 'pa' / 'pa_pg' (PA MD), "
                                "'kyc_bank' (Commercial Banks KYC), 'kyc_nbfc' (NBFC KYC), "
                                "'kyc' (spans both KYC MDs), "
                                "'ppi' / 'prepaid' / 'wallet' (Prepaid Instruments MD), "
                                "'cards' / 'credit_card' / 'debit_card' / 'tokenisation' (Commercial Banks Cards MD), "
                                "'e_mandate' / 'recurring' (E-mandate Framework), "
                                "'digital_payment_security' / 'afa' (Digital Payment Security Controls). "
                                "Unknown values are ignored (search spans full corpus)."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max provisions to return (default 5).",
                            "default": 5,
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="rbi.search",
                description=(
                    "Direct keyword/topic search over Master Direction chunks. Use this when "
                    "the user has a clean query like 'what are the net-worth requirements for PAs' "
                    "rather than a clause to compliance-check. Returns ranked chunks with citations. "
                    "v0.5 uses hybrid retrieval (FTS5 + sqlite-vec dense, RRF fused). "
                    "REQUIRED: every response includes a `_disclaimer` field; you MUST surface it "
                    "when presenting results to the user."
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
                        "limit": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="rbi.get_document",
                description=(
                    "Fetch a Master Direction's metadata + table of contents. Pass include_text=true "
                    "to also get the full assembled body text. Useful when check_compliance or search "
                    "surfaces a chunk and the LLM wants more context around it. "
                    "REQUIRED: every response includes a `_disclaimer` field; you MUST surface it "
                    "when presenting results to the user."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "document_id": {
                            "type": "string",
                            "description": "Canonical document ID like 'rbi:master_direction:12896'.",
                        },
                        "include_text": {
                            "type": "boolean",
                            "description": "If true, include the full assembled body text. Default false.",
                            "default": False,
                        },
                        "as_of": {
                            "type": "string",
                            "description": (
                                "ISO 8601 date. Reserved for v1.1; ignored at v0.1.5 (always returns "
                                "current version)."
                            ),
                        },
                    },
                    "required": ["document_id"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="rbi.check_current",
                description=(
                    "Safety/utility tool: paste an RBI URL (Master Direction or notification page) "
                    "and learn whether it's current, withdrawn, or out-of-corpus. Useful for "
                    "verifying a citation the user already has. Two URL patterns supported at v0.1; "
                    "other inputs return a structured 'unsupported_at_v0.1' response. "
                    "REQUIRED: every response includes a `_disclaimer` field; you MUST surface it "
                    "when presenting results to the user."
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
        ]

    @server.call_tool()  # type: ignore[no-untyped-call,misc]
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        db_path = _resolve_db_path()
        # Ensure schema exists so the server can start before the first crawl.
        init_db(db_path)
        args = arguments or {}

        if name == "rbi.check_compliance":
            with connect(db_path) as conn:
                result = check_compliance(
                    conn,
                    args.get("text", ""),
                    topic_hint=args.get("topic_hint"),
                    limit=int(args.get("limit", 5)),
                )
            return [TextContent(type="text", text=json.dumps(_wrap_response(result), indent=2))]

        if name == "rbi.search":
            with connect(db_path) as conn:
                result = search(
                    conn,
                    args.get("query", ""),
                    filters=args.get("filters") or {},
                    limit=int(args.get("limit", 5)),
                )
            return [TextContent(type="text", text=json.dumps(_wrap_response(result), indent=2))]

        if name == "rbi.get_document":
            with connect(db_path) as conn:
                result = get_document(
                    conn,
                    args.get("document_id", ""),
                    include_text=bool(args.get("include_text", False)),
                    as_of=args.get("as_of"),
                )
            return [TextContent(type="text", text=json.dumps(_wrap_response(result), indent=2))]

        if name == "rbi.check_current":
            with connect(db_path) as conn:
                result = check_current(conn, args.get("url_or_ref", ""))
            return [TextContent(type="text", text=json.dumps(_wrap_response(result), indent=2))]

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    _wrap_response(
                        {
                            "status": "error",
                            "reason": "unknown_tool",
                            "tool": name,
                        }
                    ),
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
