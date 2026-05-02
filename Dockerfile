# RBI Source MCP — production image, multi-stage build.
#
# Stage 1 (builder) holds uv, the wheel cache, and all build artifacts.
# Stage 2 (runtime) gets ONLY the resolved venv + model cache + source —
# no uv binary, no wheel cache, no apt cache. Final image stays well
# under Fly's 8 GB unpacked limit.
#
# Layer caching:
#   - apt deps cached unless the apt list changes
#   - uv venv cached unless pyproject.toml or uv.lock change
#   - source COPY is the last invalidation point — most deploys are
#     source-only and skip everything above
#
# v0.6: query embedding moved from local sentence-transformers to
# Cloudflare Workers AI (@cf/baai/bge-base-en-v1.5, 768-dim). The runtime
# image no longer pulls torch / sentence-transformers / the bge-small
# model — image size dropped ~3 GB → ~500 MB and cold boot from
# 75-200s to ~5-10s. Deploys land in 2-3 min (was 12-14).

# ============================================================================
# STAGE 1: BUILDER — uv, deps, model bake
# ============================================================================
FROM python:3.12-slim AS builder

# uv from the official upstream image. Faster than pip, deterministic
# from uv.lock, better layer-caching semantics.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# UV_LINK_MODE=copy: avoids cross-filesystem hardlink errors under
# Docker. We pay the duplication cost in this stage; the runtime image
# only inherits the .venv (no cache), so the bloat doesn't ship.
# UV_COMPILE_BYTECODE=1: precompile .pyc at install time, cheaper cold
# imports.
ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1

# DEPS LAYER — cached unless pyproject.toml or uv.lock change.
# `--no-dev` skips dev extras. `local-embeddings` is also an extra (not
# in main deps), so sentence-transformers + torch are NOT installed.
# Production talks to Cloudflare Workers AI over HTTPS for embeddings.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# SOURCE LAYER — changes on every code edit. uv sync at this step only
# registers the project package itself; deps are already installed.
COPY src ./src
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev

# ============================================================================
# STAGE 2: RUNTIME — slim, no build tools
# ============================================================================
FROM python:3.12-slim

# OS deps:
#   - poppler-utils → pdftotext (PDF extraction)
#   - sqlite3       → CLI for ops debugging
#   - ca-certificates → httpx TLS to rbi.org.in
#   - gosu          → drop privileges in the entrypoint shim
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        sqlite3 \
        ca-certificates \
        curl \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. The entrypoint shim runs as root briefly to
# chown the /data volume mount, then drops privileges via gosu before
# exec'ing the server process.
RUN useradd --system --uid 1001 --gid 0 --shell /usr/sbin/nologin --home-dir /app rbi

WORKDIR /app

# Copy what the runtime actually needs from the builder. The uv binary,
# wheel cache, and any build-only artifacts stay behind. No HF cache
# anymore — embeddings happen via the cloudflare provider over HTTPS.
COPY --from=builder --chown=rbi:root /app/.venv  /app/.venv
COPY --from=builder --chown=rbi:root /app/src    /app/src
COPY --from=builder --chown=rbi:root /app/pyproject.toml /app/uv.lock /app/README.md /app/LICENSE ./

# Put the venv on PATH so console scripts resolve as plain commands.
ENV PATH="/app/.venv/bin:$PATH"

# Data volume mounted at runtime:
#   /data/db.sqlite       (corpus — see README's self-host section for setup)
#   /data/db-prev.sqlite  (rollback target [planned])
#   /data/telemetry.jsonl (anonymous opt-out, daily-rotated)
ENV RBI_SOURCE_DB=/data/db.sqlite

# Embedding-provider defaults baked into the image. NOT secrets — these
# just tell the app to use Cloudflare Workers AI with bge-base @ 768-dim.
# CF auth (CF_ACCOUNT_ID, CF_API_TOKEN) comes from Fly secrets at runtime;
# embedding_config.cloudflare_creds() reads them on first call.
ENV RBI_EMBEDDING_PROVIDER=cloudflare
ENV RBI_EMBEDDING_MODEL=@cf/baai/bge-base-en-v1.5
ENV RBI_EMBEDDING_DIM=768

ENV PORT=8080
VOLUME /data
EXPOSE 8080

# Entrypoint shim — runs once per machine boot:
#   1. (root path) chown /data so the rbi user can write the corpus.
#   2. exec the actual command under the rbi user via gosu.
# Container running non-root from `docker run --user 1001` skips the chown.
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
    && chown rbi:root /app/entrypoint.sh

# Default container entrypoint: streamable-HTTP transport.
# fly.toml's [processes] block supplies the FULL command (binary +
# flags) and Fly uses that as CMD. Don't bake the binary name into
# ENTRYPOINT or it gets duplicated in the final exec.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["rbi-source-mcp-http", "--host", "0.0.0.0", "--port", "8080"]
