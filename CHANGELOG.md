# Changelog

All notable changes to RBI Source MCP. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning follows [SemVer](https://semver.org/) (current MAJOR=0; tool surface stabilizes at v1.0).

## [Unreleased]

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
