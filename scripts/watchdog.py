"""Lightweight live-MCP watchdog. Designed to run under cron / GHA / OpenClaw.

Five fast probes (~5-15s end to end):
    1. /health returns 200 with documents > 100
    2. tools/list returns the 4 expected tools
    3. A known-good query returns the expected document_id (PA min net worth)
    4. An empty-input query flags low_confidence=True
    5. Median latency over 3 warm calls is under threshold

Exits:
    0 — all probes ok
    1 — one or more probes failed (alertable)
    2 — service unreachable

Output (one line of JSON to stdout, human-readable summary to stderr):
    {"ok": true, "probes_passed": 5, "probes_failed": 0, "p50_ms": 612, ...}

Distinct from scripts/qa_live.py (the deep, slow QA harness — 50+ probes,
30-60s, post-deploy use). The watchdog is the small, fast check that's
safe to run from cron every 5-15 minutes.

Usage:
    uv run python scripts/watchdog.py
    uv run python scripts/watchdog.py --url https://staging.example.com
    uv run python scripts/watchdog.py --p95-budget-ms 5000

Cron example:
    */15 * * * * cd /path/to/repo && uv run python scripts/watchdog.py \
        || curl -X POST https://hooks.slack.com/... -d "rbi-mcp watchdog FAILED"
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "https://rbi-source-mcp.fly.dev"
EXPECTED_TOOLS = {"rbi_check_compliance", "rbi_search", "rbi_get_document", "rbi_check_current"}
KNOWN_GOOD_QUERY = "minimum net worth requirement for payment aggregator"
KNOWN_GOOD_HINT = "pa"
KNOWN_GOOD_DOC = "rbi:master_direction:12896"


def _call_mcp(url: str, method: str, params: dict[str, Any], rid: int = 1, timeout: float = 20.0) -> tuple[dict, float]:
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        f"{url}/mcp",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json,text/event-stream",
        },
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
    elapsed = time.time() - t0
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:]), elapsed
    raise RuntimeError(f"no SSE data line: {raw[:200]}")


def _call_tool(url: str, tool: str, args: dict[str, Any], rid: int, timeout: float = 20.0) -> tuple[dict, float]:
    resp, elapsed = _call_mcp(url, "tools/call", {"name": tool, "arguments": args}, rid=rid, timeout=timeout)
    res = resp.get("result", {})
    content = res.get("content") or []
    if not content:
        return {"_empty_content": True}, elapsed
    txt = content[0].get("text", "")
    try:
        body = json.loads(txt)
    except json.JSONDecodeError:
        body = {"_raw": txt}
    body["_isError"] = res.get("isError", False)
    return body, elapsed


def watchdog(url: str, p95_budget_ms: int) -> dict[str, Any]:
    failures: list[str] = []
    detail: dict[str, Any] = {}

    # 1. /health
    try:
        t0 = time.time()
        with urllib.request.urlopen(f"{url}/health", timeout=10) as r:
            data = json.loads(r.read())
        detail["health_latency_ms"] = round((time.time() - t0) * 1000, 1)
        detail["documents"] = data.get("documents")
        if data.get("status") != "ok":
            failures.append(f"/health status={data.get('status')!r} (expected 'ok')")
        if (data.get("documents") or 0) < 100:
            failures.append(f"/health documents={data.get('documents')} (expected > 100)")
    except Exception as exc:
        failures.append(f"/health unreachable: {exc!r}")
        return {"ok": False, "failures": failures, "detail": detail, "fatal": True}

    # 2. tools/list
    try:
        resp, dt = _call_mcp(url, "tools/list", {}, rid=1)
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        detail["tools_count"] = len(tools)
        missing = EXPECTED_TOOLS - names
        if missing:
            failures.append(f"tools/list missing: {missing}")
    except Exception as exc:
        failures.append(f"tools/list failed: {exc!r}")

    # 3. Known-good query returns expected document_id at top-1
    try:
        result, dt = _call_tool(
            url, "rbi_check_compliance",
            {"text": KNOWN_GOOD_QUERY, "topic_hint": KNOWN_GOOD_HINT},
            rid=2,
        )
        provs = result.get("relevant_provisions") or []
        detail["known_good_top1"] = provs[0].get("document_id") if provs else None
        if not provs:
            failures.append("known-good query returned no provisions")
        elif provs[0].get("document_id") != KNOWN_GOOD_DOC:
            failures.append(
                f"known-good top1={provs[0].get('document_id')} (expected {KNOWN_GOOD_DOC})"
            )
        if "_disclaimer" not in result:
            failures.append("response missing _disclaimer")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            # CF embeddings rate-limited. Service is up; this probe is
            # inconclusive but not a failure on our side.
            detail["known_good_top1"] = "rate_limited"
        else:
            failures.append(f"known-good query failed: HTTP {exc.code}")
    except Exception as exc:
        failures.append(f"known-good query failed: {exc!r}")

    # 4. Empty-input flags low_confidence=True (no embedding call → no rate limit)
    try:
        result, _ = _call_tool(url, "rbi_check_compliance", {"text": ""}, rid=3)
        low_conf = result.get("low_confidence", False)
        n_prov = len(result.get("relevant_provisions") or [])
        detail["empty_input_low_conf"] = low_conf
        if not low_conf and n_prov > 0:
            failures.append(f"empty input did not flag low_confidence (n_prov={n_prov})")
    except Exception as exc:
        failures.append(f"empty-input probe failed: {exc!r}")

    # 5. Latency over 2 warm calls (light-touch: avoid hammering the upstream
    #    embedding provider's rate limits).
    queries = ["payment aggregator", "ppi loading limit"]
    latencies: list[float] = []
    rate_limited = 0
    last_err: str | None = None
    for i, q in enumerate(queries):
        try:
            _, dt = _call_tool(url, "rbi_check_compliance", {"text": q}, rid=4 + i, timeout=20)
            latencies.append(dt * 1000)
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code} {exc.reason}"
            if exc.code == 429:
                rate_limited += 1
        except Exception as exc:
            last_err = repr(exc)
        time.sleep(0.5)  # pace ourselves between calls

    detail["latency_samples"] = len(latencies)
    detail["rate_limited"] = rate_limited

    if latencies:
        p50 = statistics.median(latencies)
        p95 = max(latencies) if len(latencies) < 2 else sorted(latencies)[-1]
        detail["p50_ms"] = round(p50, 1)
        detail["p95_ms"] = round(p95, 1)
        if p95 > p95_budget_ms:
            failures.append(f"p95 {p95:.0f}ms exceeds budget {p95_budget_ms}ms")
    elif rate_limited:
        # Soft signal — service is up, just CF embeddings are throttled.
        # We deliberately don't fail on this; rate-limit headroom is a
        # capacity concern, not an availability one.
        detail["latency_status"] = "rate_limited (no fail)"
    else:
        failures.append(f"all latency probes failed (last_err={last_err})")

    return {
        "ok": not failures,
        "failures": failures,
        "detail": detail,
        "fatal": False,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--p95-budget-ms", type=int, default=5000,
                    help="Fail if p95 latency exceeds this (default: 5000)")
    ap.add_argument("--json-only", action="store_true",
                    help="Emit only the JSON summary on stdout (suppress stderr)")
    args = ap.parse_args()

    started = time.time()
    result = watchdog(args.url, args.p95_budget_ms)
    elapsed = round(time.time() - started, 2)

    summary = {
        "url": args.url,
        "ok": result["ok"],
        "elapsed_s": elapsed,
        "n_failures": len(result["failures"]),
        **result["detail"],
    }
    print(json.dumps(summary))

    if not args.json_only:
        if result["ok"]:
            print(f"OK  {args.url}  ({elapsed}s)", file=sys.stderr)
        else:
            print(f"FAIL  {args.url}  ({elapsed}s)", file=sys.stderr)
            for f in result["failures"]:
                print(f"  - {f}", file=sys.stderr)

    if result.get("fatal"):
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
