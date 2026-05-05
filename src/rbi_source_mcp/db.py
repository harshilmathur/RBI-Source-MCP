"""SQLite schema and helpers for the RBI Source MCP corpus.

v0.1 added documents/withdrawn/crawl_runs.
v0.1.5 added chunks + chunks_fts (FTS5 sparse).
v0.5 added chunks_vec (sqlite-vec dense embeddings) and hybrid retrieval.

Schema:
    documents          -- canonical MD records from the list crawl
    withdrawn          -- withdrawal records
    crawl_runs         -- audit trail of every crawl
    chunks             -- citable paragraph rows from indexed MDs
    chunks_fts         -- FTS5 inverted index over chunks.text (sparse)
    chunks_vec         -- sqlite-vec virtual table, 384-dim float32 (dense)
    pdf_artifacts      -- audit trail of PDF fetches and extractions

The DB file is rebuilt by the weekly GitHub Action. Self-hosters can
download `corpus.sqlite.xz` from the [latest-corpus release](https://github.com/harshilmathur/RBI-Source-MCP/releases/tag/latest-corpus)
manually for now (v0.7.1 will ship a `rbi-source-fetch-corpus` console
script for one-line install).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


VALID_FAMILIES: frozenset[str] = frozenset(
    {
        "master_direction",
        "standalone_circular",
        "notification",
        "master_circular",
        "press_release",
        "faq",
    }
)


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension on a connection.

    Returns True on success, False if loading is unsupported on this Python
    build. The hybrid search path falls back to FTS5-only if False, so search
    still works (just sparse-only).
    """
    try:
        import sqlite_vec
    except ImportError:
        logger.warning("sqlite_vec.import_fail", note="dense retrieval disabled")
        return False
    try:
        conn.enable_load_extension(True)
    except (sqlite3.NotSupportedError, AttributeError):
        logger.warning("sqlite_vec.load_extension_disabled")
        return False
    try:
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except sqlite3.OperationalError as exc:
        logger.warning("sqlite_vec.load_fail", error=str(exc))
        return False


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    document_id      TEXT PRIMARY KEY,    -- e.g., "rbi:master_direction:12550" or "rbi:circular:12922"
    md_id            TEXT NOT NULL,       -- numeric RBI ID; named md_id for legacy reasons but used generically across families
    title            TEXT NOT NULL,
    detail_url       TEXT NOT NULL,
    last_updated_at  TEXT,                -- "Updated as on <date>" parsed from title; ISO 8601
    issued_date      TEXT,                -- ISO 8601 date when the document was issued (from list page or PDF body)
    rbi_ref          TEXT,                -- RBI reference number (e.g., "DOR.AML.REC.61/14.06.001/2025-26")
    department       TEXT,                -- v0.1: always NULL for MDs (page is JS-rendered for groupings)
    document_family  TEXT NOT NULL DEFAULT 'master_direction',
    -- valid families validated at the application layer (db.py: VALID_FAMILIES).
    -- We deliberately don't use a CHECK constraint because SQLite can't ALTER
    -- a CHECK without a table rebuild, and we want to be able to add new
    -- families without an offline migration on every deployed corpus.
    pdf_urls_json    TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'current'
                       CHECK (status IN ('current', 'withdrawn', 'superseded', 'draft', 'unknown')),
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    raw_list_sha256  TEXT,                -- sha of the list page that introduced this row
    -- Compound uniqueness: md_id is unique WITHIN a family, not across all
    -- families. master_direction id=12922 and press_release prid=12922 are
    -- distinct documents and must not collide on upsert. Codex review
    -- 2026-04-28 caught the original column-level UNIQUE(md_id) as a
    -- cross-family overwrite hazard.
    UNIQUE (md_id, document_family)
);

CREATE INDEX IF NOT EXISTS idx_documents_md_id ON documents(md_id);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
-- idx_documents_family is created inside _migrate_schema after the column
-- itself is added, so existing pre-migration databases don't fail here.

CREATE TABLE IF NOT EXISTS withdrawn (
    -- Identity is the original circular's ID when known, else original_ref + title hash.
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    original_ref      TEXT,                -- e.g., "RBI/DOR/2018/45"
    original_id       TEXT,                -- numeric ID from NotificationUser.aspx?Id=
    title             TEXT NOT NULL,
    issued_date       TEXT,
    withdrawn_date    TEXT,
    replacement_ref   TEXT,
    detail_url        TEXT,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_withdrawn_original_id ON withdrawn(original_id);
CREATE INDEX IF NOT EXISTS idx_withdrawn_original_ref ON withdrawn(original_ref);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    source            TEXT NOT NULL,        -- 'md_list' | 'withdrawn_list' | 'md_detail' | 'pdf_fetch' | etc.
    source_url        TEXT NOT NULL,
    status_code       INTEGER,
    raw_html_sha256   TEXT,
    items_seen        INTEGER,
    items_added       INTEGER,
    items_changed     INTEGER,
    items_removed     INTEGER,
    error             TEXT
);

-- v0.1.5: chunks + FTS5 for compliance retrieval
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id         TEXT PRIMARY KEY,        -- e.g., "rbi:md:12896:p:8.2.a"
    document_id      TEXT NOT NULL,           -- FK to documents.document_id
    md_id            TEXT NOT NULL,           -- denormalized for fast joins
    rbi_ref          TEXT,                    -- e.g., "RBI/DPSS/2025-26/141" parsed from PDF header
    section          TEXT,                    -- e.g., "8" (numeric section)
    paragraph_anchor TEXT,                    -- e.g., "8.2.a"
    page             INTEGER,                 -- best-effort page number from pdftotext
    text             TEXT NOT NULL,
    text_sha256      TEXT NOT NULL,
    char_start       INTEGER,                 -- offset in the original extracted text
    char_end         INTEGER,
    pdf_url          TEXT,
    indexed_at       TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id)
);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_md_id ON chunks(md_id);

-- FTS5 contentless-shadow index. We store the actual text in `chunks` and use
-- FTS5 just for the inverted index. Joining via rowid keeps it fast.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Triggers keep FTS5 in sync with chunks. Insert/update/delete must propagate.
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE IF NOT EXISTS pdf_artifacts (
    pdf_url          TEXT PRIMARY KEY,
    document_id      TEXT NOT NULL,
    md_id            TEXT NOT NULL,
    pdf_sha256       TEXT NOT NULL,
    text_sha256      TEXT NOT NULL,
    bytes            INTEGER NOT NULL,
    page_count       INTEGER,
    extraction_quality REAL,                  -- pages_with_text / total_pages, 0.0-1.0
    is_indexed       INTEGER NOT NULL DEFAULT 0,  -- 1 once chunks land
    fetched_at       TEXT NOT NULL,
    extracted_at     TEXT,
    error            TEXT
);

-- v0.7: corpus metadata kv table. Used for schema version, embedding model
-- identity, build timestamps, etc. The schema_version row gates downstream
-- consumers (the v0.7.1 fetch script will refuse on mismatch with the running
-- package's expected version) so a v0.7 install can never silently read a
-- v0.8 corpus that has new columns / new tables / different embed dim.
CREATE TABLE IF NOT EXISTS corpus_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Bump this whenever the corpus shape changes in a way that breaks an
# older runtime: new required column, new table the runtime SELECTs from,
# embedding dimension change, FTS/vec rebuild required, etc. The fetch
# script refuses to install a corpus whose schema_version doesn't match.
# Kept short — gets embedded in the GitHub Release artifact filename via
# the corpus-release.yml workflow.
CORPUS_SCHEMA_VERSION = "1"

# v0.5: chunks_vec virtual table for dense embeddings. We create it
# separately because virtual-table DDL requires the extension to be loaded.
# Dimension is driven by embedding_config.DIM so an A/B run with a different
# embedding model (bge-base@768, bge-m3@1024, etc.) doesn't need a code
# change here. Default stays 384 for the legacy bge-small-en-v1.5 path.
def _chunks_vec_schema_sql() -> str:
    from . import embedding_config as _cfg

    return (
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec "
        f"USING vec0(embedding float[{_cfg.DIM}]);"
    )


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Context manager that opens, initializes, and closes a SQLite connection.

    Loads the sqlite-vec extension (best-effort) so dense-retrieval queries
    work. If extension loading is unsupported on this Python build, FTS5-only
    sparse retrieval still works.

    The schema is applied on every open (CREATE IF NOT EXISTS makes it cheap
    and idempotent). Returns a connection with row_factory=sqlite3.Row.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA_SQL)
        _migrate_schema(conn)
        _stamp_schema_version(conn)
        # Try to load sqlite-vec and create the virtual table. Both are
        # idempotent and silent on failure (logs a warning).
        if _load_sqlite_vec(conn):
            try:
                conn.execute(_chunks_vec_schema_sql())
            except sqlite3.OperationalError as exc:
                logger.warning("chunks_vec.create_fail", error=str(exc))
        try:
            yield conn
        except Exception:
            # Rollback on exception so partial writes don't get committed by
            # the finally block. Re-raise after rollback so callers (and
            # bulk-indexer retry logic) see the original failure.
            conn.rollback()
            raise
        else:
            conn.commit()
    finally:
        conn.close()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply forward-only migrations for columns that didn't exist in earlier
    schema revisions. SQLite's CREATE TABLE IF NOT EXISTS doesn't add columns
    to a pre-existing table; we patch them in here.

    Each ALTER TABLE is wrapped in try/except so re-running on a freshly-
    created table (where the columns already exist) is a no-op. Cheap to run
    on every connect.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
    migrations: list[tuple[str, str]] = [
        ("document_family", "ALTER TABLE documents ADD COLUMN document_family TEXT NOT NULL DEFAULT 'master_direction'"),
        ("issued_date",     "ALTER TABLE documents ADD COLUMN issued_date TEXT"),
        ("rbi_ref",         "ALTER TABLE documents ADD COLUMN rbi_ref TEXT"),
    ]
    for col, sql in migrations:
        if col not in cols:
            try:
                conn.execute(sql)
                logger.info("schema.migrate", added_column=col)
            except sqlite3.OperationalError as exc:
                logger.warning("schema.migrate_fail", column=col, error=str(exc))
    # Backfill helpful index after the column exists.
    import contextlib

    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_family ON documents(document_family)")

    # Migration v2: drop the old CHECK constraint on document_family.
    # We started without a CHECK; added one in commit 00e01c7 with 4 families;
    # extended to 6 families; then realized SQLite can't ALTER a CHECK without
    # a table rebuild and we don't want a CHECK at all (app-layer validation
    # via db.VALID_FAMILIES is enough). This migration detects the legacy
    # CHECK and rebuilds the table without it. Idempotent: no-op if the
    # CHECK is already gone.
    schema_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
    ).fetchone()
    if schema_sql and "CHECK (document_family" in (schema_sql[0] or ""):
        logger.info("schema.migrate.rebuild_documents_drop_check")
        # CRITICAL: turn off FK enforcement before the table rebuild. The
        # `chunks` table has FOREIGN KEY (document_id) REFERENCES documents,
        # so `DROP TABLE documents` with FKs ON would fail on any legacy DB
        # that already has chunks indexed (which is every production DB by
        # the time this migration runs). PRAGMA foreign_keys is a no-op
        # inside a transaction, hence it must be set BEFORE BEGIN and
        # restored AFTER COMMIT. Per SQLite docs, this is the canonical
        # table-rebuild pattern. We also call foreign_key_check after the
        # rebuild to verify referential integrity wasn't broken.
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript(
                """
                BEGIN;
                CREATE TABLE documents_new (
                    document_id      TEXT PRIMARY KEY,
                    md_id            TEXT NOT NULL,
                    title            TEXT NOT NULL,
                    detail_url       TEXT NOT NULL,
                    last_updated_at  TEXT,
                    issued_date      TEXT,
                    rbi_ref          TEXT,
                    department       TEXT,
                    document_family  TEXT NOT NULL DEFAULT 'master_direction',
                    pdf_urls_json    TEXT NOT NULL DEFAULT '[]',
                    status           TEXT NOT NULL DEFAULT 'current'
                                       CHECK (status IN ('current', 'withdrawn', 'superseded', 'draft', 'unknown')),
                    first_seen_at    TEXT NOT NULL,
                    last_seen_at     TEXT NOT NULL,
                    raw_list_sha256  TEXT,
                    UNIQUE (md_id, document_family)
                );
                INSERT INTO documents_new (
                    document_id, md_id, title, detail_url, last_updated_at,
                    issued_date, rbi_ref, department, document_family, pdf_urls_json,
                    status, first_seen_at, last_seen_at, raw_list_sha256
                )
                SELECT
                    document_id, md_id, title, detail_url, last_updated_at,
                    issued_date, rbi_ref, department, document_family, pdf_urls_json,
                    status, first_seen_at, last_seen_at, raw_list_sha256
                FROM documents;
                DROP TABLE documents;
                ALTER TABLE documents_new RENAME TO documents;
                CREATE INDEX IF NOT EXISTS idx_documents_md_id ON documents(md_id);
                CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
                CREATE INDEX IF NOT EXISTS idx_documents_family ON documents(document_family);
                COMMIT;
                """
            )
            # Verify referential integrity survived the rebuild. If any
            # chunks now point at missing documents, log loudly — the data
            # is still accessible (FKs are only enforced when ON), but the
            # corpus has a real consistency problem worth surfacing.
            orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
            if orphans:
                logger.error(
                    "schema.migrate.fk_check_orphans",
                    count=len(orphans),
                    sample=[dict(r) for r in orphans[:3]],
                )
        except sqlite3.OperationalError as exc:
            logger.error("schema.migrate.rebuild_failed", error=str(exc))
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ROLLBACK")
        finally:
            # Restore FK enforcement regardless of outcome. SCHEMA_SQL has
            # already executed `PRAGMA foreign_keys = ON` once, but on an
            # exception that pragma may have been clobbered — be explicit.
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("PRAGMA foreign_keys = ON")

    # Migration v3: drop column-level UNIQUE on md_id; replace with
    # UNIQUE(md_id, document_family). Codex outside-voice review caught that
    # globally-unique md_id silently overwrites cross-family collisions
    # (master_direction id=12922 vs press_release prid=12922 would land in
    # the same row). Fresh DBs created from SCHEMA_SQL above already have
    # the compound key; this rebuild only fires on legacy DBs whose schema
    # still has the column-level UNIQUE.
    schema_sql_v3 = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
    ).fetchone()
    if schema_sql_v3 and "md_id            TEXT NOT NULL UNIQUE" in (schema_sql_v3[0] or ""):
        logger.info("schema.migrate.rebuild_documents_compound_unique")
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript(
                """
                BEGIN;
                CREATE TABLE documents_new (
                    document_id      TEXT PRIMARY KEY,
                    md_id            TEXT NOT NULL,
                    title            TEXT NOT NULL,
                    detail_url       TEXT NOT NULL,
                    last_updated_at  TEXT,
                    issued_date      TEXT,
                    rbi_ref          TEXT,
                    department       TEXT,
                    document_family  TEXT NOT NULL DEFAULT 'master_direction',
                    pdf_urls_json    TEXT NOT NULL DEFAULT '[]',
                    status           TEXT NOT NULL DEFAULT 'current'
                                       CHECK (status IN ('current', 'withdrawn', 'superseded', 'draft', 'unknown')),
                    first_seen_at    TEXT NOT NULL,
                    last_seen_at     TEXT NOT NULL,
                    raw_list_sha256  TEXT,
                    UNIQUE (md_id, document_family)
                );
                INSERT INTO documents_new (
                    document_id, md_id, title, detail_url, last_updated_at,
                    issued_date, rbi_ref, department, document_family, pdf_urls_json,
                    status, first_seen_at, last_seen_at, raw_list_sha256
                )
                SELECT
                    document_id, md_id, title, detail_url, last_updated_at,
                    issued_date, rbi_ref, department, document_family, pdf_urls_json,
                    status, first_seen_at, last_seen_at, raw_list_sha256
                FROM documents;
                DROP TABLE documents;
                ALTER TABLE documents_new RENAME TO documents;
                CREATE INDEX IF NOT EXISTS idx_documents_md_id ON documents(md_id);
                CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
                CREATE INDEX IF NOT EXISTS idx_documents_family ON documents(document_family);
                COMMIT;
                """
            )
            orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
            if orphans:
                logger.error(
                    "schema.migrate.v3.fk_check_orphans",
                    count=len(orphans),
                    sample=[dict(r) for r in orphans[:3]],
                )
        except sqlite3.OperationalError as exc:
            logger.error("schema.migrate.v3.rebuild_failed", error=str(exc))
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ROLLBACK")
        finally:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("PRAGMA foreign_keys = ON")


def init_db(db_path: str | Path) -> None:
    """Initialize the schema at db_path.

    Useful for first-run setup outside a context manager.
    """
    with connect(db_path):
        pass
    logger.info("db.init", path=str(db_path))


# ---------------------------------------------------------------------------
# corpus_meta helpers (v0.7+)
#
# A tiny KV table for corpus-level facts that the runtime needs to verify
# before reading: schema version, embedding model identity, build timestamp,
# build commit. Used by the v0.7.1 fetch script to refuse mismatches
# rather than silently misbehave on a corpus newer/older than this runtime.
# ---------------------------------------------------------------------------


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a corpus_meta row. Stamps `updated_at` to now (UTC, ISO 8601)."""
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        """
        INSERT INTO corpus_meta (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, now),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the value for `key`, or None if not present."""
    row = conn.execute("SELECT value FROM corpus_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _stamp_schema_version(conn: sqlite3.Connection) -> None:
    """Write the current CORPUS_SCHEMA_VERSION only when missing.

    Read-first to avoid the UPSERT-bumps-updated_at-on-every-open pattern
    that would turn every read-path connection into a writer. That matters
    because:
      - readers on read-only mounts (a sane self-host pattern) would
        crash with `OperationalError: attempt to write a readonly database`
      - WAL writers contend under concurrent stdio invocations
      - the value almost never changes (only on a schema bump), so the
        write-on-every-open was strictly cost without benefit

    Bump the CORPUS_SCHEMA_VERSION constant when a schema change breaks
    runtime backward-compat; existing corpora carrying an older value get
    overwritten on the first write-allowed open after a build.
    """
    existing = get_meta(conn, "schema_version")
    if existing == CORPUS_SCHEMA_VERSION:
        return
    try:
        set_meta(conn, "schema_version", CORPUS_SCHEMA_VERSION)
    except sqlite3.OperationalError as exc:
        # Read-only mount path: don't crash the open. The corpus stamp is
        # a build-time concern; runtime can run without it.
        logger.debug("db.schema_version.write_skip", error=str(exc))


# ---------------------------------------------------------------------------
# Lookups powering rbi_check_current
# ---------------------------------------------------------------------------


def find_md_by_id(
    conn: sqlite3.Connection,
    md_id: str,
    *,
    family: str = "master_direction",
) -> sqlite3.Row | None:
    """Return the documents row for an md_id within a given family, or None.

    md_id is unique only WITHIN a family (compound UNIQUE(md_id, document_family)),
    so the caller must say which family it's looking up. Defaults to
    `master_direction` because that's how check_current.py uses it (the
    URL pattern that triggers this lookup is family-specific).
    """
    cur = conn.execute(
        "SELECT * FROM documents WHERE md_id = ? AND document_family = ?",
        (md_id, family),
    )
    return cur.fetchone()


def find_withdrawn_by_original_id(
    conn: sqlite3.Connection, original_id: str
) -> sqlite3.Row | None:
    """Return the withdrawn row for an original circular ID, or None."""
    cur = conn.execute(
        "SELECT * FROM withdrawn WHERE original_id = ? ORDER BY first_seen_at DESC LIMIT 1",
        (original_id,),
    )
    return cur.fetchone()


def find_withdrawn_by_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row | None:
    """Return the withdrawn row for a textual RBI ref, or None.

    Used at v1.0 when textual-ref parsing ships in `check_current`. v0.1 calls
    this for forward-compatibility but the path is currently unused.
    """
    cur = conn.execute(
        "SELECT * FROM withdrawn WHERE original_ref = ? ORDER BY first_seen_at DESC LIMIT 1",
        (ref,),
    )
    return cur.fetchone()


# ---------------------------------------------------------------------------
# Search (v0.1.5: FTS5 only; v0.5+ adds dense via sqlite-vec)
# ---------------------------------------------------------------------------


def search_chunks_fts(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 5,
    md_id: str | None = None,
    include_withdrawn: bool = False,
) -> list[sqlite3.Row]:
    """FTS5 search over chunks.

    Returns up to `limit` rows ordered by BM25 rank (lower is better).
    `query` is passed to FTS5 directly; callers should escape user input
    with `_escape_fts5_query` to avoid syntax errors on punctuation.

    `include_withdrawn` is reserved for when we attach status to chunks via
    the documents table at query time. v0.1.5 only indexes current MDs, so
    everything returned is current.
    """
    sql = """
        SELECT
            c.chunk_id,
            c.document_id,
            c.md_id,
            c.rbi_ref,
            c.section,
            c.paragraph_anchor,
            c.page,
            c.text,
            c.pdf_url,
            d.title          AS document_title,
            d.detail_url     AS document_url,
            d.last_updated_at,
            d.status,
            bm25(chunks_fts) AS bm25_score
        FROM chunks_fts
        JOIN chunks    c ON c.rowid = chunks_fts.rowid
        LEFT JOIN documents d ON d.document_id = c.document_id
        WHERE chunks_fts MATCH ?
    """
    params: list[object] = [query]
    if md_id is not None:
        sql += " AND c.md_id = ?"
        params.append(md_id)
    if not include_withdrawn:
        sql += " AND COALESCE(d.status, 'current') = 'current'"
    sql += " ORDER BY bm25(chunks_fts) ASC LIMIT ?"
    params.append(limit)
    cur = conn.execute(sql, params)
    return cur.fetchall()


def search_chunks_vec(
    conn: sqlite3.Connection,
    query_embedding: bytes,
    *,
    limit: int = 30,
    md_id: str | None = None,
    include_withdrawn: bool = False,
) -> list[sqlite3.Row]:
    """Dense vector search over chunks_vec.

    `query_embedding` is the float32 vector serialized via sqlite_vec.serialize_float32.
    Returns up to `limit` rows ordered by L2 distance ascending (closer = better).

    Fails silently with an empty list if chunks_vec is missing (extension didn't
    load, or no chunks have been embedded yet) so callers can fall back to
    sparse-only retrieval.
    """
    sql = """
        SELECT
            c.chunk_id,
            c.document_id,
            c.md_id,
            c.rbi_ref,
            c.section,
            c.paragraph_anchor,
            c.page,
            c.text,
            c.pdf_url,
            d.title          AS document_title,
            d.detail_url     AS document_url,
            d.last_updated_at,
            d.status,
            v.distance       AS vec_distance
        FROM chunks_vec v
        JOIN chunks    c ON c.rowid = v.rowid
        LEFT JOIN documents d ON d.document_id = c.document_id
        WHERE v.embedding MATCH ? AND k = ?
    """
    params: list[object] = [query_embedding, limit]
    # Vec0 doesn't natively support filtering on joined columns inside its
    # MATCH clause, so we filter after the K-NN retrieval. Pull more
    # candidates (k = limit * over) to compensate for filtered drops.
    sql += " ORDER BY v.distance ASC"
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError as exc:
        logger.warning("vec_search.unavailable", error=str(exc))
        return []
    rows = cur.fetchall()
    if md_id is not None:
        rows = [r for r in rows if r["md_id"] == md_id]
    if not include_withdrawn:
        rows = [r for r in rows if (r["status"] or "current") == "current"]
    return rows[:limit]


def hybrid_search(
    conn: sqlite3.Connection,
    fts_query: str,
    query_embedding: bytes | None,
    *,
    limit: int = 5,
    md_id: str | None = None,
    include_withdrawn: bool = False,
    fetch_per_side: int = 30,
    rrf_k: int = 60,
) -> list[dict]:
    """Hybrid search: FTS5 (sparse) + sqlite-vec (dense), fused via RRF.

    Reciprocal Rank Fusion (k=60 by default — Cormack et al., 2009): a robust
    fusion that doesn't require score normalization between heterogeneous
    rankers. For each candidate doc, score = sum over rankers of 1/(k+rank).

    If `query_embedding` is None or chunks_vec is empty, falls back to
    FTS5-only ranking. If FTS5 returns no results, falls back to vec-only.

    Returns up to `limit` rows as plain dicts (sqlite3.Row doesn't survive
    the fusion merge cleanly), each carrying a `fusion_score` plus the
    underlying `bm25_score` and `vec_distance` (whichever applied).
    """
    sparse = search_chunks_fts(
        conn,
        fts_query,
        limit=fetch_per_side,
        md_id=md_id,
        include_withdrawn=include_withdrawn,
    )
    dense: list[sqlite3.Row] = []
    if query_embedding is not None:
        dense = search_chunks_vec(
            conn,
            query_embedding,
            limit=fetch_per_side,
            md_id=md_id,
            include_withdrawn=include_withdrawn,
        )

    # Fast path: only one side returned anything.
    if not dense and not sparse:
        return []
    if not dense:
        return [_row_to_dict(r, sparse_rank=i + 1, dense_rank=None) for i, r in enumerate(sparse[:limit])]
    if not sparse:
        return [_row_to_dict(r, sparse_rank=None, dense_rank=i + 1) for i, r in enumerate(dense[:limit])]

    # Build RRF fusion. Key by chunk_id since same chunk may appear in both.
    sparse_rank: dict[str, int] = {r["chunk_id"]: i + 1 for i, r in enumerate(sparse)}
    dense_rank: dict[str, int] = {r["chunk_id"]: i + 1 for i, r in enumerate(dense)}
    rows_by_id: dict[str, sqlite3.Row] = {}
    for r in sparse:
        rows_by_id[r["chunk_id"]] = r
    for r in dense:
        rows_by_id.setdefault(r["chunk_id"], r)

    scored: list[tuple[float, str]] = []
    for chunk_id in rows_by_id:
        s = 0.0
        if chunk_id in sparse_rank:
            s += 1.0 / (rrf_k + sparse_rank[chunk_id])
        if chunk_id in dense_rank:
            s += 1.0 / (rrf_k + dense_rank[chunk_id])
        scored.append((s, chunk_id))
    scored.sort(reverse=True)

    out: list[dict] = []
    for fusion_score, chunk_id in scored[:limit]:
        row = rows_by_id[chunk_id]
        out.append(
            _row_to_dict(
                row,
                sparse_rank=sparse_rank.get(chunk_id),
                dense_rank=dense_rank.get(chunk_id),
                fusion_score=fusion_score,
            )
        )
    return out


def _row_to_dict(
    row: sqlite3.Row,
    *,
    sparse_rank: int | None,
    dense_rank: int | None,
    fusion_score: float | None = None,
) -> dict:
    """Coalesce a sparse OR dense result row into the unified dict shape."""
    keys = row.keys()
    return {
        "chunk_id": row["chunk_id"],
        "document_id": row["document_id"],
        "md_id": row["md_id"],
        "rbi_ref": row["rbi_ref"],
        "section": row["section"],
        "paragraph_anchor": row["paragraph_anchor"],
        "page": row["page"],
        "text": row["text"],
        "pdf_url": row["pdf_url"],
        "document_title": row["document_title"],
        "document_url": row["document_url"],
        "last_updated_at": row["last_updated_at"],
        "status": row["status"],
        "bm25_score": row["bm25_score"] if "bm25_score" in keys else None,
        "vec_distance": row["vec_distance"] if "vec_distance" in keys else None,
        "sparse_rank": sparse_rank,
        "dense_rank": dense_rank,
        "fusion_score": fusion_score,
    }


def escape_fts5_query(text: str) -> str:
    """Make user text safe to pass to FTS5's MATCH operator.

    FTS5 has its own query language (AND, OR, NOT, NEAR, etc.); arbitrary
    text from a clause/PRD will trip syntax errors. Strategy:
        1. Tokenize on word boundaries.
        2. Drop tokens that are pure punctuation or empty.
        3. Quote each remaining token to prevent operator interpretation.
        4. Join with spaces (implicit AND of phrases by FTS5 default).
    """
    import re

    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    if not tokens:
        return '""'  # FTS5 will return zero rows; safer than crashing.
    # Cap query length to avoid pathological inputs; 32 tokens is plenty for
    # a clause-level compliance check.
    tokens = tokens[:32]
    quoted = [f'"{t}"' for t in tokens]
    return " OR ".join(quoted)


# ---------------------------------------------------------------------------
# Upserts (used by the refresh pipeline)
# ---------------------------------------------------------------------------


def upsert_md(
    conn: sqlite3.Connection,
    md_id: str,
    *,
    title: str,
    detail_url: str,
    last_updated_at: str | None,
    department: str | None,
    pdf_urls: list[str],
    status: str = "current",
    now: str,
    raw_list_sha256: str | None = None,
) -> None:
    """Insert or update a Master Direction row.

    `now` should be the crawl's ISO 8601 UTC timestamp; first_seen_at is set
    only on insert, last_seen_at always updated.
    """
    import json

    conn.execute(
        """
        INSERT INTO documents (
            document_id, md_id, title, detail_url, last_updated_at, department,
            pdf_urls_json, status, first_seen_at, last_seen_at, raw_list_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(md_id, document_family) DO UPDATE SET
            title = excluded.title,
            detail_url = excluded.detail_url,
            last_updated_at = COALESCE(excluded.last_updated_at, documents.last_updated_at),
            department = COALESCE(excluded.department, documents.department),
            pdf_urls_json = excluded.pdf_urls_json,
            status = excluded.status,
            last_seen_at = excluded.last_seen_at,
            raw_list_sha256 = excluded.raw_list_sha256
        """,
        (
            f"rbi:master_direction:{md_id}",
            md_id,
            title,
            detail_url,
            last_updated_at,
            department,
            json.dumps(pdf_urls),
            status,
            now,
            now,
            raw_list_sha256,
        ),
    )


def upsert_withdrawn(
    conn: sqlite3.Connection,
    *,
    original_ref: str | None,
    original_id: str | None,
    title: str,
    issued_date: str | None,
    withdrawn_date: str | None,
    replacement_ref: str | None,
    detail_url: str | None,
    now: str,
) -> None:
    """Insert or update a withdrawn-circular row.

    Identity is `(original_id, title)` when original_id is known, else
    `(original_ref, title)`. If neither is known, falls back to `(title,)`.
    """
    if original_id:
        existing = conn.execute(
            "SELECT id FROM withdrawn WHERE original_id = ?",
            (original_id,),
        ).fetchone()
    elif original_ref:
        existing = conn.execute(
            "SELECT id FROM withdrawn WHERE original_ref = ? AND title = ?",
            (original_ref, title),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id FROM withdrawn WHERE title = ? AND original_id IS NULL AND original_ref IS NULL",
            (title,),
        ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE withdrawn SET
                original_ref = COALESCE(?, original_ref),
                original_id = COALESCE(?, original_id),
                title = ?,
                issued_date = COALESCE(?, issued_date),
                withdrawn_date = COALESCE(?, withdrawn_date),
                replacement_ref = COALESCE(?, replacement_ref),
                detail_url = COALESCE(?, detail_url),
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                original_ref,
                original_id,
                title,
                issued_date,
                withdrawn_date,
                replacement_ref,
                detail_url,
                now,
                existing["id"],
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO withdrawn (
                original_ref, original_id, title, issued_date, withdrawn_date,
                replacement_ref, detail_url, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                original_ref,
                original_id,
                title,
                issued_date,
                withdrawn_date,
                replacement_ref,
                detail_url,
                now,
                now,
            ),
        )


def record_crawl_run(
    conn: sqlite3.Connection,
    *,
    started_at: str,
    finished_at: str,
    source: str,
    source_url: str,
    status_code: int | None,
    raw_html_sha256: str | None,
    items_seen: int,
    items_added: int = 0,
    items_changed: int = 0,
    items_removed: int = 0,
    error: str | None = None,
) -> int:
    """Append a row to crawl_runs. Returns the inserted row id."""
    cur = conn.execute(
        """
        INSERT INTO crawl_runs (
            started_at, finished_at, source, source_url, status_code,
            raw_html_sha256, items_seen, items_added, items_changed,
            items_removed, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at,
            finished_at,
            source,
            source_url,
            status_code,
            raw_html_sha256,
            items_seen,
            items_added,
            items_changed,
            items_removed,
            error,
        ),
    )
    return int(cur.lastrowid or 0)
