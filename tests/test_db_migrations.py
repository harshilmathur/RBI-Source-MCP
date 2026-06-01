"""Tests for the schema rebuild migrations in db._migrate_schema.

Two rebuild migrations exist:

- **v2 (drop CHECK on document_family)**: triggered when a legacy DB
  carries `CHECK (document_family ...)` in its CREATE TABLE DDL.
- **v3 (compound UNIQUE on (md_id, document_family))**: triggered when
  the legacy DB has a column-level UNIQUE index on `md_id` alone. PR 1
  rewrote the detector to be semantic (PRAGMA index_list) rather than
  whitespace-fragile.

Both rebuilds are critical: they preserve every existing row, and they
restore foreign-key integrity (via PRAGMA foreign_key_check) after the
table is dropped and recreated. A regression would either lose data or
leave dangling chunk rows pointing at vanished documents.

These tests construct legacy DBs by hand and run the production
`connect()` against them, asserting the rebuild fires and data survives.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rbi_source_mcp.db import connect


def _build_legacy_v1_documents(db_path: Path) -> None:
    """Construct a v1-style documents table with the legacy CHECK constraint.

    Mirrors the schema shape from commit 00e01c7 — column-level UNIQUE on
    md_id AND a CHECK constraint on document_family. Triggers BOTH the
    v2 (drop CHECK) and v3 (compound UNIQUE) rebuilds.
    """
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(
            """
            CREATE TABLE documents (
                document_id      TEXT PRIMARY KEY,
                md_id            TEXT NOT NULL UNIQUE,
                title            TEXT NOT NULL,
                detail_url       TEXT NOT NULL,
                last_updated_at  TEXT,
                issued_date      TEXT,
                rbi_ref          TEXT,
                department       TEXT,
                document_family  TEXT NOT NULL DEFAULT 'master_direction'
                                   CHECK (document_family IN ('master_direction', 'standalone_circular', 'notification', 'press_release')),
                pdf_urls_json    TEXT NOT NULL DEFAULT '[]',
                status           TEXT NOT NULL DEFAULT 'current',
                first_seen_at    TEXT NOT NULL,
                last_seen_at     TEXT NOT NULL,
                raw_list_sha256  TEXT
            );
            INSERT INTO documents (
                document_id, md_id, title, detail_url, document_family,
                first_seen_at, last_seen_at
            ) VALUES
                ('rbi:master_direction:111', '111', 'MD title 1',
                 'https://example.invalid/111', 'master_direction',
                 '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
                ('rbi:master_direction:222', '222', 'MD title 2',
                 'https://example.invalid/222', 'master_direction',
                 '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
                ('rbi:press_release:333', '333', 'PR title 3',
                 'https://example.invalid/333', 'press_release',
                 '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """
        )
        raw.commit()
    finally:
        raw.close()


def test_v1_to_current_migration_preserves_all_rows(tmp_path: Path) -> None:
    """Open a v1-shape legacy DB; the rebuild must fire (CHECK + UNIQUE) and
    every row must survive with no corruption."""
    db_path = tmp_path / "legacy_v1.sqlite"
    _build_legacy_v1_documents(db_path)

    # Sanity: legacy DB has both the CHECK and the column-level UNIQUE.
    raw = sqlite3.connect(db_path)
    try:
        legacy_sql = raw.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()[0]
        assert "CHECK (document_family" in legacy_sql
        assert "NOT NULL UNIQUE" in legacy_sql
    finally:
        raw.close()

    # Run the production migration path.
    with connect(db_path) as conn:
        # After migration: no CHECK, compound UNIQUE.
        new_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()[0]
        assert "CHECK (document_family" not in new_sql, "legacy CHECK must be gone"
        assert "UNIQUE (md_id, document_family)" in new_sql, "compound UNIQUE missing"

        # Every original row must be preserved.
        rows = conn.execute(
            "SELECT document_id, md_id, document_family FROM documents ORDER BY document_id"
        ).fetchall()
        assert len(rows) == 3
        ids = [r["document_id"] for r in rows]
        assert ids == [
            "rbi:master_direction:111",
            "rbi:master_direction:222",
            "rbi:press_release:333",
        ]


def test_rebuild_preserves_chunk_foreign_keys(tmp_path: Path) -> None:
    """The migration runs `PRAGMA foreign_keys = OFF` before DROP TABLE
    and reports any orphan chunks via foreign_key_check. Pin both halves
    of that contract: (a) chunks survive the rebuild, (b) they still
    point at their documents after the rebuild."""
    db_path = tmp_path / "legacy_with_chunks.sqlite"
    _build_legacy_v1_documents(db_path)

    # Insert a chunk row pointing at the MD via document_id (the real prod
    # schema has a FOREIGN KEY here; we add it loosely so the test mirrors
    # the production constraint surface).
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(
            """
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                md_id TEXT NOT NULL,
                text TEXT NOT NULL,
                FOREIGN KEY (document_id) REFERENCES documents(document_id)
            );
            INSERT INTO chunks (chunk_id, document_id, md_id, text)
            VALUES ('c:1', 'rbi:master_direction:111', '111', 'section 1 text'),
                   ('c:2', 'rbi:press_release:333', '333', 'section 1 of PR');
            """
        )
        raw.commit()
    finally:
        raw.close()

    with connect(db_path) as conn:
        # Documents preserved.
        doc_ids = [
            r["document_id"]
            for r in conn.execute(
                "SELECT document_id FROM documents ORDER BY document_id"
            )
        ]
        assert "rbi:master_direction:111" in doc_ids
        assert "rbi:press_release:333" in doc_ids

        # Chunks still resolve to their documents — no orphans.
        orphans = conn.execute(
            """
            SELECT c.chunk_id
            FROM chunks c
            LEFT JOIN documents d ON d.document_id = c.document_id
            WHERE d.document_id IS NULL
            """
        ).fetchall()
        assert orphans == [], (
            f"chunk rows orphaned after migration rebuild: {[r['chunk_id'] for r in orphans]}"
        )


def test_v3_detector_ignores_manual_unique_index_on_current_schema(
    tmp_path: Path,
) -> None:
    """An operator who manually runs `CREATE UNIQUE INDEX ... ON
    documents(md_id)` on an already-v3 table must NOT trigger a spurious
    rebuild.

    Adversarial review (post-PR 4): the v3 detector matched any unique
    single-column index on `md_id`, including user-created ones. Origin
    'u' (auto-index from column-level UNIQUE) is the only legitimate
    legacy-v2 signal; 'c' (CREATE INDEX) is a deliberate operator choice
    and the rebuild would silently drop it.
    """
    db_path = tmp_path / "manual_index.sqlite"
    # Build the canonical v3 schema fresh.
    with connect(db_path) as conn:
        sql_before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()[0]
        assert "UNIQUE (md_id, document_family)" in sql_before

        # Operator manually adds a redundant unique index on md_id alone.
        conn.execute(
            "CREATE UNIQUE INDEX manual_md_id_unique ON documents(md_id)"
        )

    # Re-open: the detector must NOT trigger a rebuild. The table SQL
    # must remain the v3 compound-UNIQUE schema verbatim.
    with connect(db_path) as conn:
        sql_after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()[0]
        assert sql_after == sql_before, (
            "v3 detector falsely re-fired on an already-migrated table"
            " just because an operator added a manual UNIQUE INDEX on md_id"
        )
        # And the manual index survives.
        manual = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='manual_md_id_unique'"
        ).fetchone()
        assert manual is not None, "operator's manual index was dropped by spurious rebuild"


def test_fresh_db_skips_legacy_migrations(tmp_path: Path) -> None:
    """A fresh DB built from SCHEMA_SQL must NOT trigger either rebuild
    (no CHECK, no column-level UNIQUE → no work needed). The connect()
    path must be idempotent on already-current schemas."""
    db_path = tmp_path / "fresh.sqlite"

    # First open creates the schema.
    with connect(db_path) as conn:
        sql_after_create = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()[0]

    # Re-open: schema must be unchanged byte-for-byte.
    with connect(db_path) as conn:
        sql_after_reopen = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()[0]

    assert sql_after_create == sql_after_reopen, (
        "fresh DB schema must be stable across re-opens — migrations are"
        " not supposed to re-run on already-current schemas"
    )
