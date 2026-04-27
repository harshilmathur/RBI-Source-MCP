# RBI Source MCP

> **Every RBI Master Direction. Version-aware. Knows what's withdrawn. Zero install.**

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that brings authoritative Reserve Bank of India regulatory information into AI coding environments like Claude.ai, Claude Code, and Cursor — with version awareness, citation-first responses, and explicit withdrawal/supersession detection.

> ⚠️ **Unofficial, community-maintained open-source tool.** RBI Source MCP is not affiliated with, endorsed by, or sponsored by the Reserve Bank of India. RBI® is a trademark of the Reserve Bank of India. For takedown or legal inquiries, contact: _<takedown email — to be set>_ (5 business-day response SLA).

## Why this exists

Fintech builders scoping a feature ("can I do recurring auto-debit under current RBI rules?") today Google `rbi.org.in`, land on an ASP.NET page that may or may not be a withdrawn circular, copy a paragraph into a PRD, and ship a feature against rules superseded three years ago. The status quo silently fails: no warning when you read withdrawn material, no version awareness, no amendment chain, no reliable citation.

RBI Source MCP fixes the silent-failure problem.

**The visceral demo:** paste a 2017 RBI URL into Claude → `"Withdrawn 2020-06-12 by RBI/DOR/2020/106. Replacement: <new URL>."`

## Coverage (planned for v1.0)

- ~80 RBI Master Directions from `BS_ViewMasDirections.aspx`
- All amendment circulars referenced inside MDs
- Full withdrawn-circulars list from `NotificationUserWithdrawnCircular.aspx`
- Weekly refresh, hash-gated, atomic-swap deploy
- KYC Master Direction is the launch-demo showcase

Coverage badges (corpus stats, last refresh) ship at v1.0.

## Quick Start

> **v0.1 status:** scaffold + crawler + MCP server skeleton. Hosted endpoint not yet live. Self-hosting via `docker compose up` is the path until the hosted URL is published.

When the hosted endpoint goes live, this will be a 2-row install table:

| Client | Connect |
|---|---|
| Claude.ai | Settings → Connectors → Add Integration → paste `<HOSTED_URL>` |
| Claude Code | `claude mcp add rbi-source --transport http <HOSTED_URL>` |

Claude Desktop + Cursor (with one-click deeplink) ship at v1.0.

## Tools (v0.1)

### `rbi.check_current(url_or_ref)` — the headline tool

Paste any RBI URL or `RBI/DOR/...` reference, get back current/withdrawn/superseded plus the replacement document.

**v0.1 supports two URL patterns:**
- `https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=<MD_ID>`
- `https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx?id=<NOTIF_ID>`

For unsupported patterns (notification URLs, FAQ URLs, textual `RBI/...` refs): returns a structured `unsupported_at_v0.1` response with a clear reason. **Never silently fails.**

### `rbi.search(query, filters?)` — hybrid retrieval

Returns chunks from current Master Directions ranked by combined BM25 (FTS5) + dense-vector (sqlite-vec, `bge-small-en-v1.5`) similarity. Filters: `topic`, `regulated_entity`, `include_withdrawn`, `as_of_date`.

### `rbi.get_document(document_id, as_of?)` — full MD fetch

Returns full text + version metadata for a Master Direction. `as_of` accepts ISO date strings; resolves to the version current on that date. Amendment-chain field ships at v1.1.

## Architecture (v0.1)

```
   Fly machine ($5/mo, always-on)
   ──────────────────────────────────
   /data/db.sqlite   ← FTS5 + sqlite-vec, atomic-swap on refresh
   /data/db-prev.sqlite ← one-command rollback target
   /data/telemetry.jsonl ← anonymous opt-out, daily-rotated
   Litestream sidecar → R2/S3 (~10s lag, disaster recovery)

   Weekly GitHub Action (Sundays 02:00 UTC)
   ──────────────────────────────────────────
   crawl → hash-gate → re-extract changed PDFs → build new SQLite
   → smoke test (10 golden queries + regression suite)
   → if PASS: scp to Fly → atomic rename → SIGHUP server (~5s blip)
   → if FAIL: abort, alert, live DB untouched
```

See [DESIGN.md](docs/DESIGN.md) for the full architecture, decisions, and review history (TBD — will be linked here once written).

## Run your own copy

```bash
git clone https://github.com/harshilmathur/RBI-Source-MCP.git
cd RBI-Source-MCP
docker compose up
```

Self-hosters: source-of-truth corpus refresh runs from this repo's GitHub Actions; you can either fetch the latest pre-built `db.sqlite` from GitHub Releases (when v1.0 ships), or run the crawler yourself with `python -m rbi_source_mcp.crawler.refresh`.

## Roadmap

**v0.1 — withdrawal sentinel (week 5 milestone):**
- Crawler for MD list + withdrawn list
- `check_current` for two URL patterns
- Hosted endpoint live on Fly
- Anonymous opt-out telemetry

**v1.0 — full corpus (week 10-12):**
- PDF extraction + quality gate
- `search` and `get_document` over all MDs
- Weekly atomic-swap refresh with smoke tests + 3 regression tests
- MCP Registry listing
- Cursor one-click install link
- Public stats page from telemetry

**v1.1+:**
- `find_updates` for the change feed
- Amendment chain extraction
- OCR pipeline for scanned MDs
- Full URL pattern parsing (notifications, FAQs, textual refs)
- `/playground` HTML zero-install try-it surface

## Contributing

Issue templates for "MD missing or wrong status," "Citation paragraph mismatch," and "Topic tag suggestion" land with the v0.1 release. Until then: open an issue describing what you found.

GitHub Discussions enabled for design questions and corpus quality reports.

## License

Apache-2.0. See [LICENSE](LICENSE).

## Disclaimer

This tool surfaces RBI source material; **it does not provide legal advice.** Use citations to inform compliance review, not replace it. RBI publications are subject to amendment without notice; this tool's "Updated as on" timestamps are best-effort.
