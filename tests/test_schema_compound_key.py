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
