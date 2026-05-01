# RBI Source MCP — production image
# Single-stage build because the runtime needs Python anyway and the image
# size delta isn't worth the multi-stage complexity at v0.1.

FROM python:3.12-slim

# Install OS deps:
#   - poppler-utils → pdftotext (PDF extraction quality gate, ships at v1.0)
#   - sqlite3       → CLI for ops debugging
#   - ca-certificates → httpx TLS to rbi.org.in
#   - gosu          → drop privileges in the entrypoint shim (see USER note below)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        sqlite3 \
        ca-certificates \
        curl \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. Defense-in-depth: a future RCE finding in any of the
# Python deps shouldn't immediately get root inside the container. We DON'T
# set USER here in the Dockerfile because the entrypoint shim needs to run as
# root briefly to chown the volume mount at /data (which mounts root:root by
# default on most platforms). The shim does `exec gosu rbi "$@"` to drop
# privileges before the actual server process starts.
RUN useradd --system --uid 1001 --gid 0 --shell /usr/sbin/nologin --home-dir /app rbi

WORKDIR /app

# Install Python deps first for layer caching.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# Data volume mounted at runtime:
#   /data/db.sqlite       (corpus — see the README's self-host section for
#                          how to populate it on a fresh deployment)
#   /data/db-prev.sqlite  (rollback target)
#   /data/telemetry.jsonl (anonymous opt-out, daily-rotated)
ENV RBI_SOURCE_DB=/data/db.sqlite
ENV PORT=8080
VOLUME /data
EXPOSE 8080

# Entrypoint shim runs once per machine boot:
#   1. (root path) ensure /data is owned by the rbi user — host volume mounts
#      typically land as root:root, so the non-root runtime can't write
#      without this.
#   2. exec the actual command under the rbi user via gosu, dropping all
#      root capabilities for the running server process.
# If somehow we're already running non-root (e.g., `docker run --user 1001`),
# the shim skips the chown and just execs — caller is on their own to ensure
# /data is writable.
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
# The stdio entrypoint (rbi-source-mcp) is still installed in the image; for
# local stdio use, override with `docker run ... rbi-source-mcp`.
#
# IMPORTANT: keep the binary name in CMD, not in ENTRYPOINT. fly.toml's
# `[processes]` block supplies the FULL command (`rbi-source-mcp-http
# --host 0.0.0.0 --port 8080`) which Fly uses as CMD. If the binary name
# is also in ENTRYPOINT, Fly concatenates them and you get
# `/app/entrypoint.sh rbi-source-mcp-http rbi-source-mcp-http --host ...`
# — argparse rejects the dup. Shim only.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["rbi-source-mcp-http", "--host", "0.0.0.0", "--port", "8080"]
