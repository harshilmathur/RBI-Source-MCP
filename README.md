# RBI Source MCP

> **The entire RBI corpus, version-aware, citation-first, in your AI workflow.**

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that brings authoritative Reserve Bank of India regulatory information into any MCP-capable client (Claude Code, Claude Desktop, Claude.ai, ChatGPT, Cursor, Cowork, Cline, Continue, Goose, Zed, ...) with hybrid retrieval over Master Directions, Circulars, Press Releases, FAQs, and Master Circulars, citation-first responses, and explicit withdrawal/supersession detection.

**Live hosted endpoint** (free, no auth): `https://rbi-source.harshil.ai/mcp/` · [install in 30s ↓](#quick-start--install-in-30-seconds)

> ⚠️ **Unofficial, community-maintained open-source tool.** RBI Source MCP is not affiliated with, endorsed by, or sponsored by the Reserve Bank of India. RBI® is a trademark of the Reserve Bank of India. For takedown or legal inquiries: open an issue on this repo (5 business-day response SLA).

## Why this exists

Fintech builders writing a clause, PRD section, or partner agreement currently ping the compliance team in Slack and wait hours-to-days to know if their language conflicts with a current RBI rule. Or they Google `rbi.org.in`, land on an ASP.NET page that may or may not be a withdrawn circular, and ship against rules superseded three years ago.

RBI Source MCP collapses this loop. Paste a clause or section into Claude/Cursor with this MCP connected, and Claude returns the relevant RBI provisions with citations, paragraph anchors, source URLs, and current/withdrawn status. **Retrieval-only by design** — this MCP doesn't issue compliance verdicts; the LLM synthesizes from the cited source material, the user makes the call.

**The visceral demo:** paste a clause from a draft TOS into Claude → cited paragraphs from the relevant Master Direction (e.g., Payment Aggregator MD, KYC MD, PPI MD, Cards MD), with paragraph anchors, the official RBI URL, and the published date. Verify with a human compliance reviewer before shipping.

## Coverage (current)

| Content family | Docs | Chunks | Source |
|---|---|---|---|
| **Master Directions** | 342 | 50,088 | `BS_ViewMasDirections.aspx` |
| **Standalone Circulars** | 290 | 1,532 | `BS_ViewListofstandalonecirculars.aspx` |
| **FAQs** | 98 | 1,828 | `FAQView.aspx` |
| **Press Releases** | 54 | 357 | `BS_PressReleaseDisplay.aspx` |
| **Master Circulars** | 19 | 2,848 | `BS_ViewMasterCirculardetails.aspx` |
| **TOTAL** | **803** | **56,653** | |

Plus **9,908 withdrawn-circular records** indexed for `check_current` lookup (metadata only, no full-text indexing).

Hybrid retrieval = FTS5 (BM25 sparse) + sqlite-vec (`bge-small-en-v1.5` 384-dim dense) fused via Reciprocal Rank Fusion (k=60). Every response carries a mandatory legal disclaimer. Eval gate: 25 hand-labeled compliance cases, must pass at ≥80%; currently 100%.

## Quick Start — install in 30 seconds

The hosted endpoint is live and free to use:

```
https://rbi-source.harshil.ai/mcp/
```

Public, unauthenticated, retrieval-only. Mumbai (Fly.io). Every response carries the mandatory legal disclaimer.

> Try it without installing anything:
> ```bash
> curl -sS https://rbi-source.harshil.ai/health
> # → {"version":"0.1.0-dev","status":"ok","documents":803}
> ```

### Connect from your client

| Client | How |
|---|---|
| **Claude Code** (CLI) | `claude mcp add rbi-source --transport http https://rbi-source.harshil.ai/mcp/` |
| **Claude Desktop** | Edit `claude_desktop_config.json`, see [block below](#claude-desktop) |
| **Claude.ai** (Pro/Team/Enterprise) | Settings → **Connectors** → **Add custom connector** → paste the URL |
| **ChatGPT** (Plus/Pro/Team/Enterprise) | Settings → **Connectors** → **Create custom connector** → paste the URL. ⚠️ See [ChatGPT note](#chatgpt-note) |
| **Cursor** | Settings → **MCP** → **Add new MCP server** → type `streamable-http`, URL: `https://rbi-source.harshil.ai/mcp/` |
| **Cowork** | Settings → MCP servers → Add → URL: `https://rbi-source.harshil.ai/mcp/`, transport: HTTP |
| **Cline** (VSCode) | `~/.cline/mcp_settings.json`, see [block below](#cline-vscode) |
| **Continue.dev** | `~/.continue/config.json`, see [block below](#continuedev) |
| **Goose** (Block) | `~/.config/goose/config.yaml`, see [block below](#goose) |
| **Zed** | `~/.config/zed/settings.json`, see [block below](#zed) |
| **Anything else** that speaks MCP streamable-HTTP | Point it at `https://rbi-source.harshil.ai/mcp/` |

After connecting, the four tools (`rbi_check_compliance`, `rbi_search`, `rbi_get_document`, `rbi_check_current`) become available in any conversation. Try:

> *"Use the RBI Source MCP. What are the net-worth requirements for a payment aggregator?"*

### Config snippets

#### Claude Desktop

Open `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows) and add:

```json
{
  "mcpServers": {
    "rbi-source": {
      "type": "http",
      "url": "https://rbi-source.harshil.ai/mcp/"
    }
  }
}
```

Restart Claude Desktop. The MCP icon should appear at the bottom of the chat box.

#### Cline (VSCode)

In Cline's settings, click **MCP Servers** → **Edit MCP Settings**, then add:

```json
{
  "mcpServers": {
    "rbi-source": {
      "type": "streamableHttp",
      "url": "https://rbi-source.harshil.ai/mcp/"
    }
  }
}
```

#### Continue.dev

In `~/.continue/config.json`:

```json
{
  "experimental": {
    "modelContextProtocolServers": [
      {
        "transport": {
          "type": "streamable-http",
          "url": "https://rbi-source.harshil.ai/mcp/"
        }
      }
    ]
  }
}
```

#### Goose

In `~/.config/goose/config.yaml`:

```yaml
extensions:
  rbi-source:
    type: streamable_http
    uri: https://rbi-source.harshil.ai/mcp/
    enabled: true
```

#### Zed

In `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "rbi-source": {
      "command": null,
      "settings": {
        "url": "https://rbi-source.harshil.ai/mcp/",
        "type": "http"
      }
    }
  }
}
```

### Verify the connection

After install, run a curl probe to confirm the endpoint is reachable from your network:

```bash
curl -sS -X POST https://rbi-source.harshil.ai/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | head -c 500
```

You should see an SSE event listing the four tools.

### Endpoints

- `GET /` — corpus stats banner (curl-friendly JSON)
- `GET /health` — liveness probe; 200 with `documents` count when healthy, 503 if corpus is empty/missing
- `POST /mcp/` — MCP streamable-HTTP transport (note the trailing slash — clients that don't follow 307s should hit `/mcp/` directly)

Verified end-to-end: **Claude Code** (CLI), **Claude.ai** (Settings → Connectors). The other client snippets follow each tool's documented MCP-config format but haven't been individually smoke-tested — if you hit issues with a specific client, please open an issue with the error output and we'll get it sorted.

> **Tool naming:** as of v0.5.10 (2026-04-29), tools use underscored names (`rbi_search`, `rbi_check_compliance`, etc.) to comply with Claude.ai's and ChatGPT's stricter `^[a-zA-Z0-9_-]{1,64}$` tool-name pattern. Earlier dotted names (`rbi.search`) no longer exist; if your client cached the old schema, refresh the connector.

### Self-host

Don't want the hosted endpoint? You can run your own copy. See [Run your own copy](#run-your-own-copy) below — it's `git clone` + `uv sync` + `rbi-source-index-all`.

## Tools

### `rbi_check_compliance(text, topic_hint?)` — HEADLINE

Paste free text (a clause, PRD section, draft policy paragraph, code comment) and get back ranked relevant provisions with citations: paragraph anchor, official URL, RBI reference, last-updated date, current/withdrawn status.

**Retrieval-only by design.** The MCP does not issue compliance verdicts. The LLM consuming the tool synthesizes the verdict from cited provisions; the user makes the decision. This separation keeps the MCP defensibly on the source-layer side of the line — we are not an unauthorized regulatory advisor.

`topic_hint` (optional) biases retrieval toward a known topic. Supported:
- `payment_aggregator` / `pa` / `pa_pg` — Payment Aggregator MD
- `kyc_bank` / `kyc_nbfc` / `kyc` (spans both)
- `ppi` / `prepaid` / `wallet` — Prepaid Instruments MD
- `cards` / `credit_card` / `debit_card` / `tokenisation` — Commercial Banks Cards MD
- `e_mandate` / `recurring` — E-mandate Framework
- `digital_payment_security` / `afa` — Digital Payment Security Controls

Unknown values are ignored (search spans full corpus). Out-of-scope inputs (recipes, sports articles, anything unrelated) return `low_confidence: true` so consuming LLMs can decline to synthesize.

### `rbi_search(query, filters?)` — direct retrieval

Same hybrid engine `check_compliance` uses, exposed for cleaner keyword queries: "what are the net-worth requirements for PAs". Returns ranked chunks with citations across the whole corpus or filtered by topic.

### `rbi_get_document(document_id, include_text?, as_of?)` — fetch a document

Returns metadata + table of contents (chunk anchors, sections, page numbers) for any document. Pass `include_text=true` to get the full assembled body. `as_of` reserved for v1.1 (when document version history lands).

### `rbi_check_current(url_or_ref)` — safety/utility tool

Paste an RBI URL and learn whether it's current, withdrawn, or out-of-corpus. Useful for verifying a citation. Three-step lookup:
1. Withdrawn-circulars list → returns `withdrawn` with replacement reference
2. Active corpus (any indexed family) → returns `current` with full citation
3. Neither → returns `not_withdrawn` with honest caveat

Supported URL patterns:
- `BS_ViewMasDirections.aspx?id=<MD_ID>`
- `NotificationUser.aspx?Id=<NOTIF_ID>` (checked against withdrawn list + active corpus)

Other inputs return a structured `unsupported_at_v0.1` response. **Never silently fails.**

### Mandatory disclaimer

Every tool response includes a `_disclaimer` field at the top of the JSON object plus an `_llm_instruction` telling the consuming LLM to surface the disclaimer when presenting results to the user. Five required points: (1) not legal advice, (2) retrieval-only — no compliance verdicts, (3) provisions may have been amended/withdrawn since the last corpus refresh, (4) verify with a qualified human compliance reviewer before acting, (5) unofficial — not affiliated with the Reserve Bank of India.

The legal posture is preserved on **error** responses too: if a tool dispatch raises (DB unavailable, embedder OOM, etc.), the wrapped error envelope still carries `_disclaimer` + `_llm_instruction` at the top. Guarded by `tests/test_error_envelope.py`.

## Architecture

```
   Fly machine (rbi-source-mcp, bom region, always-on)        [LIVE]
   ──────────────────────────────────────────────────────────
   shared-cpu-1x, 1 GB RAM, 1 GB volume, single rolling-restart
   /data/db.sqlite       ← FTS5 + sqlite-vec, atomic-swap on refresh
   /data/db-prev.sqlite  ← one-command rollback target [planned]
   custom domain         → rbi-source.harshil.ai (Cloudflare DNS)

   Weekly GitHub Action (Sundays 02:00 UTC)                   [planned]
   ──────────────────────────────────────────────────────────
   crawl 5 families → hash-gate → re-extract changed PDFs
   → embed via bge-small-en-v1.5
   → build new SQLite (FTS5 + chunks_vec virtual table)
   → smoke test (golden 25-query eval, must hit ≥80%)
   → if PASS: scp to Fly → atomic rename → SIGHUP server (~5s blip)
   → if FAIL: abort, alert, live DB untouched
```

See the [/Users/harshil/.gstack/projects/rbimcp/](/Users/harshil/.gstack/projects/rbimcp/) directory (locally, not in repo) for full design history including office-hours, DX review, and eng review.

## Run your own copy

```bash
git clone https://github.com/harshilmathur/RBI-Source-MCP.git
cd RBI-Source-MCP

# Set up Python env
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Crawl the source pages (populates documents + withdrawn metadata)
.venv/bin/rbi-source-crawl

# Index each content family (downloads bge-small ~135 MB on first run; ~1-2 hours total for full crawl)
.venv/bin/rbi-source-index-all                  # 342 Master Directions
.venv/bin/rbi-source-index-all-circulars        # 290 Standalone Circulars
.venv/bin/rbi-source-index-press-release --bulk # 54 Press Releases
.venv/bin/rbi-source-index-faq --bulk           # 98 FAQs
.venv/bin/rbi-source-index-master-circular --bulk  # 19 Master Circulars

# Verify the eval gate
.venv/bin/rbi-source-eval

# Run the MCP server (stdio)
.venv/bin/rbi-source-mcp
```

Or via Docker (when v1.0 image lands): `docker compose up`.

### Register with Claude Code locally (stdio)

```bash
claude mcp add rbi-source \
  -s user \
  -e RBI_SOURCE_DB=$(pwd)/data/db.sqlite \
  -- $(pwd)/.venv/bin/rbi-source-mcp
```

Then `/mcp` to verify connection. The four tools (`rbi_check_compliance`, `rbi_search`, `rbi_get_document`, `rbi_check_current`) become available in any Claude Code session.

### Run as an HTTP server (hosted-mode parity)

For testing the hosted experience locally, or for running on a private VM / Fly:

```bash
RBI_SOURCE_DB=$(pwd)/data/db.sqlite \
  .venv/bin/rbi-source-mcp-http --host 0.0.0.0 --port 8080
```

Endpoints:
- `GET /` — corpus stats banner (curl-friendly)
- `GET /health` — liveness probe (used by Fly + load balancers)
- `POST /mcp/` — MCP streamable-HTTP endpoint

Connect from any MCP client:
```bash
claude mcp add rbi-source --transport http http://your-host:8080/mcp
```

Or from a browser-based client, paste `http://your-host:8080/mcp` into the connector dialog.

## Roadmap

**Done (today's state):**
- ✓ All 342 Master Directions
- ✓ All 290 Standalone Circulars
- ✓ All 98 FAQs
- ✓ 54 most-recent Press Releases
- ✓ All 19 active Master Circulars
- ✓ 9,908 Withdrawn-circular metadata
- ✓ Hybrid retrieval (FTS5 + sqlite-vec + RRF fusion)
- ✓ Mandatory disclaimer on every response (5-point text, error envelopes too)
- ✓ Compound `UNIQUE(md_id, document_family)` schema (5 families coexist safely)
- ✓ 25-case eval gate
- ✓ Streamable-HTTP transport (`rbi-source-mcp-http`)
- ✓ **Hosted endpoint live** at `https://rbi-source.harshil.ai/mcp/` (Fly.io, Mumbai)
- ✓ Custom domain via Cloudflare DNS, Fly Let's-Encrypt cert auto-renewing

**v1.0 (next):**
- Weekly refresh GH Action (configured, not yet running on schedule)
- MCP Registry listing
- Cursor one-click install deeplink
- Public stats page from telemetry
- Press Releases full archive (year-by-year crawl)

**v1.1+:**
- Notifications archive (thousands of docs; needs year-iterating crawler)
- `document_versions` — preserve historical snapshots, enable `compare_versions` and `as_of` queries
- Amendment chain extraction → unblocks `find_updates` and `trace_relationships` tools
- OCR pipeline for scanned MDs
- `/playground` HTML zero-install try-it surface

## Contributing

Open an issue or PR. Contribution areas welcome:
- Parser fixes for any of the 5 content-family list pages
- Topic-hint mapping additions in `mcp/check_compliance.py`
- Eval cases (`src/rbi_source_mcp/eval/cases.py`) — especially clause + expected-provision pairs from real fintech compliance work
- New content families (e.g., RE-wise Draft Directions)

GitHub Discussions enabled for design questions and corpus quality reports.

## License

Apache-2.0. See [LICENSE](LICENSE).

## Disclaimer

This tool surfaces RBI source material; **it does not provide legal advice.** Use citations to inform compliance review, not replace it. RBI publications are subject to amendment without notice; this tool's "Updated as on" and `last_updated_at` timestamps are best-effort. Always verify with a qualified compliance reviewer before acting on any provision returned.
