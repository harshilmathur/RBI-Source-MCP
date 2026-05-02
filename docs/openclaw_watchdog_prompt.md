# OpenClaw watchdog prompt for rbi-source-mcp.fly.dev

Drop this into an OpenClaw cron schedule (every 15 min during business hours,
hourly otherwise is plenty for low-usage). The prompt is self-contained — the
spawned session has no memory of any prior conversation, so all the context
it needs is inline.

## The prompt

```
You are a watchdog session. Your job is one specific task: probe the public
RBI Source MCP at https://rbi-source-mcp.fly.dev, decide if it's healthy,
and emit a clear pass/fail signal. Do nothing else.

Run the inline Python below via Bash. It performs five fast probes and
exits 0 (healthy), 1 (degraded — alert), or 2 (unreachable — page).

Bash command:

    python3 - <<'PY'
    import json, statistics, sys, time, urllib.error, urllib.request
    URL = "https://rbi-source-mcp.fly.dev"
    EXPECTED_TOOLS = {"rbi_check_compliance", "rbi_search", "rbi_get_document", "rbi_check_current"}
    KNOWN_GOOD_DOC = "rbi:master_direction:12896"

    def mcp(method, params, rid=1, timeout=20):
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode()
        req = urllib.request.Request(f"{URL}/mcp", data=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json,text/event-stream",
        })
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
        for line in raw.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:]), time.time() - t0
        raise RuntimeError("no SSE data")

    def tool(name, args, rid):
        resp, dt = mcp("tools/call", {"name": name, "arguments": args}, rid=rid)
        res = resp.get("result", {})
        txt = (res.get("content") or [{}])[0].get("text", "{}")
        try:
            return json.loads(txt), dt
        except Exception:
            return {"_raw": txt}, dt

    failures = []
    detail = {}
    rate_limited = False

    # 1. /health
    try:
        t0 = time.time()
        with urllib.request.urlopen(f"{URL}/health", timeout=10) as r:
            d = json.loads(r.read())
        detail["documents"] = d.get("documents")
        detail["health_ms"] = round((time.time() - t0) * 1000, 1)
        if d.get("status") != "ok" or (d.get("documents") or 0) < 100:
            failures.append(f"/health bad payload: {d}")
    except Exception as e:
        print(json.dumps({"ok": False, "fatal": True, "failures": [f"/health unreachable: {e!r}"]}))
        sys.exit(2)

    # 2. tools/list
    try:
        resp, _ = mcp("tools/list", {})
        names = {t["name"] for t in resp["result"]["tools"]}
        detail["tools"] = len(names)
        missing = EXPECTED_TOOLS - names
        if missing:
            failures.append(f"tools missing: {missing}")
    except Exception as e:
        failures.append(f"tools/list: {e!r}")

    # 3. Known-good query
    try:
        result, dt = tool("rbi_check_compliance",
                          {"text": "minimum net worth requirement for payment aggregator",
                           "topic_hint": "pa"}, rid=2)
        provs = result.get("relevant_provisions") or []
        detail["top1"] = provs[0].get("document_id") if provs else None
        if not provs:
            failures.append("known-good returned no provisions")
        elif provs[0].get("document_id") != KNOWN_GOOD_DOC:
            failures.append(f"known-good top1={detail['top1']} (expected {KNOWN_GOOD_DOC})")
        if "_disclaimer" not in result:
            failures.append("missing _disclaimer")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            rate_limited = True
            detail["top1"] = "rate_limited"
        else:
            failures.append(f"known-good HTTP {e.code}")
    except Exception as e:
        failures.append(f"known-good: {e!r}")

    # 4. Negative case (no embed call → no rate-limit risk)
    try:
        result, _ = tool("rbi_check_compliance", {"text": ""}, rid=3)
        if not result.get("low_confidence") and (result.get("relevant_provisions") or []):
            failures.append("empty input did not flag low_confidence")
    except Exception as e:
        failures.append(f"negative: {e!r}")

    # 5. Latency p95 over 2 calls (skip if already rate-limited)
    if not rate_limited:
        lats = []
        for i, q in enumerate(["payment aggregator", "ppi loading limit"]):
            try:
                _, dt = tool("rbi_check_compliance", {"text": q}, rid=4 + i)
                lats.append(dt * 1000)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    rate_limited = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if lats:
            detail["p95_ms"] = round(max(lats), 1)
            if max(lats) > 5000:
                failures.append(f"p95 {max(lats):.0f}ms exceeds 5s budget")

    if rate_limited:
        detail["note"] = "CF embeddings rate-limited (capacity, not availability)"

    out = {"ok": not failures, "url": URL, "n_failures": len(failures),
           "failures": failures, **detail}
    print(json.dumps(out))
    sys.exit(0 if not failures else 1)
    PY

After it runs, parse the JSON line on stdout and the exit code:

  - exit 0 → silent. Do not push a notification. Do not open an issue.
    The watchdog is meant to be quiet on success.

  - exit 1 → degraded. Send a PushNotification with title
    "rbi-source-mcp watchdog: degraded" and body listing the failures.
    Do NOT open a GitHub issue for transient blips; only open one if
    the same failures repeat across two consecutive runs (the previous
    watchdog session would have left a note in the project's checkpoint
    file or you can read recent timeline events to detect repeats).

  - exit 2 → unreachable. Send a PushNotification immediately:
    title "rbi-source-mcp watchdog: SERVICE DOWN", body with the JSON.
    This is page-worthy for a public MCP.

If "rate_limited" appears in the output, do NOT treat it as a failure.
It means CF Workers AI throttled us — the MCP itself is up and well.
Note it once but don't escalate.

Do not run any other tools. Do not modify code. Do not deploy. Your only
job is probe + report. Keep your final response under 100 words.
```

## Why this shape

- **Single Bash invocation, all logic inline.** No git checkout, no
  dependencies beyond Python 3 stdlib. The OpenClaw runner can be on a
  fresh box and this still works.
- **No tools beyond Bash.** The agent isn't supposed to investigate or
  fix; just probe and signal. Restricting toolset matches the job.
- **Exit codes drive the action mapping.** The agent has a clean three-way
  decision: silent / notify / page. No ambiguity.
- **Rate-limit-aware.** CF Workers AI's free tier (10K neurons/day, plus
  per-minute caps) is the most likely intermittent failure mode. The
  prompt explicitly tells the agent not to alarm on 429s.
- **Repeat-failure logic is delegated to the agent.** The prompt asks
  the agent to use OpenClaw's session-history hooks (timeline.jsonl,
  checkpoint.md) to decide if the failure is recurring. This avoids
  alert spam on transient blips.

## Recommended schedule

```
*/15  9-23 * * *   # every 15 min during waking hours (IST)
0     0-8 * * *    # hourly overnight
```

Or simpler, every 30 min round the clock if you don't care about
overnight noise: `*/30 * * * *`.

## Tuning

- `KNOWN_GOOD_DOC = "rbi:master_direction:12896"` — the PA MD. If you
  later remove or replace that document, update this constant or the
  watchdog will false-alarm. Pick a different stable document_id from
  `documents` table that you don't expect to churn.
- `5000` ms p95 budget is generous (typical p95 is < 1s). Tighten to
  3000 if you want earlier warning of CF degradation.
- The "two consecutive failures before opening an issue" rule is
  optional — if you'd rather alert on every failure, drop that
  constraint in the prompt.
