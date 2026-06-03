# RBI Source MCP

[![PyPI](https://img.shields.io/pypi/v/rbi-source-mcp.svg)](https://pypi.org/project/rbi-source-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/rbi-source-mcp.svg)](https://pypi.org/project/rbi-source-mcp/)
[![License](https://img.shields.io/pypi/l/rbi-source-mcp.svg)](LICENSE)
[![CI](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/ci.yml)
[![Daily corpus build](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/corpus-release.yml/badge.svg)](https://github.com/harshilmathur/RBI-Source-MCP/actions/workflows/corpus-release.yml)

> **The entire RBI corpus, version-aware, citation-first, in your AI workflow.**

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that brings authoritative Reserve Bank of India regulatory information into any MCP-capable client (Claude Code, Claude.ai, Claude Desktop, ChatGPT, Cursor, Cline, Continue, Goose, Zed, …) with hybrid retrieval over Master Directions, Standalone Circulars, Master Circulars, Press Releases, and FAQs, citation-first responses, and explicit withdrawal/supersession detection.

**Fastest start — no install.** Point any MCP client at the free, no-auth hosted instance:

```
https://rbi-source.harshil.ai/mcp/
```

For example, in Claude Code:

```bash
claude mcp add rbi-source --transport http https://rbi-source.harshil.ai/mcp/
```

Per-client setup (Claude.ai, ChatGPT, Cursor, …) is in [Connect to your MCP client](#connect-to-your-mcp-client).

Prefer to run it yourself — offline, fully self-hosted, or as a Python library? [Run locally](#run-locally-self-host):

```bash
pip install rbi-source-mcp        # or: uv tool install rbi-source-mcp
rbi-source-fetch-corpus           # ~80 MB sigstore-signed corpus
# Add to your MCP client: {"mcpServers": {"rbi-source": {"command": "rbi-source-mcp"}}}
```

> ⚠️ **Unofficial, community-maintained.** Not affiliated with, endorsed by, or sponsored by the Reserve Bank of India. RBI® is a trademark of the Reserve Bank of India. For takedown or legal inquiries, open an issue.

## Why

Fintech builders writing a clause, PRD section, or partner agreement currently ping the compliance team in Slack and wait hours-to-days to know if their language conflicts with a current RBI rule. Or they Google `rbi.org.in`, land on an ASP.NET page that may or may not be a withdrawn circular, and ship against rules superseded three years ago.

RBI Source MCP collapses that loop. Paste a clause into Claude/Cursor with this MCP connected; the LLM gets back ranked relevant RBI provisions with paragraph anchors, official URLs, RBI references, and current/withdrawn status. **Retrieval-only by design** — the MCP returns the cited material; the LLM synthesizes; the user decides; a qualified human compliance reviewer signs off before anyone acts.

## Coverage

| Content family | Docs | Chunks | Source page |
|---|---|---|---|
| **Master Directions** | ~340 | ~50k | `BS_ViewMasDirections.aspx` |
| **Standalone Circulars** | ~290 | ~1.5k | `BS_ViewListofstandalonecirculars.aspx` |
| **FAQs** | ~100 | ~1.8k | `FAQView.aspx` |
| **Press Releases** | ~55 | ~360 | `BS_PressReleaseDisplay.aspx` |
| **Master Circulars** | ~20 | ~2.8k | `BS_ViewMasterCirculardetails.aspx` |
| **TOTAL** | **~810 docs** | **~57k chunks** | |

Plus **~10k withdrawn-circular records** indexed for `rbi_check_current` lookups (metadata only, no full-text indexing). Live counts refresh daily — `GET /health` reports the current numbers.

Hybrid retrieval = FTS5 (BM25 sparse) + sqlite-vec (`bge-base-en-v1.5` 768-dim dense) fused via Reciprocal Rank Fusion (k=60). Every response carries a mandatory legal disclaimer. Eval gate: ~25 hand-labeled compliance cases at ≥80%, currently 100%.

## Run locally (self-host)

Most users should just point their client at the hosted URL above. Run locally when you want it offline, with no dependency on the hosted endpoint, on your own infrastructure, or embedded as a Python library:

```bash
uv tool install rbi-source-mcp                 # or: pipx install / pip install
rbi-source-fetch-corpus                        # → ~/.local/share/rbi-source-mcp/db.sqlite
rbi-source-doctor                              # preflight: Python, sqlite-vec, corpus, model
```

That's the full install. The default embedding path runs `bge-base-en-v1.5` in-process via `sentence-transformers` (~440 MB model auto-downloaded on first query, cached at `~/.cache/huggingface/hub/`). No external API needed, CPU-only, works on a laptop.

> Use `uv tool install` (or `pipx install`) instead of `uvx rbi-source-mcp` — `uvx` is one-shot ephemeral; Claude Desktop restarting the server would re-resolve deps and re-load the model. `uv tool install` keeps a persistent venv with the model cached.

## Connect to your MCP client

The hosted endpoint works with any MCP client — no install, just the URL `https://rbi-source.harshil.ai/mcp/`. Self-hosters swap in the local stdio binary (the **Run locally** column; requires the [local install](#run-locally-self-host) above).

| Client | Hosted (no install) | Self-host (stdio) |
|---|---|---|
| **Claude Code** | `claude mcp add rbi-source --transport http https://rbi-source.harshil.ai/mcp/` | `claude mcp add rbi-source -s user -- rbi-source-mcp` |
| **Claude.ai / ChatGPT** | Paid plan → Settings → Connectors → paste `https://rbi-source.harshil.ai/mcp/` | — (remote transport only) |
| **Claude Desktop** | Paid plan → Settings → Connectors → paste `https://rbi-source.harshil.ai/mcp/` | `claude_desktop_config.json` → `{"mcpServers": {"rbi-source": {"command": "rbi-source-mcp"}}}` |
| **Cursor** | Settings → MCP → Add new → `streamable-http` → `https://rbi-source.harshil.ai/mcp/` | `{"command": "rbi-source-mcp"}` in `mcp.json` |
| Cline, Continue, Goose, Zed | See [CONNECT.md](docs/CONNECT.md) | See [CONNECT.md](docs/CONNECT.md) |

After connecting, four tools become available. Try: *"Use the RBI Source MCP. What are the net-worth requirements for a payment aggregator?"*

## Tools

### `rbi_check_compliance(text, topic_hint?, limit?)` — headline

Paste free text (a clause, PRD section, draft policy paragraph, code comment); get back ranked relevant provisions with citations: paragraph anchor, official URL, RBI reference, last-updated date, current/withdrawn status.

**Retrieval-only by design.** Returns cited material; the LLM synthesizes; the user verifies with a qualified human compliance reviewer. The tool does not issue compliance verdicts.

`topic_hint` (optional) biases retrieval to one MD: `pa` / `kyc` / `ppi` / `cards` / `e_mandate` / `afa` (full alias list in the tool's input schema). Unknown values are ignored. Out-of-scope inputs return `low_confidence: true` so consuming LLMs can decline to synthesize.

### `rbi_search(query, filters?, limit?)` — direct retrieval

Same hybrid engine as `rbi_check_compliance`, exposed for cleaner keyword queries. Returns ranked chunks with citations across the whole corpus or filtered by topic.

### `rbi_get_document(document_id, include_text?, as_of?)` — fetch a document

Returns metadata + table of contents (chunk anchors, sections, page numbers). Pass `include_text=true` for the full assembled body. `as_of` is reserved for v1.1.

### `rbi_check_current(url_or_ref)` — safety / utility tool

Paste an RBI URL; learn whether it's current, withdrawn, or out-of-corpus. Three-step lookup: withdrawn-circulars list → active corpus → honest "not in corpus" with caveat. Supported URL patterns: `BS_ViewMasDirections.aspx?id=<MD_ID>` and `NotificationUser.aspx?Id=<NOTIF_ID>`. Other inputs return a structured `unsupported_at_v0.1` response. Never silently fails.

### Mandatory disclaimer

Every tool response includes a `_disclaimer` field at the top of the JSON object plus an `_llm_instruction` telling the consuming LLM to surface the disclaimer. Five required points: (1) not legal advice, (2) retrieval-only, (3) provisions may have been amended/withdrawn since the last corpus refresh, (4) verify with a qualified human compliance reviewer before acting, (5) unofficial.

Preserved on **error** responses too — DB unavailable, embedder OOM, etc. all keep `_disclaimer` + `_llm_instruction` at the top. Guarded by `tests/test_error_envelope.py`.

## Architecture

```
Runtime (single-process Python, any Linux/macOS host)
  ./data/db.sqlite      FTS5 + sqlite-vec, atomic-swap on refresh
  bge-base-en-v1.5      in-process via sentence-transformers
                        (~440 MB, ~80-120ms per query CPU, 10K LRU cache)
                        Or RBI_EMBEDDING_PROVIDER=cloudflare for Workers AI

Corpus build (.github/workflows/corpus-release.yml)
  Daily 02:00 UTC:    diff vs latest-corpus  (~5 min)
  Monthly 1st:        full rebuild safety net (~50 min)
  crawl → diff via content-hash → re-extract changed PDFs → embed
    → build SQLite (FTS5 + chunks_vec) → eval gate (≥80% absolute,
    <5pp regression) → sigstore-sign → publish to single rolling
    `latest-corpus` release
```

Older builds are not retained as timestamped tags; per-build forensics (commit SHA, timestamp, doc count, eval score) live inside the SQLite via the `corpus_meta` table, plus 30-day GHA artifact retention.

## Advanced

### Pin or fork the corpus

Default `rbi-source-fetch-corpus` pulls the moving `latest-corpus` release. For a specific historical corpus, download `corpus.sqlite.xz` from a specific [`corpus-release.yml`](.github/workflows/corpus-release.yml) workflow run (30-day artifact retention). For a corpus from a different repo:

```bash
rbi-source-fetch-corpus --repo your-org/your-corpus-fork --tag latest-corpus
```

Same surface from Python: `from rbi_source_mcp.fetch_corpus import fetch; fetch(repo="your-org/your-corpus-fork", tag="latest-corpus")`. Asset names (`corpus.sqlite.xz`, `.sha256`, `.sigstore.json`) are conventions a forked pipeline must publish under.

### Cryptographic verification

SHA256 is mandatory. For full sigstore verification against the GitHub Actions OIDC identity that signed the release:

```bash
pip install 'rbi-source-mcp[verify]' && rbi-source-fetch-corpus --verify-sigstore
```

Refuses any corpus not signed by the official `corpus-release.yml` workflow on `main`.

### Cloudflare Workers AI embedder

Skip the local torch model and embed via Cloudflare:

```bash
export RBI_EMBEDDING_PROVIDER=cloudflare
export CF_ACCOUNT_ID=...                 # https://dash.cloudflare.com → Account ID
export CF_API_TOKEN=...                  # → API Tokens → "Workers AI: Read"
```

Same `bge-base-en-v1.5` model on both paths; prebuilt corpus is compatible. `rbi-source-doctor` catches runtime/corpus mismatches before they can corrupt retrieval.

### Build the corpus yourself

```bash
git clone https://github.com/harshilmathur/RBI-Source-MCP.git && cd RBI-Source-MCP
uv sync
uv run rbi-source-crawl
uv run rbi-source-index-all                     # Master Directions
uv run rbi-source-index-all-circulars           # Standalone Circulars
uv run rbi-source-index-press-release --bulk    # Press Releases
uv run rbi-source-index-faq --bulk              # FAQs
uv run rbi-source-index-master-circular --bulk  # Master Circulars
uv run rbi-source-eval                   # gate at ≥80%
```

Takes ~30-60 min. Same pipeline runs daily in [`corpus-release.yml`](.github/workflows/corpus-release.yml).

### HTTP transport

The hosted endpoint above runs the HTTP transport (`rbi-source-mcp-http`); use it for the zero-install path or to host your own instance for a team. For a local self-host, stdio is the simplest transport. Behind a reverse proxy, set `RBI_TRUSTED_PROXY_HEADERS` to the client-IP header(s) your proxy injects (e.g. `x-forwarded-for`, `cf-connecting-ip`, `fly-client-ip`) AND set `RBI_TRUSTED_PROXY_CIDRS` to the proxy's egress range. Without both, per-IP rate limits collapse or become spoofable. Default (env unset) is peer IP, correct for direct exposure on localhost.

## Roadmap

Shipped surface: [CHANGELOG.md](CHANGELOG.md).

**Next:** Press Releases full archive · RBI Speeches crawler · MCP Registry listing · pin all GitHub Actions to commit SHAs.

**v1.0+:** Notifications archive (year-iterating crawler) · `document_versions` historical snapshots → `compare_versions` + `as_of` queries · amendment chain extraction → `find_updates` + `trace_relationships` tools · OCR pipeline for scanned MDs.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Highest-leverage contributions:

- Eval cases (`src/rbi_source_mcp/eval/cases.py`) — clause + expected-provision pairs from real fintech compliance work
- Corpus quality reports (issue with the "corpus quality" template; include verbatim tool input + response)
- Parser fixes for any of the five content-family list pages
- Topic-hint mapping additions in `mcp/check_compliance.py`
- New content families (open an issue first to align on scope)

## Security & telemetry

Security: see [SECURITY.md](SECURITY.md). Don't open public issues for vulnerabilities — use a [private security advisory](https://github.com/harshilmathur/RBI-Source-MCP/security/advisories/new).

Telemetry: off by default. Activates only when `POSTHOG_API_KEY` is set in the env. Install with `pip install 'rbi-source-mcp[telemetry]'`. When enabled, captures shape-only metadata (tool name, latency, length bucket, error class) — never query text, document IDs, response bodies, or client identifiers. Events are anonymous (`$process_person_profile: false`, `disable_geoip=True`); distinct ID is `MCP_INSTANCE_ID` / `FLY_MACHINE_ID` / synthesized local UUID.

## License

MIT. See [LICENSE](LICENSE).

## Disclaimer

This software returns RBI source material with citations. It does not provide legal or compliance advice. Provisions may have been amended, withdrawn, or superseded after the corpus's last refresh. Always verify against the official RBI source before acting and engage a qualified compliance reviewer for any regulatory decision. Use at your own risk.
