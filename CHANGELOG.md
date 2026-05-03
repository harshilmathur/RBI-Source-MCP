# Changelog

All notable changes to RBI Source MCP. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning follows [SemVer](https://semver.org/) (current MAJOR=0; tool surface stabilizes at v1.0).

## [Unreleased]

### v0.6.2 — anonymous server-side telemetry (hosted instance only)

Adds an optional PostHog integration so the hosted instance at
`rbi-source.harshil.ai` can answer "which tools actually get used,
how slow are they, what's the error rate." Off by default everywhere
else.

- **New module `src/rbi_source_mcp/telemetry.py`** — wraps the PostHog
  Python SDK behind an env-var gate. Activates only when
  `POSTHOG_API_KEY` is set; otherwise every entry point is a no-op
  with zero allocations and no posthog import.
- **Capture point** — single `mcp_tool_called` event fired from a
  `try/finally` inside `server.py:call_tool`. One wrap, all four
  tools covered. Captures only shape-level metadata (length buckets,
  `limit`, `has_filters`, `topic_hint` enum, status, latency_ms,
  exception class name on failure), never query text, clause text,
  document IDs, URLs, or response bodies. Events use a stable
  per-machine `distinct_id` (Fly machine id when available) and
  `$process_person_profile: false` so PostHog does not build user
  profiles. `disable_geoip=True` on the SDK.
- **`hosted` extra** in `pyproject.toml` pulls `posthog>=3.0.0`.
  Dockerfile passes `--extra hosted` in both `uv sync` invocations.
  OSS install path (`uv sync` with no extras) does not pull posthog.
- **`/health` exposes `telemetry: bool`** so a quick curl confirms
  the wiring on the hosted instance.
- **Lifespan flush** — `server_http.py` calls `telemetry.shutdown()`
  on lifespan teardown so the last batch of events on a deploy
  doesn't get dropped.
- **README** — new `## Telemetry` section documents what is
  captured, what is not, and how self-hosters opt in or stay off.

Website (`rbi-source.harshil.ai`) telemetry is handled separately
via Cloudflare Web Analytics (cookieless, JS injected at the proxy).

### v0.6.1 — weekly refresh hardening + cache observability

Three fixes to `.github/workflows/refresh.yml` addressing failure modes
observed during the v0.6.0 manual migration:

1. Pre-clean stale `/data/db.sqlite.new` before sftp. `flyctl sftp put`
   refuses to overwrite, so a previous failed run would block every
   subsequent weekly cron without manual intervention.
2. Switch from `flyctl ssh sftp shell` heredoc to `flyctl sftp put`
   (one-shot, proper exit codes). The shell form was observed silently
   hanging during the migration and its exit code didn't reliably reflect
   put-success.
3. Verify byte-count match between local and remote before atomic-rename.
   A 252 MB upload truncated to 94 MB once due to a wireguard hiccup;
   without a stat check, that would have shipped a broken corpus.

`/health` now surfaces `query_cache` stats (hits / misses / currsize /
maxsize) so external monitors and the watchdog can verify cache
effectiveness without ssh.

Plus minor cleanup in `src/rbi_source_mcp/indexer/embed.py`: replace
`__import__("pathlib").Path(...)` with proper top-level import; dedup
the double `cloudflare_creds()` call inside `_cf_embed`.

### v0.6.0 — Cloudflare Workers AI embeddings

Production now embeds queries via `@cf/baai/bge-base-en-v1.5` (768-dim)
over HTTPS instead of running sentence-transformers in-process. Eval
gate holds at 24/24 cases. Local 3-way bake-off (small/base/m3) showed
m3 regresses on the headline PPI loading-limit case despite being
larger, so we ship bge-base.

**Image impact:**
- Runtime image: ~4 GB → **89 MB** (no torch, no resident model)
- Cold-boot embedder prewarm: 75-200s → **~1.07s**
- Source-only deploys: 12-14 min → **2-3 min**
- Fly machine memory: 2 GB → **1 GB**

**Code:**
- New `src/rbi_source_mcp/embedding_config.py`: env-driven
  `RBI_EMBEDDING_{PROVIDER,MODEL,DIM}` with safe defaults
  (local/bge-small/384) so existing deployments keep working with no
  env changes.
- `indexer/embed.py` dispatches between local sentence-transformers
  and Cloudflare Workers AI HTTP, with 100/batch + retry on 429/5xx
  and defensive L2 normalization.
- `indexer/embed.py`: 10K-entry LRU cache on `embed_query` keyed on
  raw user text. Identical queries (watchdog, popular questions) skip
  the CF round-trip entirely (~250ms saved).
- `db.py`: `chunks_vec` schema is now DIM-aware (was hardcoded at
  `float[384]`); `vec0(embedding float[N])` reads from
  `embedding_config.DIM`.
- `sentence-transformers` moved from main `dependencies` to
  `[project.optional-dependencies] local-embeddings`. Production runtime
  sync skips it; local devs opt in via `uv sync --extra local-embeddings`.

**Infra:**
- `Dockerfile`: drops the bge-small model pre-bake step + HF cache
  copy. Bakes `RBI_EMBEDDING_*` defaults so the image is self-configured
  once secrets land.
- `fly.toml`: memory drops 2 GB → 1 GB; comments updated to reflect the
  CF-based architecture.
- `.github/workflows/refresh.yml`: full pipeline wired (was a stub) —
  crawl + 5 indexers + eval gate + sftp + atomic-rename onto the Fly
  volume. Uses CF creds + app-scoped `FLY_API_TOKEN` from repo secrets.
- `.github/workflows/ci.yml`: installs `[dev,local-embeddings]` so the
  test suite still exercises the local embedder path.

**Research artifacts kept in repo:**
- `scripts/qa_live.py` — deep QA harness (~50 probes, ~30s) for the
  live URL.
- `scripts/watchdog.py` — light watchdog (~5 probes, ~3s) safe for cron.
- `scripts/reembed_to_bge_base.py` — corpus re-embed with adaptive
  halve-on-400 batching (CF's 60K-token cap on bge-m3).
- `scripts/eval_dump.py` + `scripts/compare_ab.py` +
  `scripts/compare_3way.py` — the bake-off harness.

### v0.5.12 — license change Apache-2.0 → MIT

Changed the project license from Apache-2.0 to MIT. MIT is the more
common choice for small Python tooling like this and makes contribution
+ downstream embedding simpler. No code changes; license/classifier
updates in 8 files: LICENSE (full text replaced), pyproject.toml
(classifier), README.md (badge + footer), CONTRIBUTING.md (clause),
server_http.py (banner JSON `license` field), static/index.html (trust
strip, architecture bullet, footer).

The earlier v0.1 CHANGELOG entry that says 'Apache-2.0 license' is left
unchanged as a historical record of what shipped at v0.1.

### v0.5.10 — Claude.ai connector compatibility

Three fixes landed in sequence chasing Claude.ai's "Custom Connectors"
flow into a working state. None affect Claude Code, curl, or any client
that worked before — they unblock browser-based MCP clients (Claude.ai,
ChatGPT) without breaking the simpler ones.

**1. Ceremonial OAuth 2.1 endpoints (`src/rbi_source_mcp/oauth.py`).**
Claude.ai walks RFC 8414 + RFC 9728 + RFC 7591 BEFORE attempting the
MCP handshake; if those discovery endpoints 404, the connector bails
with "Couldn't reach the MCP server." The corpus is public RBI text —
no actual access control to enforce — but the protocol still requires
the endpoints to exist. New endpoints:
  - `GET /.well-known/oauth-protected-resource[/mcp]` (RFC 9728)
  - `GET /.well-known/oauth-authorization-server` (RFC 8414)
  - `POST /register` (RFC 7591 dynamic client registration)
  - `GET /authorize` — auto-approves with PKCE, no user prompt
  - `POST /token` — code+verifier → bearer
  - `verify_token` stub for future use; /mcp/ does NOT gate on the bearer
    so Claude Code without OAuth keeps working

PKCE (S256) is enforced; codes are one-shot. In-memory store with TTLs.
Tests in `tests/test_oauth.py` (9 cases) cover the full handshake plus
the rate-limit exclusion (Claude.ai hits the discovery endpoints
multiple times per setup; they don't count against the 60/min budget).

**2. ASGI path normalizer (`_NormalizeMcpPath`).** After OAuth
succeeds, Claude.ai POSTs to `/mcp` (no trailing slash) carrying the
bearer token. Starlette's default behavior was a 307 redirect to
`/mcp/`, which Claude.ai didn't follow — token dropped, connector
retried OAuth ~3-4x, then bailed with "Authorization with the MCP
server failed." Just disabling redirect_slashes wasn't enough:
`Mount("/mcp", ...)` strips its prefix and the inner streamable-HTTP
session manager 404s on empty path. Fix: pure-ASGI middleware (runs
BEFORE routing) rewrites `scope.path = "/mcp/"` transparently. Added
`tests/test_http_middleware.py::test_mcp_no_trailing_slash_does_not_redirect`.

**3. Tool name rename: dots → underscores.** Claude.ai's frontend
validator rejects tool names that don't match
`^[a-zA-Z0-9_-]{1,64}$`. Renamed:
  - `rbi.check_compliance` → `rbi_check_compliance`
  - `rbi.search`           → `rbi_search`
  - `rbi.get_document`     → `rbi_get_document`
  - `rbi.check_current`    → `rbi_check_current`

Renamed across `server.py` tool definitions + dispatch, all tests, the
integration test script, and the README. Historical CHANGELOG entries
preserved with the original dotted names. Existing Claude Code users
auto-pick-up the new names via `tools/list` (no manual reconfig);
saved chats with old tool calls in history aren't affected.

**4. Integration test updated.** `t_307_redirect` (which asserted the
redirect for /mcp without slash) is now `t_no_slash_no_redirect`
(asserts 200 directly).

### v0.5.8 — first hosted deploy live

The hosted endpoint is **live** at `https://rbi-source.harshil.ai/mcp/`
(Fly.io single-machine in Mumbai, custom domain via Cloudflare DNS).

**Deploy enablers:**
- `Dockerfile` ENTRYPOINT changed to `/app/entrypoint.sh` (shim only).
  The binary name lives in CMD, where `fly.toml`'s `[processes]` block
  overrides it. Earlier mistake: baking the binary name into both
  ENTRYPOINT and `[processes]` made Fly concatenate them and the
  container boot-looped 10 times before giving up.
- New entrypoint shim seeds `/data/db.sqlite` from `/app/initial-db.sqlite`
  on first boot only. Fly's volume mount at `/data` would otherwise hide
  any DB baked into the image. The shim is idempotent: subsequent boots
  see the existing `/data/db.sqlite` and skip the copy.
- New `.dockerignore` keeps the build context lean (excludes `data/`,
  the venv, caches, tests). `db.sqlite.initial` at the repo root is
  preserved for the seed.
- `uv.lock` is now tracked for reproducible builds across local + CI +
  Fly Docker layer.
- Custom domain `rbi-source.harshil.ai` set up: A + AAAA records on
  Cloudflare DNS pointing at Fly's shared v4 + dedicated v6 IPs. Fly
  Let's-Encrypt cert auto-renews. Cloudflare proxy currently ON
  (Universal SSL wildcard cert seen by clients) — gray-cloud direct
  is documented as the alternative if the extra hop becomes a
  latency concern.

### v0.5.7 — outside-voice (codex) review fixes + schema hardening

5 real issues caught by `/codex review` that the gstack `/review`
missed. All fixed; 1 deferred; 2 declined as intentional.

- **Compound `UNIQUE(md_id, document_family)`.** Original column-level
  `UNIQUE(md_id)` would silently overwrite cross-family collisions.
  Migration v3 rebuilds legacy DBs with FK enforcement disabled, runs
  `foreign_key_check` after to surface orphans. All 3 `ON CONFLICT(md_id)`
  call sites updated to compound. `find_md_by_id(conn, md_id, family=...)`
  now family-scoped. Chunk DELETE/SELECT scoped by `document_id`
  (already family-prefixed) so a sibling family's chunks survive when
  one family is re-indexed. Verified on the live 803-doc / 56,653-chunk
  corpus: migration fired once, all rows preserved, idempotent on
  re-open. New `tests/test_schema_compound_key.py` (4 tests).
- **FK-safe migration v2.** The earlier CHECK-drop rebuild ran
  `DROP TABLE documents` with FKs ON — would fail on any DB with
  chunks already indexed. Wrap with `PRAGMA foreign_keys = OFF/ON`
  + `foreign_key_check` after.
- **Deep `/health`.** Was a process-up ping; now opens SQLite, counts
  documents, re-attempts sqlite-vec load. 503 on `corpus_empty` (LB
  removes the machine), 200 with `degraded: true` if sqlite-vec missing
  but FTS5 still works. 30s cache on result.
- **Input size cap.** `MAX_INPUT_LEN = 32_000` enforced on `text` and
  `query`. Returns wrapped `input_too_large` envelope (disclaimer
  preserved at top) so the LLM caller knows to split + retry.
  Prevents event-loop blocking on huge payloads.
- **Disclaimer text — five points, all five families.** Earlier text
  only mentioned "RBI Master Directions"; corpus now spans 5 families
  (Master Directions, Circulars, Master Circulars, Press Releases,
  FAQs). `LLM_INSTRUCTION` now enumerates 5 points (the
  non-affiliation point was missing). Test
  `test_disclaimer_lists_all_corpus_families` guards corpus coverage.

77 tests passing (was 72: +4 compound-key, +1 family-coverage). Ruff clean.

### v0.5.6 — streamable-HTTP transport

- New `server_http.py` module — Starlette ASGI app wrapping the existing
  `_build_server()` from `server.py` via `StreamableHTTPSessionManager`.
  Same 4 tools, same disclaimer wrap, different transport.
- New console script `rbi-source-mcp-http` runs uvicorn on the ASGI app.
  Honors `$PORT` (Fly convention) and `$RBI_SOURCE_HOST`.
- Three HTTP endpoints:
  - `GET /` — corpus-stats banner (JSON, curl-friendly)
  - `GET /health` — liveness probe for Fly + load balancers
  - `POST /mcp/` — streamable-HTTP MCP endpoint (note trailing slash;
    Starlette `Mount` issues a 307 from `/mcp` → `/mcp/`)
- Permissive CORS on `/mcp/` (`allow_origins=*`) so browser-based MCP
  clients can connect from a different origin.
- `fly.toml` updated: `[http_service]` block live, `internal_port=8080`,
  `auto_stop_machines=off`, `min_machines_running=1`, health check on
  `/health` every 30s. Memory bumped from 256 MB → 1 GB (bge-small +
  sentence-transformers + numpy + sqlite-vec push resident set to ~600
  MB; the original 256 MB plan was too small).
- `Dockerfile` default ENTRYPOINT switched to `rbi-source-mcp-http`
  binding 0.0.0.0:8080. Stdio binary still in the image; override with
  `docker run ... rbi-source-mcp` for local use.
- 3 new tests in `test_http_server.py`: verify `/health`, `/`, and end-
  to-end MCP tool call over HTTP returns the disclaimer-wrapped SSE
  response. Uses `asgi-lifespan.LifespanManager` to drive the lifespan
  context that initializes the session manager's task group.
- New dev dep: `asgi-lifespan>=2.1.0`.

68 tests passing (was 65); ruff clean.

### v0.5.5 — five content families indexed (current state)

**Corpus state:**
- 803 distinct documents
- 56,653 chunks (all embedded with bge-small-en-v1.5)
- 9,908 withdrawn-circular records (metadata only, for `check_current` lookup)
- ~165 MB SQLite

**Coverage by family:**
- 342 Master Directions (50,088 chunks)
- 290 Standalone Circulars (1,532 chunks)
- 98 FAQs (1,828 chunks)
- 54 Press Releases (357 chunks)
- 19 Master Circulars (2,848 chunks)

**v0.5.5 (this slice) — added Press Releases, FAQs, Master Circulars**
- New crawlers: `press_release_list`, `faq_list`, `master_circular_list`.
- New indexers: `build_press_release_index`, `build_faq_index`, `build_master_circular_index`.
- Shared `indexer/persist.py` helper — single upsert + chunks + embeddings path used by all 5 indexers.
- Schema migration v2: drop the legacy `CHECK (document_family IN (...))` constraint via table rebuild. Application-layer validation (`db.VALID_FAMILIES`) replaces it; SQLite can't ALTER a CHECK without a table rebuild and we want extensible families.
- FAQ extraction does NOT strip `<form>` (ASP.NET wraps content in `<form>`; stripping nukes the body).
- Master Circulars 2-step crawl: index page → 12 department categories → flatten direct-PDF list.
- 5 console scripts added: `rbi-source-index-press-release`, `rbi-source-index-faq`, `rbi-source-index-master-circular`, plus the existing MD/circular indexers.

**v0.5.0-v0.5.4 (prior in this session)**
- Mandatory `_disclaimer` + `_llm_instruction` injected into every MCP response (`server._wrap_response`).
- Eval set + REG-4 regression suite (`eval/cases.py`, `eval/runner.py`). 25 cases. 80% pass-rate gate. Currently 100%.
- Bulk indexer auto-retry on transient 504 Gateway Timeouts.
- Full 342-MD coverage (was originally scoped as "single-MD proof").
- Standalone Circulars added (290 indexed, parser fix for `[serial, ref, title, date]` cell layout).
- Hybrid retrieval: FTS5 (BM25 sparse) + sqlite-vec (`bge-small-en-v1.5` 384-dim dense) fused via Reciprocal Rank Fusion (k=60). RRF threshold 0.020 separates real matches from out-of-scope inputs.
- `low_confidence` calibration tightened with hybrid retrieval (was BM25-only). Still flags out-of-scope inputs (recipes, sports articles).
- `check_current` extended: 3-step lookup (withdrawn → active corpus → not-withdrawn-with-caveat).
- Issue-date parsing from PDF body header (parses "September 15, 2025" next to RBI ref) — fills in `last_updated_at` when MD title doesn't carry an "(Updated as on …)" parenthetical.
- Parenthesized sub-clause anchors: paragraph 5 now produces `5.(1)`, `5.(2)`, ..., `5.(23)` — siblings of each other, NOT nested children.
- Title-preservation rule for re-index: prefer fresh non-garbage list-page titles for circulars (which have authoritative list-page titles); preserve existing titles for MDs (whose detail-page H1 is just "Master Directions").

### v0.1.5 — compliance-check reframing

After live MCP testing, the user reframed the headline use case from
"paste an RBI URL → check withdrawal status" (v1) to "paste a clause/PRD
section → get back relevant RBI provisions with citations" (v2). The PDF
extraction + chunking + retrieval pipeline that v1 had scoped for v1.0
moved into v0.1 because compliance-checking against text needs the rule
text itself.

#### Added

- **`rbi.check_compliance(text, topic_hint?)`** — the new headline tool. Takes free text, runs hybrid retrieval (FTS5 sparse only at v0.1.5; dense via sqlite-vec ships at v0.5), returns ranked relevant provisions with citations. Retrieval-only by design — the MCP does not issue compliance verdicts; the LLM consuming the tool synthesizes from cited provisions; the user makes the decision.
- **`rbi.search(query, filters?)`** — promoted from stub to full implementation. Same retrieval engine `check_compliance` uses, exposed for direct keyword queries.
- **`rbi.get_document(document_id, include_text?, as_of?)`** — promoted from stub. Returns metadata + table of contents + optional full body.
- **Detail-page crawler** (`crawler/md_detail.py`) — fetches an MD's BS_ViewMasDirections.aspx page, locates primary PDF + annexures.
- **WAF-aware PDF fetcher** (`crawler/pdf_fetch.py`) — bypasses rbidocs.rbi.org.in's F5/Big-IP bot protection via browser headers + Referer (no JS execution required).
- **PDF text extractor** (`extractor/pdf.py`) — pdftotext wrapper with quality gate (≥80% pages text-extractable).
- **Paragraph chunker** (`indexer/chunk.py`) — outline-aware chunking with section.paragraph_anchor preservation. Letters are correctly handled as siblings (4.a, 4.b, 4.c) rather than children.
- **End-to-end indexer** (`indexer/build_md_index.py`) — exposed as `rbi-source-index <md_id>` console script.
- **Schema additions:** `chunks` table + FTS5 virtual table with auto-sync triggers + `pdf_artifacts` audit table.
- **Tests:** test_chunk.py (chunker correctness, sibling-letter anchors, idempotency), test_check_compliance.py (DX contract, low_confidence signal, citation completeness).

#### Changed

- `rbi.check_current` demoted from headline to safety/utility tool. Behavior unchanged.
- README + CHANGELOG rewritten to reflect compliance-check direction.

#### Verified live (against real RBI)

- Indexer runs end-to-end on the Payment Aggregator Master Direction (id=12896): detail page → PDF (439 KB) → text (54 KB, 26 pages, 100% extraction quality) → 136 chunks → FTS5 index.
- `check_compliance("What is the minimum net-worth required for a payment aggregator?")` returns paragraph 6.a "An entity seeking authorisation to commence or carry on PA business shall have a minimum net-worth of ₹15 crore..." as top match (BM25 -13.44).
- Out-of-scope inputs (recipe text) correctly flagged `low_confidence: true`.
- 43 unit tests passing, ruff lint clean.

### v0.1 — initial scaffold (preserved for audit trail)

- Initial scaffold from /office-hours, /plan-devex-review, and /plan-eng-review.
- Crawler for the Master Directions list (`BS_ViewMasDirections.aspx`).
- Crawler for the Withdrawn Circulars list (`NotificationUserWithdrawnCircular.aspx`).
- SQLite schema for `documents`, `withdrawn`, and `crawl_runs` tables.
- `rbi.check_current` tool — fully implemented for two URL patterns, structured "unsupported_at_v0.1" responses for everything else.
- `rbi.search` and `rbi.get_document` tool stubs returning "coming_in_v1.0" envelopes (now superseded by full implementations in v0.1.5).
- MCP stdio server entry point (`rbi-source-mcp`).
- Refresh pipeline orchestrator (`rbi-source-crawl`).
- Dockerfile + docker-compose.yml for self-host.
- fly.toml template for the hosted deployment.
- GitHub Actions: CI on push/PR + weekly refresh (Sundays 02:00 UTC).
- Apache-2.0 license + trademark disclaimer.
