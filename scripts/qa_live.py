"""Deep QA harness for the live RBI Source MCP.

Hits a deployed instance over HTTPS and runs ~50 probes across all four
MCP tools, with realistic positive cases, adversarial inputs, schema
checks, latency profiling, and disclaimer audit. Designed to be the
post-deploy sanity check and the source of truth for "is the live
service correct, not just up."

Usage:
    uv run python scripts/qa_live.py
    uv run python scripts/qa_live.py --url https://rbi-source-mcp.fly.dev
    uv run python scripts/qa_live.py --json    # machine-readable

Exit code:
    0 — every probe passed (or only soft warnings)
    1 — one or more hard failures
    2 — couldn't reach the service at all

Distinct from scripts/watchdog.py: this is the deep, slow (~30-60s),
human-readable post-deploy check. The watchdog is the fast, light
check that's safe to run from cron.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Probe:
    name: str
    section: str
    status: str  # "pass" | "fail" | "warn"
    detail: str = ""
    latency_ms: float | None = None


@dataclass(slots=True)
class Report:
    base_url: str
    started_at: float
    probes: list[Probe] = field(default_factory=list)

    def add(self, **kw: Any) -> Probe:
        p = Probe(**kw)
        self.probes.append(p)
        return p

    @property
    def passed(self) -> int:
        return sum(1 for p in self.probes if p.status == "pass")

    @property
    def warned(self) -> int:
        return sum(1 for p in self.probes if p.status == "warn")

    @property
    def failed(self) -> int:
        return sum(1 for p in self.probes if p.status == "fail")


def call_mcp(url: str, method: str, params: dict[str, Any], rid: int = 1, timeout: float = 30.0) -> tuple[dict, float]:
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


def call_tool(url: str, tool: str, args: dict[str, Any], rid: int, timeout: float = 30.0) -> tuple[dict, float]:
    resp, elapsed = call_mcp(
        url, "tools/call", {"name": tool, "arguments": args}, rid=rid, timeout=timeout
    )
    if "error" in resp:
        return {"_jsonrpc_error": resp["error"]}, elapsed
    res = resp.get("result", {})
    is_err = res.get("isError", False)
    content = res.get("content") or []
    if not content:
        return {"_empty_content": True, "_isError": is_err}, elapsed
    txt = content[0].get("text", "")
    try:
        body = json.loads(txt)
        body["_isError"] = is_err
        return body, elapsed
    except json.JSONDecodeError:
        return {"_raw": txt, "_isError": is_err}, elapsed


# ---------------------------------------------------------------------------
# Probe sections
# ---------------------------------------------------------------------------


def probe_health(url: str, r: Report) -> dict:
    try:
        t0 = time.time()
        with urllib.request.urlopen(f"{url}/health", timeout=10) as resp:
            data = json.loads(resp.read())
        dt_ms = (time.time() - t0) * 1000
        if data.get("status") == "ok" and data.get("documents", 0) > 100:
            r.add(name="/health", section="health", status="pass",
                  detail=f"documents={data['documents']}", latency_ms=dt_ms)
        else:
            r.add(name="/health", section="health", status="fail",
                  detail=f"unexpected payload: {data}", latency_ms=dt_ms)
        return data
    except Exception as exc:
        r.add(name="/health", section="health", status="fail", detail=repr(exc))
        return {}


def probe_tools_list(url: str, r: Report) -> dict[str, dict]:
    try:
        resp, dt = call_mcp(url, "tools/list", {}, rid=1)
        tools = resp["result"]["tools"]
        names = sorted(t["name"] for t in tools)
        expected = {"rbi_check_compliance", "rbi_search", "rbi_get_document", "rbi_check_current"}
        missing = expected - set(names)
        extras = set(names) - expected
        if missing:
            r.add(name="tools/list", section="schema", status="fail",
                  detail=f"missing tools: {missing}", latency_ms=dt * 1000)
        else:
            r.add(name="tools/list", section="schema", status="pass",
                  detail=f"{len(tools)} tools", latency_ms=dt * 1000)
        if extras:
            r.add(name="tools/list extras", section="schema", status="warn",
                  detail=f"unexpected tools: {extras}")
        # schema sanity per tool
        by_name: dict[str, dict] = {}
        for t in tools:
            by_name[t["name"]] = t
            schema = t.get("inputSchema") or {}
            if schema.get("type") != "object" or "properties" not in schema:
                r.add(name=f"{t['name']}.inputSchema", section="schema", status="fail",
                      detail=f"malformed schema: {schema}")
            if not (t.get("description") or "").strip():
                r.add(name=f"{t['name']}.description", section="schema", status="warn",
                      detail="empty description")
        r.add(name="all tools have valid inputSchema", section="schema", status="pass")
        return by_name
    except Exception as exc:
        r.add(name="tools/list", section="schema", status="fail", detail=repr(exc))
        return {}


def probe_positive_cases(url: str, r: Report) -> None:
    """Realistic queries across the 7 MD coverage areas. Top-1 must hit
    the expected document_id; anchor is a soft check (chunking can vary)."""
    cases = [
        ("PA min net worth", "minimum net worth requirement for payment aggregator", "pa", "rbi:master_direction:12896"),
        ("PA escrow merchants", "escrow account settlement to merchants", "pa", "rbi:master_direction:12896"),
        ("PPI loading limit", "what is the loading limit for prepaid payment instruments", "ppi", "rbi:master_direction:12156"),
        ("PPI reload sources", "permitted reload sources for semi-closed wallets", "ppi", "rbi:master_direction:12156"),
        ("E-mandate pre-debit", "pre-debit notification for e-mandate", "e_mandate", "rbi:master_direction:13374"),
        ("E-mandate transaction limits", "transaction limits and velocity for e-mandate", "e_mandate", "rbi:master_direction:13374"),
        ("KYC NBFC periodic updation", "periodic updation of KYC for NBFCs", "kyc_nbfc", "rbi:master_direction:12943"),
        ("KYC bank video CDD", "video-based customer identification process for banks", "kyc_bank", None),  # any KYC MD
        ("Cards tokenisation", "tokenisation of card details for merchant storage", "cards", "rbi:master_direction:13155"),
        ("Cards co-branded", "co-branded credit card arrangements", "cards", "rbi:master_direction:13155"),
        ("Security AFA", "additional factor authentication for digital wallet transactions", "afa", "rbi:master_direction:12032"),
        ("Security encryption", "encryption requirements for digital payment channels", "afa", "rbi:master_direction:12032"),
    ]
    for label, query, hint, expected_doc in cases:
        try:
            result, dt = call_tool(url, "rbi_check_compliance", {"text": query, "topic_hint": hint}, rid=100)
            provs = result.get("relevant_provisions") or []
            if not provs:
                r.add(name=f"positive: {label}", section="retrieval", status="fail",
                      detail="no provisions returned", latency_ms=dt * 1000)
                continue
            top1 = provs[0]
            if expected_doc is not None and top1.get("document_id") != expected_doc:
                r.add(name=f"positive: {label}", section="retrieval", status="warn",
                      detail=f"top1={top1.get('document_id')}::{top1.get('paragraph_anchor')} (expected {expected_doc})",
                      latency_ms=dt * 1000)
            else:
                r.add(name=f"positive: {label}", section="retrieval", status="pass",
                      detail=f"top1={top1.get('document_id')}::{top1.get('paragraph_anchor')}",
                      latency_ms=dt * 1000)
            if "_disclaimer" not in result:
                r.add(name=f"{label} disclaimer", section="disclaimer", status="fail",
                      detail="missing _disclaimer")
        except Exception as exc:
            r.add(name=f"positive: {label}", section="retrieval", status="fail", detail=repr(exc))


def probe_negative_cases(url: str, r: Report) -> None:
    """Adversarial inputs. We expect either low_confidence=True or empty results."""
    cases = [
        ("empty text", {"text": ""}),
        ("whitespace only", {"text": "   \n\t  "}),
        ("gibberish", {"text": "blarghity blarghity foo bar baz xyzzy quux"}),
    ]
    for label, args in cases:
        try:
            result, dt = call_tool(url, "rbi_check_compliance", args, rid=200)
            low_conf = result.get("low_confidence", False)
            n_prov = len(result.get("relevant_provisions") or [])
            if low_conf or n_prov == 0:
                r.add(name=f"negative: {label}", section="negative", status="pass",
                      detail=f"low_conf={low_conf} n_prov={n_prov}", latency_ms=dt * 1000)
            else:
                r.add(name=f"negative: {label}", section="negative", status="warn",
                      detail=f"returned {n_prov} provisions w/ low_conf=False",
                      latency_ms=dt * 1000)
        except Exception as exc:
            r.add(name=f"negative: {label}", section="negative", status="fail", detail=repr(exc))


def probe_oversized_input(url: str, r: Report) -> None:
    try:
        big = ("payment aggregator merchant settlement minimum net worth requirement " * 200)[:15000]
        result, dt = call_tool(url, "rbi_check_compliance", {"text": big}, rid=300, timeout=45)
        if "_jsonrpc_error" in result:
            r.add(name="oversized input", section="edge", status="warn",
                  detail=f"rejected: {result['_jsonrpc_error']}")
        elif result.get("relevant_provisions"):
            r.add(name="oversized input", section="edge", status="pass",
                  detail=f"handled {len(big)} chars", latency_ms=dt * 1000)
        else:
            r.add(name="oversized input", section="edge", status="warn",
                  detail="no provisions returned", latency_ms=dt * 1000)
    except Exception as exc:
        r.add(name="oversized input", section="edge", status="fail", detail=repr(exc))


def probe_search(url: str, r: Report) -> None:
    cases = [
        ("free text", {"query": "payment aggregator escrow"}, True),
        ("with limit=3", {"query": "tokenisation", "limit": 3}, True),
        ("filter include_withdrawn", {"query": "PPI loading", "filters": {"include_withdrawn": True}}, True),
        ("empty query", {"query": ""}, False),
    ]
    for label, args, expect_results in cases:
        try:
            result, dt = call_tool(url, "rbi_search", args, rid=400)
            if "_jsonrpc_error" in result:
                if not expect_results:
                    r.add(name=f"rbi_search: {label}", section="search", status="pass",
                          detail="rejected as expected", latency_ms=dt * 1000)
                else:
                    r.add(name=f"rbi_search: {label}", section="search", status="fail",
                          detail=str(result["_jsonrpc_error"]))
                continue
            results = result.get("results") or result.get("documents") or []
            if expect_results and results:
                r.add(name=f"rbi_search: {label}", section="search", status="pass",
                      detail=f"{len(results)} results", latency_ms=dt * 1000)
            elif not expect_results and not results:
                r.add(name=f"rbi_search: {label}", section="search", status="pass",
                      detail="empty as expected", latency_ms=dt * 1000)
            else:
                r.add(name=f"rbi_search: {label}", section="search", status="warn",
                      detail=f"{len(results)} results, expect={expect_results}",
                      latency_ms=dt * 1000)
        except Exception as exc:
            r.add(name=f"rbi_search: {label}", section="search", status="fail", detail=repr(exc))


def probe_get_document(url: str, r: Report) -> None:
    cases = [
        ("valid PA MD", {"document_id": "rbi:master_direction:12896"}, True),
        ("valid PPI MD", {"document_id": "rbi:master_direction:12156"}, True),
        ("with include_text=true", {"document_id": "rbi:master_direction:12896", "include_text": True}, True),
        ("nonexistent", {"document_id": "rbi:master_direction:99999999"}, False),
        ("malformed id", {"document_id": "not-a-valid-id"}, False),
    ]
    for label, args, expect_ok in cases:
        try:
            result, dt = call_tool(url, "rbi_get_document", args, rid=500)
            if "_jsonrpc_error" in result:
                if not expect_ok:
                    r.add(name=f"rbi_get_document: {label}", section="documents", status="pass",
                          detail="rejected gracefully", latency_ms=dt * 1000)
                else:
                    r.add(name=f"rbi_get_document: {label}", section="documents", status="fail",
                          detail=str(result["_jsonrpc_error"]))
                continue
            status = result.get("status")
            title = result.get("title") or result.get("document", {}).get("title") if isinstance(result.get("document"), dict) else None
            if expect_ok:
                if status not in ("error", "not_found", "unknown") and (title or result.get("document_id")):
                    r.add(name=f"rbi_get_document: {label}", section="documents", status="pass",
                          detail=f"title={(title or '?')[:50]}", latency_ms=dt * 1000)
                    if args.get("include_text"):
                        body = result.get("body") or result.get("text") or ""
                        if len(body) > 1000:
                            r.add(name=f"  body present ({len(body)} chars)", section="documents", status="pass")
                        else:
                            r.add(name=f"  body short ({len(body)} chars)", section="documents", status="warn",
                                  detail="include_text=true but body is short or missing")
                else:
                    r.add(name=f"rbi_get_document: {label}", section="documents", status="fail",
                          detail=f"status={status} title={title!r}")
            else:
                if status in ("error", "not_found", "unknown") or not title:
                    r.add(name=f"rbi_get_document: {label}", section="documents", status="pass",
                          detail=f"handled missing (status={status})", latency_ms=dt * 1000)
                else:
                    r.add(name=f"rbi_get_document: {label}", section="documents", status="warn",
                          detail=f"unexpected non-empty response: status={status}")
        except Exception as exc:
            r.add(name=f"rbi_get_document: {label}", section="documents", status="fail", detail=repr(exc))


def probe_check_current(url: str, r: Report) -> None:
    cases = [
        ("MD URL → current", {"url_or_ref": "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12896"}, "current"),
        ("withdrawn circular URL → withdrawn", {"url_or_ref": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12919&Mode=0"}, "withdrawn"),
        ("unknown circular URL → not_withdrawn", {"url_or_ref": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=99999&Mode=0"}, "not_withdrawn"),
        ("internal ref → unsupported", {"url_or_ref": "rbi:master_direction:12896"}, "unsupported_at_v0.1"),
    ]
    for label, args, expected_status in cases:
        try:
            result, dt = call_tool(url, "rbi_check_current", args, rid=600)
            if "_jsonrpc_error" in result:
                r.add(name=f"rbi_check_current: {label}", section="freshness", status="fail",
                      detail=str(result["_jsonrpc_error"]))
                continue
            status = result.get("status")
            if status == expected_status:
                r.add(name=f"rbi_check_current: {label}", section="freshness", status="pass",
                      detail=f"status={status}", latency_ms=dt * 1000)
            else:
                r.add(name=f"rbi_check_current: {label}", section="freshness", status="warn",
                      detail=f"status={status} (expected {expected_status})", latency_ms=dt * 1000)
        except Exception as exc:
            r.add(name=f"rbi_check_current: {label}", section="freshness", status="fail", detail=repr(exc))


def probe_disclaimer_audit(url: str, r: Report) -> None:
    """Disclaimer must be present on every successful response. We sample
    one of each tool to confirm."""
    samples = [
        ("rbi_check_compliance", {"text": "minimum net worth"}),
        ("rbi_search", {"query": "tokenisation", "limit": 1}),
        ("rbi_get_document", {"document_id": "rbi:master_direction:12896"}),
        ("rbi_check_current", {"url_or_ref": "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12896"}),
    ]
    for tool, args in samples:
        try:
            result, _ = call_tool(url, tool, args, rid=700)
            disc = result.get("_disclaimer", "")
            instr = result.get("_llm_instruction", "")
            if "RBI" in disc and "NOT LEGAL" in disc.upper():
                r.add(name=f"{tool} _disclaimer", section="disclaimer", status="pass",
                      detail="present + has RBI + NOT LEGAL")
            else:
                r.add(name=f"{tool} _disclaimer", section="disclaimer", status="fail",
                      detail=f"missing or weak: {disc[:80]!r}")
            if not instr:
                r.add(name=f"{tool} _llm_instruction", section="disclaimer", status="warn",
                      detail="absent — LLM may drop disclaimer")
        except Exception as exc:
            r.add(name=f"{tool} disclaimer audit", section="disclaimer", status="fail", detail=repr(exc))


def probe_latency(url: str, r: Report) -> None:
    """Warm-call latency over 15 sequential rbi_check_compliance calls."""
    queries = [
        "payment aggregator net worth",
        "ppi loading limit",
        "kyc periodic updation",
        "tokenisation card",
        "additional factor authentication",
        "e-mandate pre-debit",
        "encryption for digital payments",
        "fraud monitoring alerts",
        "escrow account settlement",
        "ppi reload sources",
        "minimum capital nbfc",
        "merchant onboarding due diligence",
        "outsourcing arrangements",
        "digital banking customer protection",
        "data localisation",
    ]
    latencies: list[float] = []
    for q in queries:
        try:
            _, dt = call_tool(url, "rbi_check_compliance", {"text": q}, rid=800, timeout=30)
            latencies.append(dt * 1000)
        except Exception:
            pass
    if not latencies:
        r.add(name="latency profile", section="latency", status="fail",
              detail="all probes failed")
        return
    p50 = statistics.median(latencies)
    p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) >= 2 else latencies[0]
    detail = (
        f"n={len(latencies)} p50={p50:.0f}ms p95={p95:.0f}ms "
        f"min={min(latencies):.0f}ms max={max(latencies):.0f}ms"
    )
    if p95 < 3000:
        r.add(name="latency p95 < 3s", section="latency", status="pass", detail=detail)
    elif p95 < 5000:
        r.add(name="latency p95 < 5s", section="latency", status="warn", detail=detail)
    else:
        r.add(name="latency p95", section="latency", status="fail", detail=f"too slow: {detail}")


def probe_concurrency(url: str, r: Report, n: int = 5) -> None:
    """Light parallel load. n=5 keeps it polite for low-usage hosting."""
    import threading
    results: list[tuple[bool, float]] = []
    lock = threading.Lock()

    def worker():
        try:
            _, dt = call_tool(url, "rbi_check_compliance", {"text": "ppi loading limit"}, rid=900, timeout=30)
            with lock:
                results.append((True, dt * 1000))
        except Exception:
            with lock:
                results.append((False, 0.0))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    wall = (time.time() - t0) * 1000
    ok = sum(1 for s, _ in results if s)
    if ok == n:
        avg = statistics.mean(dt for s, dt in results if s)
        r.add(name=f"{n} parallel calls", section="concurrency", status="pass",
              detail=f"all {n} ok, mean={avg:.0f}ms, wall={wall:.0f}ms")
    else:
        r.add(name=f"{n} parallel calls", section="concurrency", status="warn",
              detail=f"{ok}/{n} succeeded under load")


def probe_error_handling(url: str, r: Report) -> None:
    """JSON-RPC layer: missing required fields, unknown tool, malformed args."""
    cases = [
        ("missing required text", {"name": "rbi_check_compliance", "arguments": {}}, True),
        ("unknown tool", {"name": "rbi_does_not_exist", "arguments": {"text": "x"}}, True),
        ("missing url_or_ref", {"name": "rbi_check_current", "arguments": {}}, True),
    ]
    for label, params, expect_is_error in cases:
        try:
            resp, dt = call_mcp(url, "tools/call", params, rid=950)
            res = resp.get("result", {})
            is_err = res.get("isError", False)
            jsonrpc_err = "error" in resp
            if expect_is_error and (is_err or jsonrpc_err):
                r.add(name=f"error: {label}", section="error_handling", status="pass",
                      detail=f"isError={is_err} jsonrpc_err={jsonrpc_err}",
                      latency_ms=dt * 1000)
            elif not expect_is_error and not is_err and not jsonrpc_err:
                r.add(name=f"error: {label}", section="error_handling", status="pass",
                      detail="ok", latency_ms=dt * 1000)
            else:
                r.add(name=f"error: {label}", section="error_handling", status="warn",
                      detail=f"isError={is_err} jsonrpc_err={jsonrpc_err}, expect={expect_is_error}")
        except Exception as exc:
            r.add(name=f"error: {label}", section="error_handling", status="fail", detail=repr(exc))


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


def render_text(r: Report) -> None:
    by_section: dict[str, list[Probe]] = {}
    for p in r.probes:
        by_section.setdefault(p.section, []).append(p)
    for section, probes in by_section.items():
        print(f"\n=== {section} ===")
        for p in probes:
            marker = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[p.status]
            lat = f" [{p.latency_ms:.0f}ms]" if p.latency_ms is not None else ""
            sep = "  " + ("— " + p.detail if p.detail else "")
            print(f"  {marker:4s}  {p.name}{lat}{sep}")
    print()
    print("=" * 70)
    print(f"SUMMARY  pass={r.passed}  warn={r.warned}  fail={r.failed}  "
          f"total={len(r.probes)}  elapsed={time.time() - r.started_at:.1f}s")
    print("=" * 70)


def render_json(r: Report) -> None:
    print(json.dumps({
        "base_url": r.base_url,
        "started_at": r.started_at,
        "elapsed_s": round(time.time() - r.started_at, 1),
        "summary": {
            "pass": r.passed,
            "warn": r.warned,
            "fail": r.failed,
            "total": len(r.probes),
        },
        "probes": [
            {
                "name": p.name,
                "section": p.section,
                "status": p.status,
                "detail": p.detail,
                "latency_ms": p.latency_ms,
            }
            for p in r.probes
        ],
    }, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://rbi-source-mcp.fly.dev",
                    help="Base URL of the live MCP (default: production)")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON instead of human-readable text")
    args = ap.parse_args()

    r = Report(base_url=args.url, started_at=time.time())

    # Reachability gate. If /health doesn't return, fail fast with code 2.
    try:
        urllib.request.urlopen(f"{args.url}/health", timeout=10).read()
    except Exception as exc:
        print(f"FATAL: {args.url} is unreachable: {exc}", file=sys.stderr)
        return 2

    probe_health(args.url, r)
    probe_tools_list(args.url, r)
    probe_positive_cases(args.url, r)
    probe_negative_cases(args.url, r)
    probe_oversized_input(args.url, r)
    probe_search(args.url, r)
    probe_get_document(args.url, r)
    probe_check_current(args.url, r)
    probe_disclaimer_audit(args.url, r)
    probe_error_handling(args.url, r)
    probe_concurrency(args.url, r)
    probe_latency(args.url, r)

    (render_json if args.json else render_text)(r)
    return 1 if r.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
