# CLAUDE.md — RBI Source MCP project notes

Project-specific guidance for Claude Code agents working in this repository.
This file is loaded as context on every session start. Keep it terse, factual,
and current.

## What this project is

An MCP server that gives Claude/Cursor source-grounded retrieval over the
entire RBI corpus. **Retrieval-only by design** — never issues compliance
verdicts. The legal posture (no-legal-advice disclaimer on every response)
is load-bearing; do not change it without explicit user direction.

**Status as of v0.9.0 (2026-06-01):** public OSS repo, published on PyPI
(`pip install rbi-source-mcp`), corpus distributed via signed GitHub
Releases. A maintainer-run hosted instance is available at
`rbi-source.harshil.ai` (deployment specifics are out of scope for this
tree — see "Hosted endpoint" below). Three-line install works end-to-end
for any user. SHA-pinned every GHA workflow action, wired Dependabot,
audited end-to-end with adversarial code review. `GET /` on the HTTP
transport now serves a corpus-stats JSON banner to every caller (no
in-package HTML/favicon — a deployment layers any branded page on at the
transport edge).

## Key files

```
src/rbi_source_mcp/
  ├─ server.py              MCP stdio server, registers 4 tools, wraps every response with disclaimer
  ├─ server_http.py         Streamable-HTTP transport (Starlette ASGI app + uvicorn runner).
  │                         Reuses _build_server() — same tools, same disclaimer wrap. Endpoints:
  │                         /, /health, /mcp/ (note trailing slash).
  ├─ fetch_corpus.py        v0.8 — `rbi-source-fetch-corpus` CLI. Downloads + verifies
  │                         (SHA256 + optional sigstore) + decompresses + schema-checks
  │                         + atomically installs the corpus from GitHub Releases.
  │                         Read-only via SQLite URI mode=ro for the schema check.
  ├─ doctor.py              v0.8 — `rbi-source-doctor` preflight CLI. 8 checks:
  │                         python, sqlite_vec, corpus, corpus_runtime_match
  │                         (dim match), hf_cache, cf_creds, torch, local_model.
  ├─ disclaimer.py          DISCLAIMER + LLM_INSTRUCTION constants. Wording is load-bearing.
  ├─ db.py                  SQLite schema, _migrate_schema, corpus_meta KV table,
  │                         CORPUS_SCHEMA_VERSION constant, hybrid_search
  │                         (FTS5 + sqlite-vec + RRF). _stamp_schema_version
  │                         only stamps when missing (never overwrites).
  ├─ check_current.py       Withdrawn/active lookup (3-step)
  ├─ telemetry.py           Optional PostHog (env-gated). MCP_INSTANCE_ID /
  │                         MCP_REGION env vars; FLY_* fallback for back-compat.
  ├─ mcp/
  │   ├─ check_compliance.py  HEADLINE tool — paste clause, get cited provisions
  │   ├─ search.py            Direct keyword/topic search
  │   └─ get_document.py      Document metadata + ToC
  ├─ crawler/                ... (5 families: md_list, withdrawn_list, circular_list,
  │                              press_release_list, faq_list, master_circular_list,
  │                              notif_detail, pdf_fetch, refresh)
  ├─ extractor/pdf.py        pdftotext wrapper, RBI-ref + issue-date parsers
  ├─ embedding_config.py     Provider/model/dim driven by env (RBI_EMBEDDING_*).
  │                          v0.8.1 unified default: bge-base-en-v1.5 @ 768-dim
  │                          for BOTH local and cloudflare providers.
  │                          LOCAL_MODEL_REVISION env pins the HF revision.
  ├─ indexer/
  │   ├─ chunk.py            Outline-aware paragraph chunker
  │   ├─ embed.py            Provider-dispatching embedder. Local: sentence-transformers
  │   │                      (lazy singleton, revision-pinned); cloud: Cloudflare Workers AI.
  │   │                      embed_query() has a 10K-entry LRU cache keyed on raw text.
  │   ├─ persist.py          SHARED — upsert document + chunks + embeddings
  │   ├─ build_md_index.py + build_all.py        (already_indexed_ids: content-hash
  │   │                                           skip via JOIN on document_id +
  │   │                                           date(fetched_at) >= last_updated_at)
  │   ├─ build_circular_index.py + build_all_circulars.py
  │   ├─ build_press_release_index.py
  │   ├─ build_faq_index.py
  │   └─ build_master_circular_index.py
  └─ eval/
      ├─ cases.py            ~25 hand-labeled test cases (REG-4 regression suite)
      └─ runner.py           Eval gate; passes at ≥80%

.github/workflows/
  ├─ ci.yml                  Tests on push/PR (matrix: 3.11, 3.12)
  ├─ corpus-release.yml      Daily 02:00 UTC diff build + monthly 1st-of-month full
  │                          rebuild. Crawl → index → smoke gate (≥80% absolute
  │                          AND <5pp regression) → sigstore-sign → publish to
  │                          GitHub Releases (timestamped tag + latest-corpus alias).
  └─ release.yml             PyPI publish on v* tag via trusted publishing OIDC.
                             Sign-in-build-job pattern: build + sigstore-sign +
                             then publish + attach-release run in parallel from
                             signed artifacts.

scripts/                     Ops + research tooling, not shipped in the runtime image.
  ├─ reembed_to_bge_base.py  Corpus re-embed across providers (A/B testing)
  ├─ eval_dump.py + compare_ab.py + compare_3way.py   Embedding-model bake-off

tests/                       192 unit tests passing, 3 skipped. HTTP transport
                             coverage in test_http_server.py + test_http_middleware.py
                             (incl. the JSON-only GET / contract: browser Accept
                             gets JSON, favicon/homepage paths 404)
data/db.sqlite              ~250 MB at 768-dim; ~803 docs, ~57k chunks (drifts daily)
```

## Hosted endpoint (maintainer's instance)

A free, no-auth instance of this package runs at `https://rbi-source.harshil.ai/mcp/`.
It runs the same code + corpus pipeline that's in this repo; deployment specifics
are out of scope for this tree.

## Console scripts

Headline UX (the three a self-host user actually touches):

```
rbi-source-mcp                       Run the MCP server over stdio (Claude Desktop / Claude Code)
rbi-source-fetch-corpus              Download + verify + install the prebuilt corpus from GH Releases
rbi-source-doctor                    Preflight: python / sqlite-vec / corpus / dim match / hf-cache / model
```

Advanced (used by the GHA corpus-release workflow + by users who want to
crawl + index from scratch):

```
rbi-source-mcp-http [--host --port]  Streamable-HTTP transport (for hosting behind a proxy)
rbi-source-crawl                     Refresh MD list + withdrawn list
rbi-source-index <md_id>             Index one Master Direction
rbi-source-index-all                 Bulk-index all Master Directions
rbi-source-index-circular <notif_id> Index one standalone circular
rbi-source-index-all-circulars       Bulk-index ~290 standalone circulars
rbi-source-index-press-release [--bulk]
rbi-source-index-faq [--bulk]
rbi-source-index-master-circular --bulk
rbi-source-eval                      Run the ~25-case eval gate (≥80% must pass)
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
uv run pytest -q                 # 192 unit tests pass, 3 skipped
uv run ruff check src/ tests/    # lint, must be clean
uv run rbi-source-eval           # corpus quality gate (must pass at ≥80%)
```

End-to-end integration tests against a live HTTP endpoint live outside this
tree (operator-side, not part of the OSS package). The eval gate inside this
repo is the canonical regression test.

The eval is the canonical regression test — if it drops below 80%, retrieval
quality has regressed and the build should fail. Currently 100% (25/25).
The corpus-release workflow (`.github/workflows/corpus-release.yml`) gates
publish on ≥80% absolute AND <5pp regression vs the prior release's eval.

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
| **PyPI distribution** | ✓ LIVE at `pip install rbi-source-mcp` (v0.9.0, sigstore-signed) |
| **Repo public** | ✓ flipped public 2026-05-05; OSS surface clean |
| Hosted endpoint (maintainer's instance) | ✓ LIVE at `https://rbi-source.harshil.ai/mcp/` (deployment specifics out of scope for this tree) |
| HTTP transport | ✓ landed — server_http.py + rbi-source-mcp-http console script; `GET /` serves a JSON banner to all callers |
| Corpus build → GitHub Releases | ✓ corpus-release.yml: daily 02:00 UTC diff + monthly 1st-of-month full; bootstrap completed 2026-05-05; switched from weekly→daily on 2026-05-05 |
| `rbi-source-fetch-corpus` + `rbi-source-doctor` | ✓ landed in v0.8 |
| **GHA actions pinned to commit SHAs** | ✓ landed in v0.8.4 — publishing path locked; Dependabot at `.github/dependabot.yml` tracks the `# pin: <name>@<tag>` comments. ci.yml uses tag-based pin (acceptable for non-publishing CI) |
| **Security hardening** | ✓ landed in v0.8.4 — env-gated rate-limit IP (`RBI_TRUSTED_PROXY_HEADERS`), OAuth bucket separation, dim-mismatch warning surfaced, eager DB validation, FTS5 escape internalized, OAuth metadata trimmed to honest set, allow_credentials=False explicit |
| Press Releases (last 50, no historical backfill) | ✓ live — `BS_PressReleaseDisplay.aspx` top-50 only, by user direction (the page itself doesn't expose deeper history) |
| Acts & Regulations (statutes) | **Next up — proposed v0.9.** Closes the "statute < MD < amendment" layering for compliance answers. ~10 statutes, low volume, stable |
| RBI Speeches | Deferred — user signal explicitly against (low compliance signal vs noise) |
| Notifications archive (umbrella feed since 1991) | Deferred — research showed mostly-redundant with what we have (MDs + Standalones); long tail is operational/agency-banking ops, low compliance signal |
| Amendment Directions | Skipped after research — RBI re-uploads MDs in-place with consolidated text, so amendments are history-only. Current state of every rule is already in our MD index |
| `document_versions` (history) | Currently only stores current state |
| Amendment chain extraction | Blocks `find_updates` + `trace_relationships` tools |
| OCR pipeline for scanned MDs | Currently excluded with `excluded: ocr_required` flag |
| Public stats page from telemetry | PostHog dashboard exists for maintainer; public page not built |
| `compare_versions` tool | Blocked on `document_versions` |
| `find_updates` tool | Blocked on amendment-chain extraction |
| MCP Registry submissions | Submission content prepared; listing pending |

## Design history

The CHANGELOG and git history are the public record of what shipped and why.
Architectural rationale lives in module/function docstrings and the per-PR
descriptions; read those when resuming work to recover context.
