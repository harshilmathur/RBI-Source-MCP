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

# Default container entrypoint: streamable-HTTP transport.
# The stdio entrypoint (rbi-source-mcp) is still installed in the image; for
# local stdio use, override with `docker run ... rbi-source-mcp`.
# Fly's [processes] block (in fly.toml) sets the actual command at runtime
# but this default makes `docker run` work end-to-end.
ENTRYPOINT ["rbi-source-mcp-http"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
