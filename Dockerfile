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
    && rm -rf /var/lib/apt/lists/*

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
RUN printf '%s\n' \
        '#!/bin/sh' \
        'set -e' \
        'if [ ! -f "$RBI_SOURCE_DB" ] && [ -f /app/initial-db.sqlite ]; then' \
        '  echo "[bootstrap] seeding $RBI_SOURCE_DB from baked corpus"' \
        '  mkdir -p "$(dirname "$RBI_SOURCE_DB")"' \
        '  cp /app/initial-db.sqlite "$RBI_SOURCE_DB"' \
        'fi' \
        'exec "$@"' \
        > /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh

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
