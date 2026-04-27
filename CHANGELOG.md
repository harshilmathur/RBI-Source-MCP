# Changelog

All notable changes to RBI Source MCP. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning follows [SemVer](https://semver.org/) (current MAJOR=0; tool surface stabilizes at v1.0).

## [Unreleased]

### Added
- Initial scaffold from /office-hours, /plan-devex-review, and /plan-eng-review.
- Crawler for the Master Directions list (`BS_ViewMasDirections.aspx`).
- Crawler for the Withdrawn Circulars list (`NotificationUserWithdrawnCircular.aspx`).
- SQLite schema for `documents`, `withdrawn`, and `crawl_runs` tables.
- `rbi.check_current` tool — fully implemented for two URL patterns, structured "unsupported_at_v0.1" responses for everything else.
- `rbi.search` and `rbi.get_document` tool stubs returning "coming_in_v1.0" envelopes.
- MCP stdio server entry point (`rbi-source-mcp`).
- Refresh pipeline orchestrator (`rbi-source-crawl`).
- Dockerfile + docker-compose.yml for self-host.
- fly.toml template for the hosted deployment (Mumbai region, single-machine, always-on).
- GitHub Actions: CI on push/PR + weekly refresh (Sundays 02:00 UTC).
- Apache-2.0 license + trademark disclaimer.

### Notes
- v0.1 ships with crawler + check_current end-to-end. PDF extraction, FTS5 + sqlite-vec indexing, hybrid search, smoke tests, and atomic-swap deploy ship at v1.0.
- Hosted endpoint URL not yet locked. Self-hosters can run via `docker compose up`.
