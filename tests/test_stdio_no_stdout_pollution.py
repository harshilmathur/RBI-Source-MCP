"""Regression test: stdio MCP server emits ONLY valid JSON-RPC on stdout.

v0.8.2 bug: structlog's default `PrintLoggerFactory()` writes to
`sys.stdout`. The MCP stdio transport reserves stdout for line-delimited
JSON-RPC, so any log line corrupts the protocol. Claude Desktop reports:

    Unexpected non-whitespace character after JSON at position N

Fix: `_configure_stdio_logging()` rebinds structlog + stdlib logging to
`sys.stderr` before `server.run()` starts. This test confirms the fix
and catches regressions if a transitive dep ever starts logging to
stdout again.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _send_jsonrpc_and_capture(messages: list[dict], wait_seconds: float = 2.0) -> tuple[list[str], str]:
    """Spawn the stdio server, send each message, return (stdout_lines, stderr).

    Uses `python -m rbi_source_mcp.server` so the test works in any
    environment where the package is importable, without requiring the
    `rbi-source-mcp` console script to be on PATH.
    """
    p = subprocess.Popen(
        [sys.executable, "-m", "rbi_source_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(REPO_ROOT),
    )
    try:
        for msg in messages:
            line = json.dumps(msg) + "\n"
            p.stdin.write(line)
            p.stdin.flush()
        time.sleep(wait_seconds)
        p.terminate()
        out, err = p.communicate(timeout=10)
    except Exception:
        p.kill()
        raise
    return [line for line in out.splitlines() if line.strip()], err


def test_stdio_initialize_emits_only_json_on_stdout() -> None:
    """Single initialize round-trip: stdout = exactly one JSON-RPC line."""
    out, err = _send_jsonrpc_and_capture(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        ]
    )
    assert len(out) >= 1, f"expected at least one stdout line, got {out}; stderr={err[:300]!r}"
    for i, line in enumerate(out, 1):
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(f"stdout line {i} is not valid JSON: {exc}\n  raw: {line[:200]!r}\n  stderr: {err[:300]!r}")


def test_stdio_initialize_plus_tools_list_emits_only_json_on_stdout() -> None:
    """Multi-message: initialize + initialized notification + tools/list.

    This was the original repro path — `tools/list` triggers more code
    paths that historically wrote log lines to stdout.
    """
    out, err = _send_jsonrpc_and_capture(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ],
        wait_seconds=3.0,
    )
    assert len(out) >= 2, f"expected initialize + tools/list responses, got {len(out)}; stderr={err[:300]!r}"
    for i, line in enumerate(out, 1):
        try:
            d = json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(f"stdout line {i} is not valid JSON: {exc}\n  raw: {line[:200]!r}")
        assert d.get("jsonrpc") == "2.0", f"line {i} missing jsonrpc=2.0 field: {d}"


def test_stdio_logging_routed_to_stderr() -> None:
    """The startup `server.start` log line must appear on STDERR, not stdout."""
    out, err = _send_jsonrpc_and_capture(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        ]
    )
    # The structlog "server.start" event should land somewhere visible — stderr.
    assert "server.start" in err, (
        f"expected 'server.start' on stderr (logging is configured to write there). "
        f"stderr was: {err[:500]!r}"
    )
    # And it must NOT have leaked onto stdout.
    for line in out:
        assert "server.start" not in line, (
            f"log line leaked onto stdout: {line!r} — this corrupts the MCP stdio protocol"
        )
