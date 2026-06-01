"""Tests for the documents-table compound UNIQUE(md_id, document_family).

Codex outside-voice review (2026-04-28) caught that the original column-level
UNIQUE(md_id) silently overwrote cross-family collisions: a Master Direction
with id=12922 and a Press Release with prid=12922 both claim md_id='12922'
and the second upsert would clobber the first. Fix: compound UNIQUE on
(md_id, document_family). These tests guard the new invariant.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from rbi_source_mcp.db import connect, find_md_by_id


def _insert(conn: sqlite3.Connection, *, document_id: str, md_id: str, family: str) -> None:
    """Minimal insert helper that exercises the compound key."""
    conn.execute(
        """
        INSERT INTO documents (
            document_id, md_id, title, detail_url, document_family,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            md_id,
            f"title-{document_id}",
            f"https://example/{document_id}",
            family,
            "2026-04-28T00:00:00Z",
            "2026-04-28T00:00:00Z",
        ),
    )


def test_same_md_id_allowed_across_different_families() -> None:
    """The whole point of the compound key: id=12922 in master_direction
    and id=12922 in press_release are distinct documents and must coexist."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "compound.sqlite"
        with connect(db_path) as conn:
            _insert(
                conn,
                document_id="rbi:master_direction:12922",
                md_id="12922",
                family="master_direction",
            )
            _insert(
                conn,
                document_id="rbi:press_release:12922",
                md_id="12922",
                family="press_release",
            )
            rows = conn.execute(
                "SELECT document_family FROM documents WHERE md_id = ?", ("12922",)
            ).fetchall()
            families = {r["document_family"] for r in rows}
            assert families == {"master_direction", "press_release"}


def test_same_md_id_within_same_family_rejected() -> None:
    """Within a single family, md_id is still unique. Two MDs with id=12922
    is a real conflict (same canonical RBI document) and must raise."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "compound.sqlite"
        with connect(db_path) as conn:
            _insert(
                conn,
                document_id="rbi:master_direction:12922",
                md_id="12922",
                family="master_direction",
            )
            with pytest.raises(sqlite3.IntegrityError):
                _insert(
                    conn,
                    document_id="rbi:master_direction:12922-dup",
                    md_id="12922",
                    family="master_direction",
                )


def test_find_md_by_id_is_family_scoped() -> None:
    """find_md_by_id must return the row for the requested family, even if
    a sibling family has the same md_id."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "compound.sqlite"
        with connect(db_path) as conn:
            _insert(
                conn,
                document_id="rbi:master_direction:12922",
                md_id="12922",
                family="master_direction",
            )
            _insert(
                conn,
                document_id="rbi:press_release:12922",
                md_id="12922",
                family="press_release",
            )
            md_row = find_md_by_id(conn, "12922", family="master_direction")
            pr_row = find_md_by_id(conn, "12922", family="press_release")
            assert md_row is not None
            assert pr_row is not None
            assert md_row["document_id"] == "rbi:master_direction:12922"
            assert pr_row["document_id"] == "rbi:press_release:12922"
            # Default family is master_direction (matches check_current.py usage)
            default_row = find_md_by_id(conn, "12922")
            assert default_row is not None
            assert default_row["document_family"] == "master_direction"


def _build_legacy_v2_documents(db_path: Path, *, md_id_decl: str) -> None:
    """Construct a legacy v2-style documents table manually.

    `md_id_decl` is the exact column-declaration text (e.g. `"md_id TEXT NOT NULL UNIQUE"`
    or `"md_id\\tTEXT NOT NULL UNIQUE"`). This lets us exercise the v3
    detector against whitespace variants that the old literal-substring
    detector silently missed.
    """
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(
            f"""
            CREATE TABLE documents (
                document_id      TEXT PRIMARY KEY,
                {md_id_decl},
                title            TEXT NOT NULL,
                detail_url       TEXT NOT NULL,
                last_updated_at  TEXT,
                issued_date      TEXT,
                rbi_ref          TEXT,
                department       TEXT,
                document_family  TEXT NOT NULL DEFAULT 'master_direction',
                pdf_urls_json    TEXT NOT NULL DEFAULT '[]',
                status           TEXT NOT NULL DEFAULT 'current',
                first_seen_at    TEXT NOT NULL,
                last_seen_at     TEXT NOT NULL,
                raw_list_sha256  TEXT
            );
            INSERT INTO documents (
                document_id, md_id, title, detail_url, document_family,
                first_seen_at, last_seen_at
            ) VALUES (
                'rbi:master_direction:42',
                '42',
                'A legacy MD row',
                'https://example/42',
                'master_direction',
                '2026-01-01T00:00:00Z',
                '2026-01-01T00:00:00Z'
            );
            """
        )
        raw.commit()
    finally:
        raw.close()


@pytest.mark.parametrize(
    "md_id_decl",
    [
        # Original literal the old detector matched (12 spaces).
        "md_id            TEXT NOT NULL UNIQUE",
        # Single space — would have failed substring match.
        "md_id TEXT NOT NULL UNIQUE",
        # Tab whitespace — would have failed substring match.
        "md_id\tTEXT\tNOT NULL\tUNIQUE",
        # Mixed whitespace.
        "md_id  TEXT  NOT NULL  UNIQUE",
    ],
)
def test_v3_detector_triggers_on_whitespace_variants(
    md_id_decl: str, tmp_path: Path
) -> None:
    """The v3 detector must be DDL-whitespace-insensitive.

    Regression: prior to PR 1, the detector matched only the exact 12-space
    literal `'md_id            TEXT NOT NULL UNIQUE'`. A legacy DB created by
    any tool that emitted different whitespace would silently skip the
    migration and keep the cross-family overwrite hazard the migration exists
    to fix.
    """
    db_path = tmp_path / "legacy_v2.sqlite"
    _build_legacy_v2_documents(db_path, md_id_decl=md_id_decl)

    # Sanity: legacy DB literally has the column-level UNIQUE index on md_id.
    raw = sqlite3.connect(db_path)
    try:
        idxs = raw.execute("PRAGMA index_list(documents)").fetchall()
        unique_single_col_idxs = []
        for _seq, name, is_unique, *_ in idxs:
            if not is_unique:
                continue
            cols = [r[2] for r in raw.execute(f"PRAGMA index_info({name!r})").fetchall()]
            if cols == ["md_id"]:
                unique_single_col_idxs.append(name)
        assert unique_single_col_idxs, "fixture didn't create a unique-on-md_id index"
    finally:
        raw.close()

    # Open through the production connect() — should trigger the v3 rebuild.
    with connect(db_path) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()[0]
        assert "UNIQUE (md_id, document_family)" in sql
        # And the legacy column-level UNIQUE is gone.
        assert "NOT NULL UNIQUE" not in sql.replace("UNIQUE (md_id, document_family)", "")

        # Data must survive the rebuild.
        row = conn.execute(
            "SELECT document_id, md_id FROM documents WHERE md_id = ?",
            ("42",),
        ).fetchone()
        assert row is not None
        assert row["document_id"] == "rbi:master_direction:42"


def test_compound_unique_constraint_is_in_schema() -> None:
    """Verify the schema literally contains the compound UNIQUE — not just
    that inserts behave as if it does."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "compound.sqlite"
        with connect(db_path) as conn:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
            ).fetchone()[0]
            assert "UNIQUE (md_id, document_family)" in sql
            # And the old column-level UNIQUE is gone.
            assert "md_id            TEXT NOT NULL UNIQUE" not in sql
