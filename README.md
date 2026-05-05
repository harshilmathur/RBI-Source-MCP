# RBI Source MCP

[![PyPI](https://img.shields.io/pypi/v/rbi-source-mcp.svg)](https://pypi.org/project/rbi-source-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/rbi-source-mcp.svg)](https://pypi.org/project/rbi-source-mcp/)
[![License](https://img.shields.io/pypi/l/rbi-source-mcp.svg)](LICENSE)
[![CI](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/ci.yml)
[![Weekly corpus build](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/corpus-release.yml/badge.svg)](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/corpus-release.yml)
[![Hosted demo](https://img.shields.io/badge/hosted-rbi--source.harshil.ai-c9a85c)](https://rbi-source.harshil.ai/)

> **The entire RBI corpus, version-aware, citation-first, in your AI workflow.**

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that brings authoritative Reserve Bank of India regulatory information into any MCP-capable client (Claude Code, Claude.ai, Claude Desktop, ChatGPT, Cursor, Claude Cowork, Cline, Continue, Goose, Zed, ...) with hybrid retrieval over Master Directions, Standalone Circulars, Master Circulars, Press Releases, and FAQs, citation-first responses, and explicit withdrawal/supersession detection.

Two ways to use it:

- **Try the hosted demo:** `https://rbi-source.harshil.ai/mcp/` (free, no auth, no install) · [client setup ↓](#connect-from-your-client)
- **Self-host in 3 lines:** `pip install rbi-source-mcp` + `rbi-source-fetch-corpus` + Claude Desktop config · [details ↓](#self-host)

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

Two paths. Pick one.

### A. Use the hosted demo (zero install)

The maintainer runs a free, no-auth instance:

```
https://rbi-source.harshil.ai/mcp/
```

Public, unauthenticated, retrieval-only. Hosted in Mumbai. Every response carries the mandatory legal disclaimer. Good for evaluation; rate-limited to 60 req/min/IP.

```bash
curl -sS https://rbi-source.harshil.ai/health
# → {"version":"0.8.x","status":"ok","documents":803}
```

### B. Self-host (3 lines, runs locally)

```bash
pip install rbi-source-mcp                       # or: uv tool install / pipx install
rbi-source-fetch-corpus                          # downloads ~80MB sigstore-signed corpus
# Add to claude_desktop_config.json: {"mcpServers": {"rbi-source": {"command": "rbi-source-mcp"}}}
```

Defaults to the local `bge-base-en-v1.5` embedder (no API keys). [Full self-host guide ↓](#self-host)

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

<details>
<summary><b>Config snippets for Cline / Continue / Goose / Zed</b></summary>

**Cline** (VSCode) — in MCP Settings:
```json
{ "mcpServers": { "rbi-source": { "type": "streamableHttp", "url": "https://rbi-source.harshil.ai/mcp/" } } }
```

**Continue.dev** — in `~/.continue/config.json`:
```json
{ "experimental": { "modelContextProtocolServers": [ { "transport": { "type": "streamable-http", "url": "https://rbi-source.harshil.ai/mcp/" } } ] } }
```

**Goose** — in `~/.config/goose/config.yaml`:
```yaml
extensions:
  rbi-source:
    type: streamable_http
    uri: https://rbi-source.harshil.ai/mcp/
    enabled: true
```

**Zed** — in `~/.config/zed/settings.json`:
```json
{ "context_servers": { "rbi-source": { "command": null, "settings": { "url": "https://rbi-source.harshil.ai/mcp/", "type": "http" } } } }
```

</details>

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

`topic_hint` (optional) biases retrieval to one MD: `pa` / `kyc` / `ppi` / `cards` / `e_mandate` / `afa` (full alias list in the tool's input schema). Unknown values are ignored — search spans the full corpus. Out-of-scope inputs (recipes, sports articles, anything unrelated) return `low_confidence: true` so consuming LLMs can decline to synthesize.

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
   Runtime (single-process Python, any Linux/macOS host)
   ──────────────────────────────────────────────────────
   ./data/db.sqlite      FTS5 + sqlite-vec, atomic-swap on refresh
   ./data/db-prev.sqlite one-command rollback target

   Default embedding path: bge-base-en-v1.5 in-process via
   sentence-transformers (~440 MB model on disk, ~3-8s cold load,
   ~80-120ms per query on CPU). With a 10K-entry process-local LRU
   cache, repeat queries skip the cost entirely.

   Optional Cloudflare Workers AI path: same model, ~250ms/call over
   HTTPS, no torch on the box. Picked via
   RBI_EMBEDDING_PROVIDER=cloudflare.

   Both paths emit 768-dim vectors → ONE corpus, ONE schema, no
   client-side dim juggling.

   HTTP-mode public surface (rbi-source-mcp-http)
   ──────────────────────────────────────────────
   GET  /             homepage HTML (browsers) / corpus banner JSON (clients)
   GET  /health       deep liveness probe (counts docs, re-checks
                      sqlite-vec, surfaces query_cache hit/miss stats)
   POST /mcp/         MCP streamable-HTTP transport
   *    OAuth 2.1     RFC 8414, 9728, 7591 ceremonial endpoints

   Weekly corpus build (GitHub Actions, Sundays 02:00 UTC)
   ────────────────────────────────────────────────────────
   crawl 5 families → diff via content-hash → re-extract changed PDFs
   → embed (CF Workers AI in CI; bge-base @ 768-dim)
   → build new SQLite (FTS5 + chunks_vec virtual table)
   → smoke gate (rbi-source-eval, ≥80% absolute AND <5pp regression)
   → publish corpus.sqlite.xz as a GitHub Release asset (sigstore-signed)
```

## Self-host

Three lines, no Docker, no Cloudflare account, no crawling.

```bash
# 1. Install (persistent — stays available across Claude Desktop restarts)
uv tool install rbi-source-mcp                  # or: pipx install rbi-source-mcp

# 2. Get the prebuilt corpus (~80 MB compressed, refreshed weekly via GitHub Actions)
rbi-source-fetch-corpus                         # → ~/.local/share/rbi-source-mcp/db.sqlite

# 3. Wire to Claude Desktop (claude_desktop_config.json) or Claude Code:
#    {
#      "mcpServers": {
#        "rbi-source": { "command": "rbi-source-mcp" }
#      }
#    }
```

That's it. The default embedding path runs `bge-base-en-v1.5` in-process via `sentence-transformers` (~440 MB model auto-downloaded on first query, cached at `~/.cache/huggingface/hub/`). No external API needed. CPU-only — works on a laptop.

> Use `uv tool install` (or `pipx install`) instead of `uvx rbi-source-mcp` — `uvx` is a one-shot ephemeral runner; Claude Desktop restarting the server every time would re-resolve deps and re-load the model. `uv tool install` keeps a persistent venv with the model cached.

### Verify the install

```bash
rbi-source-doctor                               # runs preflight: Python, sqlite-vec, corpus, HF cache, model
```

Should print all-green. If anything fails, the output tells you the specific fix.

### Register with Claude Code (stdio)

```bash
claude mcp add rbi-source -s user -- rbi-source-mcp
```

### Pin a corpus version (optional)

`rbi-source-fetch-corpus` defaults to the moving `latest-corpus` release. For reproducible deploys, pin to an immutable timestamped tag:

```bash
rbi-source-fetch-corpus --tag corpus-2026-05-04-1234567
```

Browse available tags at [github.com/harshilmathur/RBI-Source-MCP/releases](https://github.com/harshilmathur/RBI-Source-MCP/releases).

### Cryptographic verification (optional)

By default the fetch script verifies SHA256 (mandatory). For full sigstore signature verification against the GitHub Actions OIDC identity:

```bash
pip install 'rbi-source-mcp[verify]'            # pulls sigstore-python (~25 MB)
rbi-source-fetch-corpus --verify-sigstore
```

This refuses to install any corpus that wasn't signed by the official `corpus-release.yml` workflow on `main`. Defends against PyPI compromise + tampered release assets.

### Cloudflare Workers AI (alternative embedder)

If you'd rather embed via the Cloudflare API instead of running torch locally:

```bash
export RBI_EMBEDDING_PROVIDER=cloudflare
export RBI_EMBEDDING_MODEL=@cf/baai/bge-base-en-v1.5
export RBI_EMBEDDING_DIM=768
export CF_ACCOUNT_ID=...                        # https://dash.cloudflare.com → Account ID
export CF_API_TOKEN=...                         # https://dash.cloudflare.com/profile/api-tokens → "Workers AI: Read"
```

Both providers emit 768-dim vectors against the same `bge-base-en-v1.5` model (BAAI weights for local; Cloudflare-hosted variant for the API). The prebuilt corpus matches; runtime + corpus mismatches are caught by `rbi-source-doctor`'s `corpus_runtime_match` check before any retrieval happens.

### Build the corpus yourself (advanced)

If you want fresher data than the weekly release, or want to fork the indexer:

```bash
git clone https://github.com/harshilmathur/RBI-Source-MCP.git
cd RBI-Source-MCP
uv sync
uv run rbi-source-crawl                         # crawl list pages
uv run rbi-source-index-all                     # index Master Directions
uv run rbi-source-index-all-circulars           # Standalone Circulars
uv run rbi-source-index-press-release --bulk    # Press Releases
uv run rbi-source-index-faq --bulk              # FAQs
uv run rbi-source-index-master-circular --bulk  # Master Circulars
uv run rbi-source-eval                          # gate at ≥80%
```

Crawl + index takes ~30-60 min. The same pipeline runs weekly in [`.github/workflows/corpus-release.yml`](.github/workflows/corpus-release.yml) and publishes the result as a GitHub Release.

### Run as an HTTP server (advanced)

For an internal team behind a reverse proxy:

```bash
rbi-source-mcp-http --host 0.0.0.0 --port 8080
```

### Weekly corpus refresh

The workflow at `.github/workflows/corpus-release.yml` runs every Sunday: crawl + index + smoke gate (≥80% absolute, <5pp regression) + sigstore-sign + publish to [Releases](https://github.com/harshilmathur/RBI-Source-MCP/releases). Self-hosters get fresh data by re-running `rbi-source-fetch-corpus` whenever they want.

## Roadmap

Shipped surface is in [CHANGELOG.md](CHANGELOG.md). What's next:

**v0.9 (next release):** Press Releases full archive (5y year-by-year crawl) · RBI Speeches crawler · pin GitHub Actions to commit SHAs · MCP Registry listing

**v1.0+:** Notifications archive (thousands of docs; year-iterating crawler) · `document_versions` historical snapshots → enable `compare_versions` and `as_of` queries · amendment chain extraction → unblocks `find_updates` and `trace_relationships` tools · OCR pipeline for scanned MDs · low-confidence threshold tuned with a fusion-score floor

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Short version:

- Eval cases (`src/rbi_source_mcp/eval/cases.py`) — clause + expected-provision pairs from real fintech compliance work — are the highest-leverage contribution
- Corpus quality reports — file an issue with the "corpus quality" template, include the verbatim tool input + response
- Parser fixes for any of the five content-family list pages
- Topic-hint mapping additions in `mcp/check_compliance.py`
- New content families (open an issue first to align on scope)

## Security

See [SECURITY.md](SECURITY.md). Don't open public issues for vulnerabilities — use a [private security advisory](https://github.com/harshilmathur/RBI-Source-MCP/security/advisories/new).

## Telemetry

The hosted instance at `rbi-source.harshil.ai` captures anonymous server-side usage events to PostHog so I can see which tools get called, latency, and error rate. **Self-host installs never phone home** — telemetry only activates when `POSTHOG_API_KEY` is set in the environment, which it isn't in any path the OSS code ships with.

What the hosted instance captures per tool call:

- tool name (`rbi_search` / `rbi_check_compliance` / `rbi_get_document` / `rbi_check_current`)
- latency (ms), status (`ok` / `error` / `input_too_large` / `db_unavailable`)
- shape-only metadata: `limit`, `has_filters`, `include_text`, `topic_hint` enum value, length-bucket of the input (e.g. `"500-2000"`)
- exception class name on failure, server version, region (when `MCP_REGION` or `FLY_REGION` is set)

What it does **not** capture: query strings, clause text, document IDs, response bodies, URLs passed to `rbi_check_current`, IP, or any client identifier. Events are anonymous (`$process_person_profile: false`) and `disable_geoip=True` is set on the SDK.

The website uses Cloudflare Web Analytics (cookieless, no consent banner needed).

To self-host with your own collector: `pip install 'rbi-source-mcp[hosted]'` (pulls the PostHog SDK) and set `POSTHOG_API_KEY` + optional `POSTHOG_HOST`. Leave them unset and you can `pip install rbi-source-mcp` without the extra — the default for every install.

## License

MIT. See [LICENSE](LICENSE).

## Disclaimer

This tool surfaces RBI source material; **it does not provide legal advice.** Use citations to inform compliance review, not replace it. RBI publications are subject to amendment without notice; this tool's `last_updated_at` timestamps are best-effort. Always verify with a qualified compliance reviewer before acting on any provision returned.
