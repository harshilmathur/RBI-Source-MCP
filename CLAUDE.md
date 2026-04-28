# CLAUDE.md — RBI Source MCP project notes

Project-specific guidance for Claude Code agents working in this repository.
This file is loaded as context on every session start. Keep it terse, factual,
and current.

## What this project is

An MCP server that gives Claude/Cursor source-grounded retrieval over the
entire RBI corpus. **Retrieval-only by design** — never issues compliance
verdicts. The legal posture (no-legal-advice disclaimer on every response)
is load-bearing; do not change it without explicit user direction.

## Key files

```
src/rbi_source_mcp/
  ├─ server.py              MCP stdio server, registers 4 tools, wraps every response with disclaimer
  ├─ server_http.py         Streamable-HTTP transport (Starlette ASGI app + uvicorn runner).
  │                         Reuses _build_server() — same tools, same disclaimer wrap. Endpoints:
  │                         /, /health, /mcp/ (note trailing slash).
  ├─ disclaimer.py          DISCLAIMER + LLM_INSTRUCTION constants. Wording is load-bearing.
  ├─ db.py                  SQLite schema, _migrate_schema, hybrid_search (FTS5 + sqlite-vec + RRF)
  ├─ check_current.py       Withdrawn/active lookup (3-step)
  ├─ mcp/
  │   ├─ check_compliance.py  HEADLINE tool — paste clause, get cited provisions
  │   ├─ search.py            Direct keyword/topic search
  │   └─ get_document.py      Document metadata + ToC
  ├─ crawler/
  │   ├─ md_list.py / md_detail.py        Master Directions
  │   ├─ withdrawn_list.py                Withdrawn-circulars metadata (multi-section parser)
  │   ├─ circular_list.py                 Standalone Circulars list page
  │   ├─ notif_detail.py                  Generic detail-page fetcher (PDF + HTML body)
  │   ├─ press_release_list.py / press_release_detail.py
  │   ├─ faq_list.py                      FAQs (HTML-only; do NOT strip <form>)
  │   ├─ master_circular_list.py          2-step: index → 12 categories → direct PDFs
  │   ├─ pdf_fetch.py                     PDF download with WAF bypass headers
  │   └─ refresh.py                       Weekly orchestrator for MD list + withdrawn list
  ├─ extractor/pdf.py        pdftotext wrapper, RBI-ref + issue-date parsers
  ├─ indexer/
  │   ├─ chunk.py            Outline-aware paragraph chunker
  │   ├─ embed.py            bge-small-en-v1.5 (lazy-loaded singleton)
  │   ├─ persist.py          SHARED — upsert document + chunks + embeddings
  │   ├─ build_md_index.py + build_all.py
  │   ├─ build_circular_index.py + build_all_circulars.py
  │   ├─ build_press_release_index.py
  │   ├─ build_faq_index.py
  │   └─ build_master_circular_index.py
  └─ eval/
      ├─ cases.py            25 hand-labeled test cases (REG-4 regression suite)
      └─ runner.py           Eval gate; passes at ≥80%

tests/                       77 unit tests, all passing (incl. error-envelope, compound-key)
data/db.sqlite              ~171 MB; 803 documents, 56,653 chunks (5 families)
fly.toml + Dockerfile        Hosted-endpoint config; LIVE at https://rbi-source.harshil.ai/mcp/
.dockerignore                Keeps build context lean; preserves db.sqlite.initial seed
```

## Hosted endpoint

Live at `https://rbi-source.harshil.ai/mcp/` (Fly app `rbi-source-mcp`,
Mumbai, single machine, always-on). Custom domain via Cloudflare DNS
(currently orange-cloud / proxied; the gray-cloud direct option is
documented but not chosen).

- Fly v4 IP: `66.241.124.231` (shared)
- Fly v6 IP: `2a09:8280:1::10c:e3fc:0` (dedicated)
- Cert: Let's-Encrypt via Fly (auto-renew). Cloudflare wildcard cert
  is what end users see when proxy is on.
- Endpoints: `GET /` (banner), `GET /health` (deep check, 503 on
  empty corpus / sqlite-vec missing degrades to 200 + `degraded: true`),
  `POST /mcp/` (streamable-HTTP).
- Logs: `fly logs -a rbi-source-mcp`. SSH: `fly ssh console -a rbi-source-mcp`.
- Redeploy: `fly deploy --ha=false` from repo root.

## Console scripts

```
rbi-source-mcp                       Run the MCP server over stdio (local Claude Code)
rbi-source-mcp-http [--host --port]  Run the MCP server over streamable HTTP (hosted)
rbi-source-crawl                     Refresh MD list + withdrawn list
rbi-source-index <md_id>             Index one Master Direction
rbi-source-index-all                 Bulk-index all 342 Master Directions
rbi-source-index-circular <notif_id> Index one standalone circular
rbi-source-index-all-circulars       Bulk-index 290 standalone circulars
rbi-source-index-press-release [--bulk]
rbi-source-index-faq [--bulk]
rbi-source-index-master-circular --bulk
rbi-source-eval                      Run the 25-case eval gate
```

All scripts use `RBI_SOURCE_DB` env var (defaults to `./data/db.sqlite`).

## Architectural conventions

- **One `persist_document_and_chunks` for all 5 indexers.** When adding a new
  content family, do NOT duplicate the upsert + embed + insert logic. Use
  the shared helper in `indexer/persist.py`.
- **`document_id` format: `rbi:<family>:<id>`.** Family is encoded in the ID.
  The `documents.md_id` column is misnamed (legacy from MD-only days) but
  used generically for the numeric/hash external ID.
- **Family enum validated at app layer**, not via SQL CHECK. SQLite can't
  ALTER a CHECK without a table rebuild; we deliberately removed it.
  See `db.VALID_FAMILIES`.
- **Schema migrations are forward-only and idempotent.** `_migrate_schema`
  runs on every `connect()`. Adding columns: append to the `migrations` list.
  Dropping a CHECK or restructuring: write a one-shot rebuild block. **Wrap
  table rebuilds with `PRAGMA foreign_keys = OFF/ON`** — chunks reference
  documents.document_id, and `DROP TABLE documents` with FKs ON will fail
  on any DB that already has chunks. v2 + v3 migrations both follow this
  pattern; PRAGMA foreign_key_check after the rebuild surfaces orphans.
- **Every MCP response is wrapped with `_disclaimer` + `_llm_instruction`**
  at the top of the JSON object via `server._wrap_response()`. Never bypass.
  **Also on the error path** — every tool dispatch is in try/except that
  routes to `_error_response()` (which calls `_wrap_response`). Tests in
  `tests/test_error_envelope.py` guard the disclaimer-on-error contract.
- **md_id is unique per family, not globally.** Schema uses
  `UNIQUE(md_id, document_family)`. All upserts use
  `ON CONFLICT(md_id, document_family)`. Lookups by md_id alone are
  family-scoped via `find_md_by_id(conn, md_id, family=...)`.
- **Input-size cap on free-text fields.** `MAX_INPUT_LEN = 32_000` in
  server.py. `text` (check_compliance) and `query` (search) are checked
  before dispatch; oversized inputs return a wrapped `input_too_large`
  envelope. Prevents event-loop blocking on huge payloads.

## Known gotchas

- **rbidocs.rbi.org.in has F5/Big-IP bot protection.** Direct `curl` returns
  HTML (a JS challenge), not the PDF. The bypass is `crawler/pdf_fetch.py`'s
  browser headers + a `Referer` pointing back to the rbi.org.in detail page.
  No JS execution required.
- **MD list-page department headings are JavaScript-rendered.** Static crawl
  sees `<h2 class='dop_header'>"+$(curEle).html()+"</h2>` (a JS template
  string). Department field for MDs is set to `None` at v0.5; revisit if
  filtering by department becomes important.
- **FAQ pages wrap content in `<form>` (ASP.NET).** Do NOT strip `<form>`
  in FAQ body extraction — that nukes the entire body. See
  `indexer/build_faq_index._extract_faq_body`.
- **Withdrawn-circulars page is multi-section.** Different sections have
  different cell layouts (3-cell vs 4-cell rows). Parser in
  `crawler/withdrawn_list.py` handles both via cell-content heuristics.
- **Standalone-circulars list-page row layout: `[serial, ref, title, date]`.**
  Do not assume `[serial, ref, date, title]` — that bug shipped once and
  required a corpus-wide re-index to fix.
- **`bm25()` returns negative scores in SQLite.** More negative = more
  relevant. Don't invert the sign.
- **RRF fusion uses k=60.** Top fusion score for "both rankers at rank 1"
  is 2/61 ≈ 0.0328. Calibrated `low_confidence` threshold = 0.020.

## Testing

```bash
uv run pytest -q                 # 82 unit tests
uv run ruff check src/ tests/    # lint, must be clean
uv run rbi-source-eval           # corpus quality gate (must pass at ≥80%)
uv run python scripts/integration_test_public.py    # 37 end-to-end cases against the live public endpoint
```

The integration script hits `https://rbi-source.harshil.ai/mcp/` over real
HTTPS and exercises every tool, every documented topic hint, error envelopes,
security middlewares, and the HTTP transport layer. Pacing is 0.8s/call so
the rate-limit case at the end isn't poisoned by earlier traffic. Run it
after every deploy to catch regressions on the public surface. JSON reports
land in `.gstack/integration-reports/`.

The eval is the canonical regression test — if it drops below 80%, retrieval
quality has regressed and the build should fail. Currently 100% (25/25).

## Workflow conventions

- **Always run tests + ruff before committing.** CI runs both on every push.
- **Commit messages: subject + blank + body.** No em dashes (project style).
- **Co-Authored-By trailer for AI-paired commits** (consistent with prior
  commits in this repo).
- **When extending the corpus,** don't break the eval. Re-run `rbi-source-eval`
  after any pipeline change.
- **Schema changes go through `_migrate_schema`** with explicit ALTER TABLE
  ADD COLUMN guarded by PRAGMA table_info checks. Never assume the live DB
  matches the SCHEMA_SQL string.

## What's deferred / out of scope right now

| Area | Status |
|---|---|
| Hosted Fly endpoint | ✓ LIVE at `https://rbi-source.harshil.ai/mcp/` |
| Custom domain | ✓ rbi-source.harshil.ai via Cloudflare DNS |
| HTTP transport for hosted mode | ✓ landed — server_http.py + rbi-source-mcp-http console script |
| Weekly refresh GH Action | Configured but not yet running on schedule |
| Notifications archive (thousands of docs) | Year-by-year POST-form crawler not yet built |
| `document_versions` (history) | Currently only stores current state |
| Amendment chain extraction | Blocks `find_updates` + `trace_relationships` tools |
| OCR pipeline for scanned MDs | Currently excluded with `excluded: ocr_required` flag |
| Public stats page from telemetry | Telemetry sink works; stats page not built |
| `compare_versions` tool | Blocked on `document_versions` |
| `find_updates` tool | Blocked on amendment-chain extraction |

## Project memory

Design history, premise locks, DX/eng/CEO review artifacts, and per-session
timeline live in `~/.gstack/projects/rbimcp/` (locally, not in repo). Read
those when resuming work to recover full context — especially the design
doc which captures all five locked premises and the v2 reframe.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill
tool as the first action.

- "review this PR" / "code review" → invoke `review`
- "ship this" / "create PR" / "deploy" → invoke `ship`
- "is this worth building" / "brainstorm" → invoke `office-hours`
- "test the site" / "find bugs" → invoke `qa`
- "debug this" / "why broken" / 500 error → invoke `investigate`
- "weekly retro" / "what did we ship" → invoke `retro`
- "architecture review" / "review the plan" → invoke `plan-eng-review`
- "save progress" / "checkpoint" → invoke `checkpoint`
