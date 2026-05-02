# RBI Source MCP

[![CI](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
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
| **Master Directions** | ~340 | ~50k | `BS_ViewMasDirections.aspx` |
| **Standalone Circulars** | ~290 | ~1.5k | `BS_ViewListofstandalonecirculars.aspx` |
| **FAQs** | ~100 | ~1.8k | `FAQView.aspx` |
| **Press Releases** | ~55 | ~360 | `BS_PressReleaseDisplay.aspx` |
| **Master Circulars** | ~20 | ~2.8k | `BS_ViewMasterCirculardetails.aspx` |
| **TOTAL** | **~810 docs** | **~57k chunks** | |

Plus **~10k withdrawn-circular records** indexed for `rbi_check_current` lookup (metadata only, no full-text indexing). Live counts grow each Sunday — see `GET /health` on the running server for the current numbers.

Hybrid retrieval = FTS5 (BM25 sparse) + sqlite-vec (`bge-base-en-v1.5` 768-dim dense, embedded via Cloudflare Workers AI on the hosted endpoint) fused via Reciprocal Rank Fusion (k=60). Every response carries a mandatory legal disclaimer. Eval gate: ~25 hand-labeled compliance cases, must pass at ≥80%; currently 100%.

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
   shared-cpu-1x, 1 GB RAM, 1 GB volume, single rolling-restart
   /data/db.sqlite       FTS5 + sqlite-vec, atomic-swap on refresh
   /data/db-prev.sqlite  one-command rollback target
   custom domain         rbi-source.harshil.ai (Cloudflare DNS)

   Image: 89 MB (no torch, no resident model). Cold-boot embedder
   prewarm: ~1s. Query embedding via Cloudflare Workers AI over HTTPS
   (~250ms/call), with a 10K-entry process-local LRU cache so repeat
   queries skip the round-trip entirely.

   Public surface
   ──────────────
   GET  /             homepage HTML (browsers) / corpus banner JSON (clients)
   GET  /health       deep liveness probe (counts docs, re-checks
                      sqlite-vec, surfaces query_cache hit/miss stats)
   POST /mcp/         MCP streamable-HTTP transport
   *    OAuth 2.1     RFC 8414, 9728, 7591 ceremonial endpoints

   Weekly GitHub Action (Sundays 02:00 UTC)
   ─────────────────────────────────────────
   crawl 5 families → hash-gate → re-extract changed PDFs
   → embed via Cloudflare Workers AI (`@cf/baai/bge-base-en-v1.5`, 768-dim)
   → build new SQLite (FTS5 + chunks_vec virtual table)
   → smoke test (rbi-source-eval, must hit ≥80%)
   → if PASS: sftp to Fly volume → size-verify → atomic rename
              (server keeps old fd open; new connections see fresh DB)
   → if FAIL: abort, upload artifact for inspection, live DB untouched
```

## Self-host

Don't want the hosted endpoint? Fork the repo and run your own copy. You have two paths for embeddings, controlled by env vars (see `src/rbi_source_mcp/embedding_config.py`):

- **Cloud (default for production)**: Cloudflare Workers AI. Free tier covers a weekly 56k-chunk corpus build + thousands of queries. Runtime image stays small (~90 MB), no torch.
- **Local**: `sentence-transformers` (bge-small-en-v1.5 @ 384-dim) running in-process. Pulls torch (~3 GB install) and downloads the model (~135 MB) on first use. No external API calls.

```bash
git clone https://github.com/harshilmathur/RBI-Source-MCP.git
cd RBI-Source-MCP

# === Option A — local embeddings (no API keys needed) ===
uv sync --extra local-embeddings

# === Option B — Cloudflare Workers AI ===
uv sync                                              # core deps only
export RBI_EMBEDDING_PROVIDER=cloudflare
export RBI_EMBEDDING_MODEL=@cf/baai/bge-base-en-v1.5
export RBI_EMBEDDING_DIM=768
export CF_ACCOUNT_ID=...                             # https://dash.cloudflare.com → Account ID
export CF_API_TOKEN=...                              # https://dash.cloudflare.com/profile/api-tokens → "Workers AI: Read"
# (or write {"account_id":..., "api_token":...} to ~/.gstack/cloudflare.json)

# Crawl + index — populates the local corpus.
# Local path: ~1-2 hours on first run (CPU-bound).
# CF path:    ~30-45 min for crawl + extract; embedding is API-fast (~110 chunks/sec).
uv run rbi-source-crawl                            # documents + withdrawn metadata
uv run rbi-source-index-all                        # Master Directions
uv run rbi-source-index-all-circulars              # Standalone Circulars
uv run rbi-source-index-press-release --bulk      # Press Releases
uv run rbi-source-index-faq --bulk                # FAQs
uv run rbi-source-index-master-circular --bulk    # Master Circulars

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

MIT. See [LICENSE](LICENSE).

## Disclaimer

This tool surfaces RBI source material; **it does not provide legal advice.** Use citations to inform compliance review, not replace it. RBI publications are subject to amendment without notice; this tool's `last_updated_at` timestamps are best-effort. Always verify with a qualified compliance reviewer before acting on any provision returned.
