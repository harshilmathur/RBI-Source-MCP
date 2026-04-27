"""SQLite schema and helpers for the RBI Source MCP corpus.

v0.1 schema is intentionally minimal: just enough to power `check_current`
end-to-end. v1.0 adds chunks, FTS5 index, and sqlite-vec embeddings.

Schema:
    documents          -- canonical MD records from the list crawl
    withdrawn          -- withdrawal records (corpus + URL-based lookup)
    crawl_runs         -- audit trail of every crawl
    extraction_runs    -- audit trail of build pipeline runs (v1.0)

The DB file is rebuilt by the weekly GitHub Action and atomic-swapped onto
the live Fly volume.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    document_id      TEXT PRIMARY KEY,    -- e.g., "rbi:master_direction:12550"
    md_id            TEXT NOT NULL UNIQUE,
    title            TEXT NOT NULL,
    detail_url       TEXT NOT NULL,
    last_updated_at  TEXT,                -- "Updated as on <date>" parsed from title; ISO 8601
    department       TEXT,                -- v0.1: always NULL (page is JS-rendered for groupings)
    pdf_urls_json    TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'current'
                       CHECK (status IN ('current', 'withdrawn', 'superseded', 'draft', 'unknown')),
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    raw_list_sha256  TEXT                 -- sha of the list page that introduced this row
);

CREATE INDEX IF NOT EXISTS idx_documents_md_id ON documents(md_id);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

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
    source            TEXT NOT NULL,        -- 'md_list' | 'withdrawn_list' | etc.
    source_url        TEXT NOT NULL,
    status_code       INTEGER,
    raw_html_sha256   TEXT,
    items_seen        INTEGER,
    items_added       INTEGER,
    items_changed     INTEGER,
    items_removed     INTEGER,
    error             TEXT
);
"""


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Context manager that opens, initializes, and closes a SQLite connection.

    The schema is applied on every open (CREATE IF NOT EXISTS makes it cheap
    and idempotent). Returns a connection with row_factory=sqlite3.Row.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA_SQL)
        yield conn
    finally:
        conn.commit()
        conn.close()


def init_db(db_path: str | Path) -> None:
    """Initialize the schema at db_path.

    Useful for first-run setup outside a context manager.
    """
    with connect(db_path):
        pass
    logger.info("db.init", path=str(db_path))


# ---------------------------------------------------------------------------
# Lookups powering rbi.check_current
# ---------------------------------------------------------------------------


def find_md_by_id(conn: sqlite3.Connection, md_id: str) -> sqlite3.Row | None:
    """Return the documents row for a Master Direction ID, or None."""
    cur = conn.execute("SELECT * FROM documents WHERE md_id = ?", (md_id,))
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
        ON CONFLICT(md_id) DO UPDATE SET
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
