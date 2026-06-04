# Contributing to RBI Source MCP

Thanks for thinking about contributing. The project is small and pragmatic, so the contribution surface is too. Read the [README](README.md) first; this file is just the mechanics.

## Where help is most useful

In rough priority order:

1. **Eval cases** in `src/rbi_source_mcp/eval/cases.py`. Real fintech compliance pairs — a clause and the RBI provision it should map to — are the single highest-leverage contribution. The eval gate (≥80% pass rate) protects retrieval quality on every change.
2. **Corpus quality reports.** If a tool returned a stale, withdrawn, or incorrectly cited provision, file an issue with the exact tool call + response. Use the "corpus quality" issue template.
3. **Parser fixes** for any of the five content-family list pages in `src/rbi_source_mcp/crawler/`. RBI ASP.NET pages drift; parsers occasionally need to keep up.
4. **Topic-hint mappings** in `src/rbi_source_mcp/mcp/check_compliance.py` — the `_TOPIC_TO_MD_ID` table biases retrieval. New mappings welcome.
5. **New content families** (e.g., RE-wise Draft Directions, Notifications archive). These are bigger lifts; open an issue first to align on scope.

## Local development

```bash
git clone https://github.com/harshilmathur/RBI-Source-MCP.git
cd RBI-Source-MCP

# uv is the project's package manager (https://docs.astral.sh/uv/)
uv sync                # install dev + runtime deps
uv run pytest          # ~192 unit tests, must pass green
uv run ruff check .    # lint, must pass clean (whole repo, matches CI)
```

To populate a local corpus (one-time, ~1-2 hours; downloads the ~440 MB `bge-base-en-v1.5` embedding model on first index run):

```bash
uv run rbi-source-crawl                                # documents + withdrawn metadata
uv run rbi-source-index-all                            # ~340 Master Directions
uv run rbi-source-index-all-circulars                  # ~290 Standalone Circulars
uv run rbi-source-index-press-release --bulk
uv run rbi-source-index-faq --bulk
uv run rbi-source-index-master-circular --bulk
uv run rbi-source-eval                                 # must pass at ≥80%
```

## Tests, lint, eval — what must pass

Before opening a PR:

- `uv run pytest` — every unit test green
- `uv run ruff check .` — clean (whole repo, including `scripts/`)
- `uv run rbi-source-eval` — pass rate ≥ 80% (currently 100%)
- If your change touches the HTTP transport, exercise `tests/test_http_server.py` and `tests/test_http_middleware.py` (banner/health/MCP routing, rate-limit + body-size middleware) — both run under `pytest`.

CI runs the first two on every push and PR.

## Pull requests

- Branch off `main`. Keep PRs small and focused — one logical change per PR.
- Commit messages: imperative subject line (e.g., `fix:`, `feat:`, `docs:`, `test:`), blank line, optional body explaining the *why*, not the *what*. The diff shows the what.
- If you're touching retrieval (chunkers, RRF logic, topic hints, ranking thresholds), include before/after eval scores in the PR description.
- If you're adding a new content family, the PR should include a parser, an indexer, an eval case, and a brief note in `CHANGELOG.md`.

AI-paired commits are fine — use a `Co-Authored-By:` trailer per the existing repo style.

## Security

If you find a security issue, please **don't** open a public issue. See [SECURITY.md](SECURITY.md) for the disclosure path.

## License

By contributing, you agree your contributions are licensed under [MIT](LICENSE), the same license as the project.
