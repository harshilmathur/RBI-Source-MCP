# RBI Source MCP

> **Every RBI Master Direction. Version-aware. Knows what's withdrawn. Zero install.**

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that brings authoritative Reserve Bank of India regulatory information into AI coding environments like Claude.ai, Claude Code, and Cursor — with version awareness, citation-first responses, and explicit withdrawal/supersession detection.

> ⚠️ **Unofficial, community-maintained open-source tool.** RBI Source MCP is not affiliated with, endorsed by, or sponsored by the Reserve Bank of India. RBI® is a trademark of the Reserve Bank of India. For takedown or legal inquiries, contact: _<takedown email — to be set>_ (5 business-day response SLA).

## Why this exists

Fintech builders writing a clause, PRD section, or partner agreement currently ping the compliance team in Slack and wait hours-to-days to know if their language conflicts with a current RBI rule. Or they Google `rbi.org.in`, land on an ASP.NET page that may or may not be a withdrawn circular, and ship against rules superseded three years ago.

RBI Source MCP collapses this loop. Paste a clause or section into Claude/Cursor with this MCP connected, and Claude returns the relevant RBI Master Direction provisions with citations, paragraph anchors, source URLs, and current/withdrawn status. Retrieval-only by design — this MCP doesn't issue compliance verdicts; the LLM synthesizes from the cited source material, and the user makes the call.

**The visceral demo:** paste a clause from a draft TOS into Claude → cited paragraphs from the Payment Aggregator Master Direction, with paragraph anchors, the official RBI URL, and the published date. Verify with a human compliance reviewer before shipping.

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

## Tools (v0.1.5)

### `rbi.check_compliance(text, topic_hint?)` — HEADLINE

Paste free text (a clause, PRD section, draft policy paragraph, code comment) and get back ranked relevant Master Direction provisions with citations: paragraph anchor, official URL, RBI reference, last-updated date, current/withdrawn status.

**Retrieval-only by design.** The MCP does not issue compliance verdicts. The LLM consuming the tool synthesizes the verdict from cited provisions; the user makes the decision. This separation keeps the MCP defensibly on the source-layer side of the line — we are not an unauthorized regulatory advisor.

`topic_hint` (optional) biases retrieval toward a known topic. Supported v0.1.5: `"payment_aggregator"`, `"pa"`, `"pa_pg"`. Unknown values are ignored.

Out-of-scope inputs (recipes, sports articles, anything unrelated) return `low_confidence: true` so consuming LLMs can decline to synthesize.

### `rbi.search(query, filters?)` — direct retrieval

Same engine `check_compliance` uses, exposed for cleaner keyword queries: "what are the net-worth requirements for PAs". Returns ranked chunks with citations.

v0.1.5 uses FTS5 sparse retrieval. Hybrid (FTS5 + sqlite-vec dense) ships at v0.5.

### `rbi.get_document(document_id, include_text?, as_of?)` — fetch a Master Direction

Returns metadata + table of contents (chunk anchors, sections, page numbers). Pass `include_text=true` to also get the full assembled body. `as_of` reserved for v1.1; ignored at v0.1.5.

### `rbi.check_current(url_or_ref)` — safety/utility tool

Paste an RBI URL and learn whether it's current, withdrawn, or out-of-corpus. Useful for verifying a citation a user already has. **v0.1.5 supports two URL patterns:**
- `https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=<MD_ID>` — Master Direction lookup, returns `current` / `unknown`.
- `https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=<NOTIF_ID>` — circular lookup, returns `withdrawn` (with `withdrawn_date`) if the ID appears in RBI's withdrawn-circulars list, else `not_withdrawn`.

For unsupported patterns (FAQ URLs, textual `RBI/...` refs, anything else): returns a structured `unsupported_at_v0.1` response. **Never silently fails.**

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

**v0.1.5 — single-MD compliance proof (current):**
- Detail-page crawler + WAF-aware PDF fetcher (rbidocs bot-protection bypass via browser headers + Referer)
- PDF extraction with quality gate
- Paragraph chunking with section anchors (137 chunks for the PA MD)
- FTS5 sparse retrieval over chunks
- `rbi.check_compliance`, `rbi.search`, `rbi.get_document` working end-to-end
- Currently indexed: Master Direction on Regulation of Payment Aggregator (PA), id=12896

**v0.5 — top-10 MDs (next):**
- Same pipeline scaled to KYC, digital lending, tokenisation, and 7 other high-traffic MDs
- Dense embeddings (`bge-small-en-v1.5` via sqlite-vec) for hybrid retrieval
- Eval set of 20 hand-labeled (clause, expected provision) pairs, must hit top-3 at ≥80%
- Hosted endpoint live on Fly with weekly atomic-swap refresh

**v1.0 — full corpus:**
- All 342 Master Directions indexed
- 3 named regression tests gating every refresh
- MCP Registry listing, Cursor one-click install link, public stats page from telemetry

**v1.1+:**
- Amendment chain extraction
- `find_updates` for the change feed
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
