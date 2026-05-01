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
#   - bge-small model cached with the venv (re-pulls on
#     sentence-transformers version bump)
#   - source COPY is the last invalidation point — most deploys are
#     source-only and skip everything above
#
# Source-only deploys: ~2-3 min  (was ~12-14 min on the old single-stage).

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
# imports for the embedder.
ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1
ENV HF_HOME=/app/.cache/huggingface

# DEPS LAYER — cached unless pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# MODEL PRE-BAKE — pull bge-small-en-v1.5 once at build time so the
# entrypoint doesn't pay the 75-200s HuggingFace download tax on every
# fresh boot. Cached together with the deps layer; only re-runs when
# sentence-transformers version bumps in uv.lock.
RUN .venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

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
# wheel cache, and any build-only artifacts stay behind.
COPY --from=builder --chown=rbi:root /app/.venv  /app/.venv
COPY --from=builder --chown=rbi:root /app/.cache /app/.cache
COPY --from=builder --chown=rbi:root /app/src    /app/src
COPY --from=builder --chown=rbi:root /app/pyproject.toml /app/uv.lock /app/README.md /app/LICENSE ./

# Put the venv on PATH so console scripts resolve as plain commands.
ENV PATH="/app/.venv/bin:$PATH"
ENV HF_HOME=/app/.cache/huggingface

# Data volume mounted at runtime:
#   /data/db.sqlite       (corpus — see README's self-host section for setup)
#   /data/db-prev.sqlite  (rollback target [planned])
#   /data/telemetry.jsonl (anonymous opt-out, daily-rotated)
ENV RBI_SOURCE_DB=/data/db.sqlite
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
