"""MCP server for RBI Source MCP.

Exposes four tools at v0.1.5:

    rbi_check_compliance(text, topic_hint?) -- HEADLINE: paste a clause/PRD/policy,
                                               get back ranked relevant RBI provisions
                                               with citations. Retrieval-only.
    rbi_search(query, filters?)             -- direct keyword/topic retrieval over MD chunks
    rbi_get_document(document_id, ...)      -- fetch a Master Direction's metadata + ToC
    rbi_check_current(url_or_ref)           -- safety/utility: is this URL withdrawn?

Run locally:
    python -m rbi_source_mcp.server

The server reads the corpus DB from $RBI_SOURCE_DB (defaults to ./data/db.sqlite).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import __version__, telemetry
from .check_current import check_current
from .db import connect, init_db
from .disclaimer import DISCLAIMER, LLM_INSTRUCTION
from .mcp.check_compliance import check_compliance
from .mcp.get_document import get_document
from .mcp.search import search

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"

# Hard cap on user-supplied free-text fields (`text` for check_compliance,
# `query` for search, etc.). The MCP server runs single-process; a
# multi-megabyte payload spent inside the embedder or FTS5 tokenizer would
# block the asyncio event loop and starve other clients. 32 KB is plenty
# for a clause-level compliance check (the README explicitly recommends
# splitting larger documents into sections, typically <2000 chars). Inputs
# longer than this are rejected with a structured error envelope rather
# than truncated, so the LLM caller learns it sent too much.
MAX_INPUT_LEN = 32_000


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


def _input_too_large_response(tool_name: str, field: str, length: int) -> TextContent:
    """Build a wrapped error envelope for an oversized free-text input.

    Symmetric with `_error_response`: disclaimer fields at the top, structured
    error body. The LLM caller learns which field was too long and what the
    cap is, so it can split the input and retry.
    """
    logger.warning(
        "tool.dispatch.input_too_large",
        tool=tool_name,
        field=field,
        length=length,
        cap=MAX_INPUT_LEN,
    )
    payload = _wrap_response(
        {
            "status": "error",
            "reason": "input_too_large",
            "tool": tool_name,
            "field": field,
            "length": length,
            "max_length": MAX_INPUT_LEN,
            "message": (
                f"Input field '{field}' is {length} characters; the limit is "
                f"{MAX_INPUT_LEN}. Split the input into smaller sections "
                "(typically <2000 chars per section is recommended) and call "
                "the tool once per section."
            ),
        }
    )
    return TextContent(type="text", text=json.dumps(payload, indent=2))


def _error_response(tool_name: str, reason: str, exc: BaseException) -> TextContent:
    """Build a wrapped error envelope for a tool dispatch that raised.

    The disclaimer fields STILL appear at the top — the legal posture must
    be preserved on the error path too. We log the real exception locally
    via structlog, but the public response only carries the exception class
    name (no traceback, no internal paths) to avoid leaking internals.
    """
    logger.error(
        "tool.dispatch.error",
        tool=tool_name,
        reason=reason,
        error=str(exc),
        exc_type=type(exc).__name__,
    )
    payload = _wrap_response(
        {
            "status": "error",
            "reason": reason,
            "tool": tool_name,
            "error_class": type(exc).__name__,
            "message": (
                "An internal error occurred while serving this tool. The error "
                "has been logged. Re-try the call; if the failure persists, "
                "open an issue at https://github.com/harshilmathur/RBI-Source-MCP."
            ),
        }
    )
    return TextContent(type="text", text=json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Sync thread-target helpers for asyncio.to_thread.
#
# Each helper opens its own SQLite connection (sqlite3 connections are NOT
# safe to share across threads with check_same_thread=True, which is the
# default) and runs the synchronous tool implementation. The async dispatch
# in `call_tool` schedules these via `asyncio.to_thread`, so the event loop
# stays responsive while sentence-transformers' `model.encode()` runs.
# ---------------------------------------------------------------------------


def _run_check_compliance(
    db_path: Path, text: str, topic_hint: str | None, limit: int
) -> dict[str, Any]:
    with connect(db_path) as conn:
        return check_compliance(conn, text, topic_hint=topic_hint, limit=limit)


def _run_search(db_path: Path, query: str, filters: dict[str, Any], limit: int) -> dict[str, Any]:
    with connect(db_path) as conn:
        return search(conn, query, filters=filters, limit=limit)


def _run_get_document(
    db_path: Path, document_id: str, include_text: bool, as_of: str | None
) -> dict[str, Any]:
    with connect(db_path) as conn:
        return get_document(conn, document_id, include_text=include_text, as_of=as_of)


def _run_check_current(db_path: Path, url_or_ref: str) -> dict[str, Any]:
    with connect(db_path) as conn:
        return check_current(conn, url_or_ref)


def _build_server() -> Server:
    """Build the MCP Server instance and register tool handlers."""
    server: Server = Server("rbi-source-mcp")

    @server.list_tools()  # type: ignore[no-untyped-call,misc]
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="rbi_check_compliance",
                description=(
                    "HEADLINE. Paste free text (a clause, PRD section, draft policy paragraph, "
                    "code comment) and get back ranked relevant Master Direction provisions with "
                    "citations — official URL, paragraph anchor, RBI reference, last-updated date, "
                    "current/withdrawn status. RETRIEVAL-ONLY by design: this tool does NOT issue "
                    "compliance verdicts. The LLM consuming the tool should synthesize a verdict "
                    "from the cited provisions and clearly mark it as 'not legal advice'. "
                    "Always include source URLs and paragraph anchors when summarizing for the user. "
                    "REQUIRED: every response includes a `_disclaimer` field; you MUST surface that "
                    "disclaimer (verbatim or paraphrased preserving all five points) when presenting "
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
                name="rbi_search",
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
                name="rbi_get_document",
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
                name="rbi_check_current",
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
        # Each branch is wrapped in try/except so unexpected exceptions
        # (sqlite OperationalError, embedder OOM, model load failure, etc.)
        # are returned as a structured `_wrap_response`-wrapped error rather
        # than escaping to the MCP SDK which returns its OWN error envelope
        # without our disclaimer fields. The legal-posture contract is that
        # EVERY tool response carries `_disclaimer` + `_llm_instruction`,
        # including failures. Don't let an exception break that.
        #
        # Telemetry: a single `mcp_tool_called` event fires from the finally
        # block at the bottom of this function. Status + extra_props are
        # mutated by each branch before returning. When POSTHOG_API_KEY is
        # unset (every self-host install), capture_tool_call is a no-op.
        # We deliberately capture only shape-level metadata (limit, has_filters,
        # length buckets, topic_hint enum value) — never the query text,
        # clause text, document ids, or response bodies.
        t0 = time.perf_counter()
        status = "ok"
        extra_props: dict[str, Any] = {}
        try:
            db_path = _resolve_db_path()
            try:
                init_db(db_path)
            except Exception as exc:  # noqa: BLE001
                status = "db_unavailable"
                extra_props["error_type"] = type(exc).__name__
                return [_error_response(name, "db_unavailable", exc)]
            args = arguments or {}

            # Each tool branch wraps its synchronous DB + embedder work in
            # asyncio.to_thread so the event loop is NOT blocked while
            # sentence-transformers runs model.encode() (CPU-bound, ~50-200ms
            # per call on shared-cpu-1x). Without this wrap, a single client
            # at modest concurrency could brown out the entire server. See
            # /cso security review finding #1 (2026-04-29).

            if name == "rbi_check_compliance":
                text = args.get("text", "") or ""
                topic_hint = args.get("topic_hint")
                limit = int(args.get("limit", 5))
                extra_props["text_length_bucket"] = telemetry.text_length_bucket(len(text))
                extra_props["limit"] = limit
                if topic_hint:
                    # topic_hint is a closed enum (kyc/uli/digital_lending/...) —
                    # safe to send. Cardinality stays bounded.
                    extra_props["topic_hint"] = str(topic_hint)
                if len(text) > MAX_INPUT_LEN:
                    status = "input_too_large"
                    return [_input_too_large_response(name, "text", len(text))]
                try:
                    result = await asyncio.to_thread(
                        _run_check_compliance,
                        db_path,
                        text,
                        topic_hint,
                        limit,
                    )
                except Exception as exc:  # noqa: BLE001
                    status = "error"
                    extra_props["error_type"] = type(exc).__name__
                    return [_error_response(name, "tool_failed", exc)]
                return [TextContent(type="text", text=json.dumps(_wrap_response(result), indent=2))]

            if name == "rbi_search":
                query = args.get("query", "") or ""
                filters = args.get("filters") or {}
                limit = int(args.get("limit", 5))
                extra_props["query_length_bucket"] = telemetry.query_length_bucket(len(query))
                extra_props["has_filters"] = bool(filters)
                extra_props["limit"] = limit
                if len(query) > MAX_INPUT_LEN:
                    status = "input_too_large"
                    return [_input_too_large_response(name, "query", len(query))]
                try:
                    result = await asyncio.to_thread(
                        _run_search,
                        db_path,
                        query,
                        filters,
                        limit,
                    )
                except Exception as exc:  # noqa: BLE001
                    status = "error"
                    extra_props["error_type"] = type(exc).__name__
                    return [_error_response(name, "tool_failed", exc)]
                return [TextContent(type="text", text=json.dumps(_wrap_response(result), indent=2))]

            if name == "rbi_get_document":
                include_text = bool(args.get("include_text", False))
                extra_props["include_text"] = include_text
                extra_props["has_as_of"] = bool(args.get("as_of"))
                try:
                    result = await asyncio.to_thread(
                        _run_get_document,
                        db_path,
                        args.get("document_id", ""),
                        include_text,
                        args.get("as_of"),
                    )
                except Exception as exc:  # noqa: BLE001
                    status = "error"
                    extra_props["error_type"] = type(exc).__name__
                    return [_error_response(name, "tool_failed", exc)]
                return [TextContent(type="text", text=json.dumps(_wrap_response(result), indent=2))]

            if name == "rbi_check_current":
                # Deliberately no extra_props — url_or_ref can carry user-leakable
                # context (e.g., a private ref id). Just count the call.
                try:
                    result = await asyncio.to_thread(
                        _run_check_current,
                        db_path,
                        args.get("url_or_ref", ""),
                    )
                except Exception as exc:  # noqa: BLE001
                    status = "error"
                    extra_props["error_type"] = type(exc).__name__
                    return [_error_response(name, "tool_failed", exc)]
                return [TextContent(type="text", text=json.dumps(_wrap_response(result), indent=2))]

            status = "unknown_tool"
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
        finally:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            telemetry.capture_tool_call(
                tool_name=name,
                status=status,
                latency_ms=latency_ms,
                properties=extra_props,
            )

    return server


def _configure_stdio_logging() -> None:
    """Force ALL log output to stderr.

    MCP stdio transport reserves stdout for line-delimited JSON-RPC. Any
    stray non-JSON byte on stdout corrupts the protocol — the client
    raises `Unexpected non-whitespace character after JSON at position N`
    and the server appears broken to the user.

    structlog's default `PrintLoggerFactory()` writes to sys.stdout. We
    rebind it to sys.stderr here, plus do the same for the stdlib logging
    root (in case the MCP SDK or any transitive dep uses logging.warning
    or similar). HuggingFace / sentence-transformers / transformers all
    print warnings via the warnings module which already goes to stderr,
    so those are fine.

    Bug fix in v0.8.2 — caught when a real user wired this into Claude
    Desktop locally. Reproduces with: `rbi-source-mcp` over stdio +
    any tool call that touches the logger (every successful call does).
    """
    import logging

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )
    # stdlib logging — anyone using `logging.getLogger(...)` instead of
    # structlog. Default StreamHandler writes to stderr already, but be
    # explicit so an upstream dep can't surprise us.
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    # Replace any pre-existing handlers (e.g., from `logging.basicConfig` in
    # a transitive import) so nothing slips onto stdout.
    root.handlers = [handler]
    root.setLevel(logging.WARNING)


async def _run_stdio() -> None:
    """Run the server over stdio (for local Claude Desktop / Claude Code use).

    Wrapped in try/finally so queued PostHog events get flushed even on
    SIGTERM / Ctrl-C / Claude Desktop quit. Without the finally clause,
    the daemon flush thread can be killed mid-batch and the last events
    drop. Codex review caught this — HTTP path already calls
    `telemetry.shutdown()` from its lifespan teardown; stdio was the
    asymmetric gap. No-op for self-host installs (POSTHOG_API_KEY unset).
    """
    _configure_stdio_logging()
    server = _build_server()
    logger.info("server.start", version=__version__, db=str(_resolve_db_path()))
    try:
        async with stdio_server() as (read_stream, write_stream):
            init_options = server.create_initialization_options()
            await server.run(read_stream, write_stream, init_options)
    finally:
        try:
            telemetry.shutdown()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    """Entry point for the `rbi-source-mcp` console script."""
    import asyncio

    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
