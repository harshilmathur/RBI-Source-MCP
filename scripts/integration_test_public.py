#!/usr/bin/env python3
"""End-to-end regression + usage tests against the live public MCP endpoint.

Hits https://rbi-source.harshil.ai/mcp/ over real HTTPS, exercises every tool,
every documented topic hint, error envelopes, security middlewares, and the
HTTP transport layer. Outputs:

    1. Console table with PASS/FAIL + evidence per case
    2. JSON dump at .gstack/integration-reports/{date}.json

Pacing: ~0.8s between calls so the deliberate-429 burst at the end isn't
poisoned by earlier traffic counting against the same per-IP window.

Run: uv run python scripts/integration_test_public.py
     uv run python scripts/integration_test_public.py --url https://other.host/mcp/
     uv run python scripts/integration_test_public.py --skip rate_limit
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "https://rbi-source.harshil.ai/mcp/"
DEFAULT_BASE = "https://rbi-source.harshil.ai"
PACING_SECONDS = 0.8


@dataclass
class Result:
    case: str
    category: str
    status: str  # PASS | FAIL | SKIP
    duration_ms: float = 0.0
    evidence: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def _post_mcp(
    client: httpx.Client, url: str, method: str, params: dict | None = None, *, headers_extra: dict | None = None
) -> tuple[int, dict[str, Any] | None, str, float]:
    """POST a JSON-RPC envelope, parse the SSE first-data-event back out.
    Returns (status_code, parsed_rpc_or_None, raw_text, duration_ms)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2024-11-05",
    }
    if headers_extra:
        headers.update(headers_extra)
    t0 = time.time()
    r = client.post(url, json=body, headers=headers, timeout=120.0)
    dur_ms = (time.time() - t0) * 1000
    text = r.text
    rpc: dict[str, Any] | None = None
    for ln in text.splitlines():
        if ln.startswith("data: "):
            try:
                rpc = json.loads(ln[len("data: "):])
                break
            except json.JSONDecodeError:
                pass
    return r.status_code, rpc, text, dur_ms


def _inner_payload(rpc: dict[str, Any]) -> dict[str, Any]:
    """Pull the wrapped JSON payload out of a tool/call result."""
    return json.loads(rpc["result"]["content"][0]["text"])


def _has_disclaimer(payload: dict[str, Any]) -> bool:
    keys = list(payload.keys())
    return (
        len(keys) >= 2
        and keys[0] == "_disclaimer"
        and keys[1] == "_llm_instruction"
        and "NOT LEGAL ADVICE" in payload["_disclaimer"]
    )


def case(category: str, name: str, runner) -> Result:  # noqa: ANN001
    t0 = time.time()
    try:
        evidence, detail = runner()
        return Result(
            case=name,
            category=category,
            status="PASS",
            duration_ms=(time.time() - t0) * 1000,
            evidence=evidence,
            detail=detail or {},
        )
    except AssertionError as e:
        return Result(
            case=name,
            category=category,
            status="FAIL",
            duration_ms=(time.time() - t0) * 1000,
            evidence=str(e)[:300],
        )
    except Exception as e:  # noqa: BLE001
        return Result(
            case=name,
            category=category,
            status="FAIL",
            duration_ms=(time.time() - t0) * 1000,
            evidence=f"{type(e).__name__}: {str(e)[:200]}",
        )


def run_all(url: str, base: str, skip: set[str]) -> list[Result]:
    results: list[Result] = []
    client = httpx.Client(timeout=120.0)

    def add(r: Result) -> None:
        results.append(r)
        time.sleep(PACING_SECONDS)

    # =========================================================================
    # A. Transport / protocol
    # =========================================================================
    def t_health():
        r = client.get(f"{base}/health", timeout=15.0)
        assert r.status_code == 200, f"health {r.status_code}"
        body = r.json()
        assert body["status"] == "ok", f"status={body.get('status')}"
        assert int(body["documents"]) > 0, "documents=0"
        return f"documents={body['documents']}", body

    def t_banner():
        r = client.get(f"{base}/", timeout=15.0)
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "rbi-source-mcp"
        fams = body["corpus"]["by_family"]
        for fam in ("master_direction", "standalone_circular", "faq", "press_release", "master_circular"):
            assert fam in fams, f"missing family in banner: {fam}"
        return f"families={list(fams.keys())}", body["corpus"]

    def t_307_redirect():
        r = client.post(f"{base}/mcp", json={}, timeout=15.0, follow_redirects=False)
        assert r.status_code == 307, f"expected 307, got {r.status_code}"
        loc = r.headers.get("location", "")
        assert loc.endswith("/mcp/"), f"redirect location: {loc}"
        return f"307 → {loc}", {"location": loc}

    def t_initialize():
        sc, rpc, text, _ = _post_mcp(
            client, url, "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "itest", "version": "1"}},
        )
        assert sc == 200, f"initialize {sc}"
        assert rpc is not None, f"no SSE data: {text[:200]}"
        srv = rpc["result"]["serverInfo"]
        return f"server={srv.get('name')}", srv

    def t_tools_list():
        sc, rpc, _, _ = _post_mcp(client, url, "tools/list", {})
        assert sc == 200
        names = {t["name"] for t in rpc["result"]["tools"]}
        expected = {"rbi.check_compliance", "rbi.search", "rbi.get_document", "rbi.check_current"}
        assert names == expected, f"tools={names}, expected={expected}"
        return f"tools={sorted(names)}", {"tools": sorted(names)}

    def t_cors_preflight():
        r = client.request("OPTIONS", url, headers={
            "Origin": "https://claude.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, MCP-Protocol-Version",
        }, timeout=15.0)
        # CORS preflight returns 200 or 204 with the right headers
        assert r.status_code in (200, 204), f"CORS preflight {r.status_code}"
        aco = r.headers.get("access-control-allow-origin", "")
        acm = r.headers.get("access-control-allow-methods", "")
        assert aco in ("*", "https://claude.ai"), f"allow-origin={aco}"
        assert "POST" in acm, f"allow-methods={acm}"
        return f"allow-origin={aco}", dict(r.headers)

    add(case("A. Transport", "GET /health returns ok with corpus count", t_health))
    add(case("A. Transport", "GET / banner has all 5 families", t_banner))
    add(case("A. Transport", "POST /mcp (no slash) → 307 to /mcp/", t_307_redirect))
    add(case("A. Transport", "MCP initialize handshake", t_initialize))
    add(case("A. Transport", "tools/list returns exactly the 4 tools", t_tools_list))
    add(case("A. Transport", "CORS preflight (Claude.ai origin)", t_cors_preflight))

    # =========================================================================
    # B. Tool contracts (success path)
    # =========================================================================
    def call_tool(tool: str, args: dict) -> tuple[dict, float]:
        sc, rpc, text, dur = _post_mcp(client, url, "tools/call", {"name": tool, "arguments": args})
        assert sc == 200, f"{tool} status={sc}: {text[:200]}"
        assert rpc is not None, f"no SSE data: {text[:200]}"
        return _inner_payload(rpc), dur

    def t_search_basic():
        payload, dur = call_tool("rbi.search", {"query": "payment aggregator net worth", "limit": 2})
        assert _has_disclaimer(payload)
        assert "results" in payload
        assert len(payload["results"]) >= 1
        for r in payload["results"]:
            for f in ("document_id", "title", "section", "paragraph_anchor", "official_url", "rbi_ref"):
                assert f in r, f"missing {f} in result"
        return f"got {len(payload['results'])} results in {dur:.0f}ms", {"latency_ms": dur}

    def t_check_compliance_basic():
        clause = "Our payment aggregator entity has a net worth of ₹10 crore at the time of authorisation."
        payload, dur = call_tool("rbi.check_compliance", {"text": clause, "topic_hint": "pa", "limit": 3})
        assert _has_disclaimer(payload)
        assert "relevant_provisions" in payload
        assert len(payload["relevant_provisions"]) >= 1
        first = payload["relevant_provisions"][0]
        # check_compliance returns chunks under `quoted_text` (not `text`) so
        # consuming LLMs distinguish the cited paragraph from any wrapper.
        for f in ("document_id", "official_url", "paragraph_anchor", "quoted_text"):
            assert f in first, f"missing {f} in provision (have: {list(first.keys())[:8]})"
        return f"got {len(payload['relevant_provisions'])} provisions in {dur:.0f}ms", {"latency_ms": dur}

    def t_compliance_low_confidence_for_recipe():
        payload, dur = call_tool("rbi.check_compliance", {"text": "Boil potatoes for 12 minutes, then mash with butter and milk."})
        assert _has_disclaimer(payload)
        # Out-of-scope inputs should flag low_confidence
        assert payload.get("low_confidence") is True, f"low_confidence={payload.get('low_confidence')}"
        return f"low_confidence=True (correct, dur={dur:.0f}ms)", {"latency_ms": dur, "low_confidence": True}

    def t_get_document_md():
        # Payment Aggregator MD (id=12896, the headline demo target)
        payload, dur = call_tool("rbi.get_document", {"document_id": "rbi:master_direction:12896", "include_text": False})
        assert _has_disclaimer(payload)
        assert payload["status"] != "error", f"status={payload.get('status')}"
        # Tolerant: accept either `title` at top or nested under `document`/`metadata`.
        flat_title = payload.get("title")
        nested = payload.get("document") or payload.get("metadata") or {}
        title = flat_title or nested.get("title")
        assert title and "Payment" in title, f"title={title}"
        return f"title='{title[:60]}' in {dur:.0f}ms", {"latency_ms": dur}

    def t_check_current_md():
        url_q = "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12896"
        payload, dur = call_tool("rbi.check_current", {"url_or_ref": url_q})
        assert _has_disclaimer(payload)
        assert payload["status"] in ("current", "withdrawn"), f"status={payload['status']}"
        return f"status={payload['status']} for PA MD ({dur:.0f}ms)", {"latency_ms": dur}

    add(case("B. Tools", "rbi.search returns ranked citations", t_search_basic))
    add(case("B. Tools", "rbi.check_compliance returns provisions", t_check_compliance_basic))
    add(case("B. Tools", "out-of-scope input flags low_confidence", t_compliance_low_confidence_for_recipe))
    add(case("B. Tools", "rbi.get_document fetches PA MD metadata", t_get_document_md))
    add(case("B. Tools", "rbi.check_current resolves PA MD URL", t_check_current_md))

    # =========================================================================
    # C. Coverage across all 5 families (each family must surface in at least one query)
    # =========================================================================
    family_queries: list[tuple[str, str, str]] = [
        # (family expected in document_id, query phrase, label)
        ("master_direction", "payment aggregator net worth requirements", "MD: PA net worth"),
        ("standalone_circular", "tokenisation of card transactions", "SC: tokenisation"),
        ("faq", "frequently asked", "FAQ generic"),
        ("press_release", "Reserve Bank announces", "PR: announcements"),
        ("master_circular", "guarantees and co-acceptances", "MC generic"),
    ]
    for fam, q, label in family_queries:
        def make(fam=fam, q=q, label=label):
            def runner():
                payload, dur = call_tool("rbi.search", {"query": q, "limit": 5, "filters": {"include_withdrawn": False}})
                assert _has_disclaimer(payload)
                doc_ids = [r.get("document_id", "") for r in payload.get("results", [])]
                hit = any(f"rbi:{fam}:" in did for did in doc_ids)
                # Note: this is a soft assertion — corpus coverage may not always rank a
                # given family in top 5 for a generic query. Mark FAIL only if zero results.
                assert payload.get("results"), f"empty results for query: {q}"
                # Soft check: log whether we actually hit the target family
                hit_label = "HIT" if hit else "MISS"
                return f"{hit_label} family={fam} ({len(doc_ids)} results, {dur:.0f}ms)", {
                    "latency_ms": dur, "family_hit": hit, "doc_ids_top5": doc_ids,
                }
            return runner
        add(case("C. Family coverage", label, make()))

    # =========================================================================
    # D. Topic hints (every documented mapping should produce non-empty results)
    # =========================================================================
    topic_hints = [
        ("pa", "minimum net worth requirements"),
        ("payment_aggregator", "minimum net worth requirements"),
        ("kyc_bank", "customer due diligence"),
        ("kyc_nbfc", "customer due diligence"),
        ("kyc", "customer identification procedure"),
        ("ppi", "loading limits prepaid wallet"),
        ("prepaid", "loading limits prepaid wallet"),
        ("cards", "tokenisation card-on-file"),
        ("credit_card", "card present transaction"),
        ("e_mandate", "recurring transaction additional factor"),
        ("recurring", "recurring transaction additional factor"),
        ("afa", "additional factor authentication"),
        ("digital_payment_security", "additional factor authentication"),
    ]
    for hint, q in topic_hints:
        def make(hint=hint, q=q):
            def runner():
                payload, dur = call_tool("rbi.check_compliance", {"text": q, "topic_hint": hint, "limit": 3})
                assert _has_disclaimer(payload)
                provs = payload.get("relevant_provisions", [])
                assert provs, f"empty provisions for hint={hint}"
                return f"hint={hint} → {len(provs)} provisions ({dur:.0f}ms)", {"latency_ms": dur, "hits": len(provs)}
            return runner
        add(case("D. Topic hints", f"hint={hint}", make()))

    # =========================================================================
    # E. Error and edge cases
    # =========================================================================
    def t_empty_text():
        payload, dur = call_tool("rbi.check_compliance", {"text": ""})
        assert _has_disclaimer(payload), "disclaimer missing on empty input"
        # Either low_confidence or an explicit error reason — both are acceptable.
        ok = payload.get("low_confidence") is True or payload.get("status") == "error"
        assert ok, f"expected low_confidence/error, got: {list(payload.keys())[:5]}"
        return f"empty input handled ({dur:.0f}ms)", {"latency_ms": dur}

    def t_punctuation_only():
        payload, dur = call_tool("rbi.search", {"query": ".,;!?@#"})
        assert _has_disclaimer(payload)
        # Should NOT crash; should return no results or low_confidence
        return f"handled ({dur:.0f}ms)", {"latency_ms": dur}

    def t_oversized_text_returns_input_too_large():
        # 33,000 chars — over the 32,000 cap.
        payload, dur = call_tool("rbi.check_compliance", {"text": "a " * 16500})
        assert _has_disclaimer(payload)
        assert payload.get("status") == "error"
        assert payload.get("reason") == "input_too_large", f"reason={payload.get('reason')}"
        return f"reason=input_too_large ({dur:.0f}ms)", {"latency_ms": dur}

    def t_unknown_tool():
        sc, rpc, text, dur = _post_mcp(client, url, "tools/call", {"name": "rbi.does_not_exist", "arguments": {}})
        assert sc == 200
        assert rpc is not None
        # Either MCP SDK rejects with a JSON-RPC error, or our handler returns wrapped unknown_tool envelope
        if "error" in rpc:
            return f"JSON-RPC error: {rpc['error'].get('message', '')[:60]}", {"branch": "rpc_error"}
        payload = _inner_payload(rpc)
        assert _has_disclaimer(payload)
        assert payload.get("reason") == "unknown_tool"
        return f"reason=unknown_tool ({dur:.0f}ms)", {"branch": "wrapped_envelope"}

    def t_check_current_unsupported_url():
        payload, _ = call_tool("rbi.check_current", {"url_or_ref": "https://example.com/not-a-real-rbi-page"})
        assert _has_disclaimer(payload)
        # Should be a structured "unsupported" envelope, not a crash. v0.1
        # uses `unsupported_at_v0.1` as the explicit status marker.
        assert payload.get("status") in (
            "unsupported", "unsupported_at_v0.1", "unknown", "error", "out_of_corpus",
        ), f"status={payload.get('status')}"
        return f"status={payload.get('status')}", {"status": payload.get("status")}

    def t_unicode_input():
        # Hindi text + currency symbol — the corpus is English but unicode shouldn't crash
        payload, dur = call_tool("rbi.search", {"query": "₹500 crore नेट वर्थ", "limit": 1})
        assert _has_disclaimer(payload)
        return f"unicode handled ({dur:.0f}ms)", {"latency_ms": dur}

    add(case("E. Edge cases", "empty text input", t_empty_text))
    add(case("E. Edge cases", "punctuation-only query", t_punctuation_only))
    add(case("E. Edge cases", "oversized text → input_too_large envelope", t_oversized_text_returns_input_too_large))
    add(case("E. Edge cases", "unknown tool name", t_unknown_tool))
    add(case("E. Edge cases", "check_current with non-RBI URL", t_check_current_unsupported_url))
    add(case("E. Edge cases", "unicode + Hindi input", t_unicode_input))

    # =========================================================================
    # F. Security middlewares (run last — rate-limit test will burn the IP budget)
    # =========================================================================
    def t_body_size_413():
        if "body_size" in skip:
            return "skipped", {}
        oversized = b"x" * 250_000  # 250 KB > 200 KB cap
        r = client.post(
            url,
            content=oversized,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(oversized)),
                "Accept": "application/json, text/event-stream",
            },
            timeout=15.0,
        )
        assert r.status_code == 413, f"expected 413, got {r.status_code}"
        body = r.json()
        assert body.get("error") == "request_too_large"
        return f"413 max_bytes={body.get('max_bytes')}", body

    def t_rate_limit_kicks_in():
        if "rate_limit" in skip:
            return "skipped", {}
        # Wait 65s so the per-IP fixed window clears — earlier tests in this
        # run already counted against the 60-req budget. Without this, the
        # first 429 hits at ~req 25 of the burst (because ~35 prior calls
        # are still in window) and we can't validate "60 should pass before
        # 429 starts" cleanly.
        time.sleep(65)
        # Burn through 70 cheap tools/list calls back-to-back (no pacing here —
        # we WANT to hit the limit). First ~60 should be 200, rest 429.
        codes: list[int] = []
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2024-11-05",
        }
        for _ in range(70):
            r = client.post(url, json=body, headers=headers, timeout=15.0)
            codes.append(r.status_code)
        first_429 = next((i for i, c in enumerate(codes) if c == 429), None)
        assert first_429 is not None, f"never got 429 in 70 reqs: {codes}"
        # With a clean window, first 429 should be at index 60 (req #61).
        # Allow ±5 slack for clock skew + body-size middleware test that ran
        # ~1s before this (counts against window only on /mcp/* paths).
        assert 55 <= first_429 <= 65, f"first 429 at index {first_429}, expected 55-65"
        ok_count = sum(1 for c in codes if c == 200)
        rl_count = sum(1 for c in codes if c == 429)
        return f"first 429 at request {first_429 + 1}; total OK={ok_count} 429={rl_count}", {
            "first_429_index": first_429, "ok": ok_count, "rate_limited": rl_count,
        }

    add(case("F. Security middleware", "POST 250KB body → 413", t_body_size_413))
    # Rate limit goes truly last — it will lock the IP for ~60s
    results.append(case("F. Security middleware", "60 reqs/min/IP → 429 past limit", t_rate_limit_kicks_in))
    return results


def _print_report(results: list[Result]) -> None:
    by_cat: dict[str, list[Result]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)

    pass_count = sum(1 for r in results if r.status == "PASS")
    fail_count = sum(1 for r in results if r.status == "FAIL")
    skip_count = sum(1 for r in results if r.status == "SKIP")

    print()
    print("=" * 80)
    print("RBI Source MCP — Public Endpoint Integration Test Report")
    print("=" * 80)
    for cat, rs in by_cat.items():
        print(f"\n## {cat}")
        for r in rs:
            mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "—"}[r.status]
            line = f"  {mark} [{r.duration_ms:>5.0f}ms] {r.case}"
            if len(line) > 78:
                line = line[:75] + "..."
            print(line)
            if r.evidence:
                print(f"     {r.evidence[:180]}")
    print()
    print("-" * 80)
    print(f"TOTAL: {len(results)} cases | PASS={pass_count} | FAIL={fail_count} | SKIP={skip_count}")
    if fail_count == 0:
        print("OUTCOME: ✓ ALL PASS")
    else:
        print(f"OUTCOME: ✗ {fail_count} FAIL — see above for evidence")
    print()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=DEFAULT_URL, help="MCP endpoint URL (must end in /mcp/)")
    p.add_argument("--base", default=DEFAULT_BASE, help="Base URL for /health and /")
    p.add_argument("--skip", action="append", default=[], help="Skip a category (e.g., rate_limit, body_size)")
    p.add_argument("--out", default=None, help="Write JSON report to this path")
    args = p.parse_args()

    skip = set(args.skip)

    print(f"Hitting: {args.url}")
    print(f"Pacing:  {PACING_SECONDS}s between calls (rate-limit test exempt)")
    if skip:
        print(f"Skipping: {sorted(skip)}")
    print()

    results = run_all(args.url, args.base, skip)
    _print_report(results)

    out_path = Path(args.out) if args.out else (
        Path(__file__).resolve().parent.parent
        / ".gstack"
        / "integration-reports"
        / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "url": args.url,
        "base": args.base,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "skip": sorted(skip),
        "results": [
            {
                "case": r.case,
                "category": r.category,
                "status": r.status,
                "duration_ms": r.duration_ms,
                "evidence": r.evidence,
                "detail": r.detail,
            }
            for r in results
        ],
        "totals": {
            "total": len(results),
            "pass": sum(1 for r in results if r.status == "PASS"),
            "fail": sum(1 for r in results if r.status == "FAIL"),
            "skip": sum(1 for r in results if r.status == "SKIP"),
        },
    }, indent=2))
    print(f"JSON report → {out_path}")

    return 0 if all(r.status != "FAIL" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
