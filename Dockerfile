# RBI Source MCP — production image
# Single-stage build because the runtime needs Python anyway and the image
# size delta isn't worth the multi-stage complexity at v0.1.

FROM python:3.12-slim

# Install OS deps:
#   - poppler-utils → pdftotext (PDF extraction quality gate, ships at v1.0)
#   - sqlite3       → CLI for ops debugging
#   - ca-certificates → httpx TLS to rbi.org.in
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        sqlite3 \
        ca-certificates \
        curl \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. /cso review (#3, 2026-04-29) flagged the production
# Dockerfile for missing USER. Fly's Firecracker VM is the outer isolation
# boundary, but defense-in-depth says the in-container process should not
# be UID 0 — that way a future RCE finding can't immediately persist by
# corrupting /app or /data. We DON'T set USER here in the Dockerfile
# because the entrypoint shim needs to run as root briefly to chown the
# Fly volume mount at /data (which mounts root:root by default). The shim
# does `exec gosu rbi "$@"` to drop privileges after the chown.
RUN useradd --system --uid 1001 --gid 0 --shell /usr/sbin/nologin --home-dir /app rbi

WORKDIR /app

# Install Python deps first for layer caching.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# Data volume mounted at runtime:
#   /data/db.sqlite       (corpus, atomic-swapped on weekly refresh)
#   /data/db-prev.sqlite  (rollback target)
#   /data/telemetry.jsonl (anonymous opt-out, daily-rotated)
ENV RBI_SOURCE_DB=/data/db.sqlite
ENV PORT=8080
VOLUME /data
EXPOSE 8080

# First-deploy seed corpus. Fly's volume mount at /data takes over at
# runtime, hiding anything COPY'd into /data — so we stage the seed at
# /app/initial-db.sqlite and copy it onto the volume on first boot via
# the entrypoint shim below. The shim is idempotent: on subsequent boots,
# /data/db.sqlite already exists (from the volume) and the copy is a
# no-op. After the first successful deploy hands off to the weekly
# refresh GitHub Action, the COPY below can be dropped from this
# Dockerfile to reclaim the ~170 MB image bloat.
COPY db.sqlite.initial /app/initial-db.sqlite
# Entrypoint shim runs once per machine boot:
#   1. (root path) ensure /data is owned by the rbi user — Fly volumes mount
#      root:root by default, so the non-root runtime can't write without this.
#   2. (root path) seed /data/db.sqlite from /app/initial-db.sqlite if the
#      volume is empty (idempotent — no-op on subsequent boots).
#   3. exec the actual command under the rbi user via gosu, dropping all
#      root capabilities for the running server process.
# If somehow we're already running non-root (e.g., `docker run --user 1001`),
# the shim skips chown/seed and just execs — caller is on their own to ensure
# /data is writable.
RUN printf '%s\n' \
        '#!/bin/sh' \
        'set -e' \
        'if [ "$(id -u)" = "0" ]; then' \
        '  mkdir -p "$(dirname "$RBI_SOURCE_DB")"' \
        '  chown -R rbi:root /data 2>/dev/null || true' \
        '  if [ ! -f "$RBI_SOURCE_DB" ] && [ -f /app/initial-db.sqlite ]; then' \
        '    echo "[bootstrap] seeding $RBI_SOURCE_DB from baked corpus"' \
        '    cp /app/initial-db.sqlite "$RBI_SOURCE_DB"' \
        '    chown rbi:root "$RBI_SOURCE_DB"' \
        '  fi' \
        '  exec gosu rbi "$@"' \
        'else' \
        '  if [ ! -f "$RBI_SOURCE_DB" ] && [ -f /app/initial-db.sqlite ]; then' \
        '    cp /app/initial-db.sqlite "$RBI_SOURCE_DB"' \
        '  fi' \
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
