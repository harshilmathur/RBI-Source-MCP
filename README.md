# RBI Source MCP

[![CI](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Live](https://img.shields.io/badge/live-rbi--source.harshil.ai-c9a85c)](https://rbi-source.harshil.ai/)

> **The entire RBI corpus, version-aware, citation-first, in your AI workflow.**

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that brings authoritative Reserve Bank of India regulatory information into any MCP-capable client (Claude Code, Claude.ai, Claude Desktop, ChatGPT, Cursor, Claude Cowork, Cline, Continue, Goose, Zed, ...) with hybrid retrieval over Master Directions, Standalone Circulars, Master Circulars, Press Releases, and FAQs, citation-first responses, and explicit withdrawal/supersession detection.

**Live hosted endpoint** (free, no auth): `https://rbi-source.harshil.ai/mcp/` · [install in 30 seconds ↓](#quick-start)

> ⚠️ **Unofficial, community-maintained open-source tool.** RBI Source MCP is not affiliated with, endorsed by, or sponsored by the Reserve Bank of India. RBI® is a trademark of the Reserve Bank of India. For takedown or legal inquiries, open an issue.

## Why

Fintech builders writing a clause, PRD section, or partner agreement currently ping the compliance team in Slack and wait hours-to-days to know if their language conflicts with a current RBI rule. Or they Google `rbi.org.in`, land on an ASP.NET page that may or may not be a withdrawn circular, and ship against rules superseded three years ago.

RBI Source MCP collapses that loop. Paste a clause into Claude/Cursor with this MCP connected; the LLM gets back ranked relevant RBI provisions with paragraph anchors, official URLs, RBI references, and current/withdrawn status. **Retrieval-only by design** — the MCP doesn't issue compliance verdicts; the LLM synthesizes from the cited material; the user decides; a qualified human compliance reviewer signs off before anyone acts.

## Coverage

| Content family | Docs | Chunks | Source page |
|---|---|---|---|
| **Master Directions** | 342 | 50,088 | `BS_ViewMasDirections.aspx` |
| **Standalone Circulars** | 290 | 1,532 | `BS_ViewListofstandalonecirculars.aspx` |
| **FAQs** | 98 | 1,828 | `FAQView.aspx` |
| **Press Releases** | 54 | 357 | `BS_PressReleaseDisplay.aspx` |
| **Master Circulars** | 19 | 2,848 | `BS_ViewMasterCirculardetails.aspx` |
| **TOTAL** | **803** | **56,653** | |

Plus **9,908 withdrawn-circular records** indexed for `rbi_check_current` lookup (metadata only, no full-text indexing).

Hybrid retrieval = FTS5 (BM25 sparse) + sqlite-vec (`bge-small-en-v1.5` 384-dim dense) fused via Reciprocal Rank Fusion (k=60). Every response carries a mandatory legal disclaimer. Eval gate: 25 hand-labeled compliance cases, must pass at ≥80%; currently 100%.

## Quick start

The hosted endpoint is live and free to use:

```
https://rbi-source.harshil.ai/mcp/
```

Public, unauthenticated, retrieval-only. Single Fly machine in Mumbai. Every response carries the mandatory legal disclaimer.

> Try it without installing anything:
> ```bash
> curl -sS https://rbi-source.harshil.ai/health
> # → {"version":"...","status":"ok","documents":803}
> ```

### Connect from your client

| Client | How |
|---|---|
| **Claude Code** (CLI) | `claude mcp add rbi-source --transport http https://rbi-source.harshil.ai/mcp/` |
| **Claude.ai / Claude Desktop** | Settings → **Connectors** → **Add custom connector** → paste the URL. Requires a paid plan (Pro / Team / Enterprise). |
| **ChatGPT** | Settings → **Connectors** → **Create custom connector** → paste the URL. Requires a paid plan (Plus / Pro / Team / Enterprise). |
| **Cursor** | Settings → **MCP** → **Add new MCP server** → type `streamable-http`, URL: `https://rbi-source.harshil.ai/mcp/` |
| **Claude Cowork** | Settings → **Connectors** → **Add custom connector** → paste the URL. |
| **Cline** (VSCode) | `~/.cline/mcp_settings.json`, see [snippet](#cline-vscode) |
| **Continue.dev** | `~/.continue/config.json`, see [snippet](#continuedev) |
| **Goose** (Block) | `~/.config/goose/config.yaml`, see [snippet](#goose) |
| **Zed** | `~/.config/zed/settings.json`, see [snippet](#zed) |
| **Anything else** that speaks MCP streamable-HTTP | Point it at `https://rbi-source.harshil.ai/mcp/` |

After connecting, four tools (`rbi_check_compliance`, `rbi_search`, `rbi_get_document`, `rbi_check_current`) become available. Try:

> *"Use the RBI Source MCP. What are the net-worth requirements for a payment aggregator?"*

### Config snippets

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

```bash
curl -sS -X POST https://rbi-source.harshil.ai/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | head -c 500
```

You should see an SSE event listing the four tools.

### Endpoints

- `GET /` — homepage (HTML for browsers, JSON corpus banner for curl/MCP clients via Accept-header detection)
- `GET /health` — liveness probe; 200 with `documents` count when healthy, 503 if corpus is empty/missing
- `POST /mcp/` — MCP streamable-HTTP transport (note the trailing slash; clients that don't follow 307s should hit `/mcp/` directly)

Verified end-to-end on **Claude Code** (CLI) and **Claude.ai** (Settings → Connectors). The other client snippets follow each tool's documented MCP-config format but haven't been individually smoke-tested — if you hit issues with a specific client, please open an issue with the error output.

## Tools

### `rbi_check_compliance(text, topic_hint?, limit?)` — headline

Paste free text (a clause, PRD section, draft policy paragraph, code comment); get back ranked relevant provisions with citations: paragraph anchor, official URL, RBI reference, last-updated date, current/withdrawn status.

**Retrieval-only by design.** The MCP returns the cited material; the LLM consuming it produces any summary or conclusion; the user verifies with a qualified human compliance reviewer. The tool does not issue compliance verdicts.

`topic_hint` (optional) biases retrieval toward a known topic. Supported:

- `payment_aggregator` / `pa` / `pa_pg` — Payment Aggregator MD
- `kyc_bank` / `kyc_nbfc` / `kyc` (spans both)
- `ppi` / `prepaid` / `wallet` — Prepaid Instruments MD
- `cards` / `credit_card` / `debit_card` / `tokenisation` — Commercial Banks Cards MD
- `e_mandate` / `recurring` — E-mandate Framework
- `digital_payment_security` / `afa` — Digital Payment Security Controls

Unknown values are ignored (search spans full corpus). Out-of-scope inputs (recipes, sports articles, anything unrelated) return `low_confidence: true` so consuming LLMs can decline to synthesize.

### `rbi_search(query, filters?, limit?)` — direct retrieval

Same hybrid engine as `rbi_check_compliance`, exposed for cleaner keyword queries: *"what are the net-worth requirements for PAs"*. Returns ranked chunks with citations across the whole corpus or filtered by topic.

### `rbi_get_document(document_id, include_text?, as_of?)` — fetch a document

Returns metadata + table of contents (chunk anchors, sections, page numbers) for any document. Pass `include_text=true` to get the full assembled body. `as_of` is reserved for v1.1 (when document version history lands).

### `rbi_check_current(url_or_ref)` — safety / utility tool

Paste an RBI URL; learn whether it's current, withdrawn, or out-of-corpus. Useful for verifying a citation. Three-step lookup:

1. Withdrawn-circulars list → returns `withdrawn` with replacement reference
2. Active corpus (any indexed family) → returns `current` with full citation
3. Neither → returns `not_withdrawn` with honest caveat

Supported URL patterns:

- `BS_ViewMasDirections.aspx?id=<MD_ID>`
- `NotificationUser.aspx?Id=<NOTIF_ID>` (checked against withdrawn list + active corpus)

Other inputs return a structured `unsupported_at_v0.1` response. Never silently fails.

### Mandatory disclaimer

Every tool response includes a `_disclaimer` field at the top of the JSON object plus an `_llm_instruction` telling the consuming LLM to surface the disclaimer. Five required points: (1) not legal advice, (2) retrieval-only — no compliance verdicts, (3) provisions may have been amended/withdrawn since the last corpus refresh, (4) verify with a qualified human compliance reviewer before acting, (5) unofficial — not affiliated with the Reserve Bank of India.

The legal posture is preserved on **error** responses too: if a tool dispatch raises (DB unavailable, embedder OOM, etc.), the wrapped error envelope still carries `_disclaimer` + `_llm_instruction` at the top. Guarded by `tests/test_error_envelope.py`.

## Architecture

```
   Fly machine (rbi-source-mcp, bom region, always-on)
   ────────────────────────────────────────────────────
   shared-cpu-1x, 2 GB RAM, 1 GB volume, single rolling-restart
   /data/db.sqlite       FTS5 + sqlite-vec, atomic-swap on refresh
   /data/db-prev.sqlite  one-command rollback target [planned]
   custom domain         rbi-source.harshil.ai (Cloudflare DNS)

   Public surface
   ──────────────
   GET  /             homepage HTML (browsers) / corpus banner JSON (clients)
   GET  /health       deep liveness probe (counts docs, re-checks sqlite-vec)
   POST /mcp/         MCP streamable-HTTP transport
   *    OAuth 2.1     RFC 8414, 9728, 7591 ceremonial endpoints

   Weekly GitHub Action (Sundays 02:00 UTC)              [planned]
   ───────────────────────────────────────────────────────────────
   crawl 5 families → hash-gate → re-extract changed PDFs
   → embed via bge-small-en-v1.5
   → build new SQLite (FTS5 + chunks_vec virtual table)
   → smoke test (golden 25-query eval, must hit ≥80%)
   → if PASS: scp to Fly → atomic rename → SIGHUP server (~5s blip)
   → if FAIL: abort, alert, live DB untouched
```

## Self-host

Don't want the hosted endpoint? Fork the repo and run your own copy.

```bash
git clone https://github.com/harshilmathur/RBI-Source-MCP.git
cd RBI-Source-MCP

# Install (uv is the project's package manager: https://docs.astral.sh/uv/)
uv sync

# Crawl + index — populates the local corpus.
# ~1-2 hours on first run; downloads bge-small-en-v1.5 (~135 MB) on the
# first index command, cached locally afterwards.
uv run rbi-source-crawl                            # documents + withdrawn metadata
uv run rbi-source-index-all                        # 342 Master Directions
uv run rbi-source-index-all-circulars              # 290 Standalone Circulars
uv run rbi-source-index-press-release --bulk       # 54 Press Releases
uv run rbi-source-index-faq --bulk                 # 98 FAQs
uv run rbi-source-index-master-circular --bulk     # 19 Master Circulars

# Verify the eval gate before serving
uv run rbi-source-eval                             # must pass at ≥80%

# Run the MCP server over stdio (for local Claude Code / Claude Desktop)
uv run rbi-source-mcp

# Or the streamable-HTTP server (hosted-mode parity, for VPS / Fly / etc.)
uv run rbi-source-mcp-http --host 0.0.0.0 --port 8080
```

### Register a self-host with Claude Code (stdio)

```bash
claude mcp add rbi-source \
  -s user \
  -e RBI_SOURCE_DB=$(pwd)/data/db.sqlite \
  -- $(pwd)/.venv/bin/rbi-source-mcp
```

### Docker

```bash
docker compose run --rm crawl --profile ops      # one-shot crawl + index into the volume
docker compose up                                # serve HTTP on :8080
```

The Compose setup uses a named `rbi_data` volume; first run will be slow as the indexers populate it. After that, `docker compose up` boots in seconds.

### Fly.io

`fly.toml` is committed and ready. Fork the repo, then:

```bash
fly launch --copy-config       # prompt for a new app name; everything else is sane defaults
fly volumes create rbi_data --region <your-region> --size 1
# populate the volume by running the indexers in the container, or
# scp a pre-built db.sqlite to /data/db.sqlite
fly deploy
```

## Roadmap

**Done:**

- All 342 Master Directions, 290 Standalone Circulars, 98 FAQs, 54 Press Releases, 19 Master Circulars
- 9,908 withdrawn-circular metadata records
- Hybrid retrieval (FTS5 + sqlite-vec + RRF fusion)
- Mandatory disclaimer on every response (5-point text, error envelopes too)
- Compound `UNIQUE(md_id, document_family)` schema (5 families coexist safely)
- 25-case eval gate
- Stdio + Streamable-HTTP transports (`rbi-source-mcp`, `rbi-source-mcp-http`)
- Hosted endpoint live at `https://rbi-source.harshil.ai/mcp/`
- Custom domain via Cloudflare DNS, Fly Let's-Encrypt cert auto-renewing
- OAuth 2.1 ceremonial endpoints (Claude.ai connector compatibility)
- Per-IP rate limiting, body-size cap, non-root container, async embedder
- HTML homepage (browsers) with Accept-header content negotiation

**v1.0 (next):**

- Weekly refresh GitHub Action (configured, not yet running on schedule)
- MCP Registry listing
- Cursor one-click install deeplink
- Press Releases full archive (year-by-year crawl)

**v1.1+:**

- Notifications archive (thousands of docs; needs year-iterating crawler)
- `document_versions` — preserve historical snapshots, enable `compare_versions` and `as_of` queries
- Amendment chain extraction → unblocks `find_updates` and `trace_relationships` tools
- OCR pipeline for scanned MDs

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Short version:

- Eval cases (`src/rbi_source_mcp/eval/cases.py`) — clause + expected-provision pairs from real fintech compliance work — are the highest-leverage contribution
- Corpus quality reports — file an issue with the "corpus quality" template, include the verbatim tool input + response
- Parser fixes for any of the five content-family list pages
- Topic-hint mapping additions in `mcp/check_compliance.py`
- New content families (open an issue first to align on scope)

## Security

See [SECURITY.md](SECURITY.md). Don't open public issues for vulnerabilities — use a [private security advisory](https://github.com/harshilmathur/RBI-Source-MCP/security/advisories/new).

## License

Apache-2.0. See [LICENSE](LICENSE).

## Disclaimer

This tool surfaces RBI source material; **it does not provide legal advice.** Use citations to inform compliance review, not replace it. RBI publications are subject to amendment without notice; this tool's `last_updated_at` timestamps are best-effort. Always verify with a qualified compliance reviewer before acting on any provision returned.
