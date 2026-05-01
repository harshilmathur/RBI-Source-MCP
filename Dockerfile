# RBI Source MCP — production image
#
# Optimized for fast deploys when only the source changes (most deploys):
# layered so that `uv sync` of the heavy deps (torch, transformers, numpy)
# is cached and skipped unless `pyproject.toml` or `uv.lock` change. The
# bge-small-en-v1.5 model is pre-baked into the image so the first request
# after a fresh deploy doesn't pay the 75-200s HuggingFace download tax.
#
# Source-only deploys: ~2-3 min  (was ~12-14 min before this rewrite).

FROM python:3.12-slim

# ---------------------------------------------------------------------------
# OS deps. Cached unless the apt list itself changes.
#   - poppler-utils → pdftotext (PDF extraction quality gate)
#   - sqlite3       → CLI for ops debugging
#   - ca-certificates → httpx TLS to rbi.org.in
#   - gosu          → drop privileges in the entrypoint shim
# ---------------------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        sqlite3 \
        ca-certificates \
        curl \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. Defense-in-depth: a future RCE in any Python dep
# shouldn't immediately get root inside the container. The entrypoint shim
# runs as root briefly to chown the /data volume, then drops to `rbi` via
# gosu before exec'ing the server.
RUN useradd --system --uid 1001 --gid 0 --shell /usr/sbin/nologin --home-dir /app rbi

# ---------------------------------------------------------------------------
# uv: fast deterministic dep installer. ~10-100x faster than pip, respects
# uv.lock, and gives us proper layer caching semantics.
# ---------------------------------------------------------------------------
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# ---------------------------------------------------------------------------
# DEPENDENCY LAYER. Cached unless pyproject.toml or uv.lock change. Source
# edits below this line skip the heavy install entirely — they reuse the
# cached venv with torch, transformers, numpy, sqlite-vec, etc. all in place.
# ---------------------------------------------------------------------------
COPY pyproject.toml uv.lock ./

# UV_LINK_MODE=copy avoids cross-filesystem hardlink errors under Docker.
# UV_COMPILE_BYTECODE=1 pre-compiles .pyc at install time so cold imports
# don't pay the bytecode-compile cost.
ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1

# --no-install-project: install only the deps from uv.lock, NOT the local
# rbi-source-mcp package itself (we'll install that after copying source).
# --no-dev: skip pytest/ruff/etc. — runtime image doesn't need them.
RUN uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
# MODEL PRE-BAKE. Pull bge-small-en-v1.5 from HuggingFace at build time so
# the entrypoint doesn't pay the 75-200s download + load tax on every fresh
# boot. ~135 MB added to the image; eliminates the embedder cold-start spike
# from /health and the first user request after a deploy.
#
# Cached together with the deps layer above — only re-runs when sentence-
# transformers version changes (i.e., when uv.lock changes).
# ---------------------------------------------------------------------------
ENV HF_HOME=/app/.cache/huggingface
RUN .venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

# ---------------------------------------------------------------------------
# SOURCE LAYER. Changes on every code edit. Only the project-install step
# below has to rerun — fast because all deps are already in the cached venv.
# ---------------------------------------------------------------------------
COPY src ./src
COPY README.md LICENSE ./

# Install the project itself (editable, no deps — they're already there).
RUN uv sync --frozen --no-dev

# Put the venv on PATH so `rbi-source-mcp-http`, `rbi-source-eval`, etc.
# resolve as plain commands without needing absolute paths.
ENV PATH="/app/.venv/bin:$PATH"

# Data volume mounted at runtime:
#   /data/db.sqlite       (corpus — see README's self-host section for setup)
#   /data/db-prev.sqlite  (rollback target [planned])
#   /data/telemetry.jsonl (anonymous opt-out, daily-rotated)
ENV RBI_SOURCE_DB=/data/db.sqlite
ENV PORT=8080
VOLUME /data
EXPOSE 8080

# ---------------------------------------------------------------------------
# Entrypoint shim — runs once per machine boot:
#   1. (root path) chown /data so the rbi user can write the corpus.
#   2. exec the actual command under the rbi user via gosu.
# Container running non-root from `docker run --user 1001` skips the chown.
# ---------------------------------------------------------------------------
RUN printf '%s\n' \
        '#!/bin/sh' \
        'set -e' \
        'if [ "$(id -u)" = "0" ]; then' \
        '  mkdir -p "$(dirname "$RBI_SOURCE_DB")"' \
        '  chown -R rbi:root /data 2>/dev/null || true' \
        '  exec gosu rbi "$@"' \
        'else' \
        '  exec "$@"' \
        'fi' \
        > /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh \
    && chown -R rbi:root /app

# Default container entrypoint: streamable-HTTP transport.
# IMPORTANT: keep the binary name in CMD, not in ENTRYPOINT. fly.toml's
# `[processes]` block supplies the FULL command and Fly uses it as CMD;
# duplicating the binary name in ENTRYPOINT causes argparse failures.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["rbi-source-mcp-http", "--host", "0.0.0.0", "--port", "8080"]
