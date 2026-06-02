# Security Policy

## Supported versions

Only the latest commit on `main` is supported. There are no maintained release branches.

The hosted endpoint at `https://rbi-source.harshil.ai/mcp/` always tracks `main` (rolling deploys via Fly.io).

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Instead:

1. Open a [private security advisory](https://github.com/harshilmathur/RBI-Source-MCP/security/advisories/new) on GitHub. This is the preferred channel — it's structured, only visible to maintainers, and has a built-in disclosure timeline.
2. Or email the maintainer at the address listed on [@harshilmathur](https://github.com/harshilmathur)'s GitHub profile, with subject prefix `[rbi-source-mcp security]`.

Please include:
- A clear description of the issue and its impact
- Steps to reproduce, ideally with a minimal proof-of-concept
- The version / commit SHA you tested against
- Any suggested fix or mitigation, if you have one

## Disclosure timeline

- **Within 5 business days**: acknowledgement that the report was received.
- **Within 30 days**: an initial assessment (severity, scope, planned fix).
- **Within 90 days**: a fix shipped to `main` and the hosted endpoint, or a written explanation if the timeline needs to extend.
- **After fix is deployed**: public disclosure (CVE if applicable) coordinated with the reporter; credit given by default unless the reporter prefers anonymity.

## What's in scope

- The MCP server code in `src/rbi_source_mcp/`
- The OAuth ceremonial endpoints (`oauth.py`)
- Supply-chain vulnerabilities in declared dependencies (Python packages in `pyproject.toml`)
- The corpus build + signing pipeline (`.github/workflows/corpus-release.yml`)
- The hosted endpoint at `https://rbi-source.harshil.ai/mcp/` as a running instance of this code (no separate deployment surface in this repo — it's the same `rbi-source-mcp` package + corpus pipeline you'd install yourself)

## What's out of scope

- The accuracy or completeness of indexed RBI source material — this is a known limitation, mitigated by the disclaimer on every response. Stale citations are a corpus-quality issue, not a security issue; please open a regular GitHub issue with the "corpus quality" template.
- DoS via legitimate traffic patterns (the per-IP rate limit is at 60 req/min on `/mcp/`; load-test reports are welcome but not security reports).
- Issues in the user's MCP client (Claude Code, Claude.ai, ChatGPT, etc.) — report those upstream.
- Issues in dependencies that don't have a reachable code path here (e.g., an `httpx` CVE only triggered by a feature this project doesn't use).

## Hardening notes

The current security posture (audited via `/cso` 2026-04-29):

- Per-IP rate limit (60 req/60s) on `/mcp/`
- 200 KB request-body cap, enforced before parsing
- Embedder runs in a thread pool — sync work doesn't block the event loop
- Container runs as non-root (uid 1001) with privilege drop in the entrypoint
- FK-safe schema migrations
- Compound `UNIQUE(md_id, document_family)` schema (no cross-family overwrite)
- Error envelope sanitized: exception class name only, no traceback or paths
- OAuth 2.1 ceremonial endpoints (RFC 8414, 9728, 7591) — PKCE-enforced

There is **no PII, no payment data, no authentication credentials** stored or transmitted. The corpus is publicly published RBI material.
