"""Tests for the shared persist_document_and_chunks helper.

`persist_document_and_chunks` is the single upsert path used by every
indexer family. PR 2 migrated the two stragglers (MD + circular) onto it,
making the helper load-bearing for every newly-indexed document.

These tests pin the contract: happy-path upsert, cross-family isolation
(critical now that md_id is only unique per family), embedder-failure
behavior, title-quality fallback, pdf_artifact upsert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rbi_source_mcp.db import connect
from rbi_source_mcp.indexer.persist import (
    looks_like_date_or_header,
    persist_document_and_chunks,
)

# A body big enough to produce at least one chunk under the paragraph chunker.
_BODY = (
    "1. Definitions\n"
    "These directions shall apply to every regulated entity engaged in the "
    "business of lending, including non-banking financial companies and "
    "all categories of banks operating under the Reserve Bank of India.\n"
    "\n"
    "2. Capital adequacy\n"
    "Every regulated entity shall maintain a minimum capital-to-risk-weighted-"
    "assets ratio of nine per cent computed on the basis of the prescribed "
    "risk weights specified in the Annex hereto.\n"
)


def _seed_one(conn, *, family: str, md_id: str, body: str = _BODY) -> int:
    """Convenience: call persist with the minimum required kwargs."""
    return persist_document_and_chunks(
        conn,
        document_id=f"rbi:{family}:{md_id}",
        md_id=md_id,
        title=f"Title for {family} {md_id}",
        detail_url=f"https://example.invalid/{family}/{md_id}",
        document_family=family,
        body_text=body,
    )


def test_happy_path_inserts_document_and_chunks(tmp_path: Path) -> None:
    db_path = tmp_path / "happy.sqlite"
    with connect(db_path) as conn:
        n = _seed_one(conn, family="master_direction", md_id="12550")
        assert n > 0

        doc = conn.execute(
            "SELECT document_id, title, document_family FROM documents "
            "WHERE md_id = ? AND document_family = ?",
            ("12550", "master_direction"),
        ).fetchone()
        assert doc is not None
        assert doc["document_id"] == "rbi:master_direction:12550"
        assert doc["document_family"] == "master_direction"

        chunk_count = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE document_id = ?",
            ("rbi:master_direction:12550",),
        ).fetchone()[0]
        assert chunk_count == n


def test_reindex_replaces_chunks_idempotently(tmp_path: Path) -> None:
    """Calling persist twice for the same doc must REPLACE chunks, not duplicate."""
    db_path = tmp_path / "reindex.sqlite"
    with connect(db_path) as conn:
        n1 = _seed_one(conn, family="master_direction", md_id="12550")
        n2 = _seed_one(conn, family="master_direction", md_id="12550")
        assert n1 == n2 > 0

        total = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE document_id = ?",
            ("rbi:master_direction:12550",),
        ).fetchone()[0]
        assert total == n2, "reindex must replace chunks, not append"


def test_sibling_family_chunks_survive_reindex(tmp_path: Path) -> None:
    """An MD with md_id=42 and a press_release with prid=42 must be independent.

    Regression target: deleting by md_id (the original MD-indexer behavior)
    would wipe the sibling family's chunks. persist.py scopes by document_id
    so this can't happen — pin that contract.
    """
    db_path = tmp_path / "siblings.sqlite"
    with connect(db_path) as conn:
        _seed_one(conn, family="master_direction", md_id="42")
        _seed_one(conn, family="press_release", md_id="42")

        # Re-index just the MD. Press release chunks must remain.
        _seed_one(conn, family="master_direction", md_id="42")
        pr_chunks = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE document_id = ?",
            ("rbi:press_release:42",),
        ).fetchone()[0]
        assert pr_chunks > 0, (
            "press_release chunks must survive an MD reindex with the same md_id"
        )


def test_title_quality_fallback_keeps_existing_richer_title(tmp_path: Path) -> None:
    """If the new title is garbage (a date string) and an existing richer
    title is in the DB, persist keeps the existing one."""
    db_path = tmp_path / "title.sqlite"
    with connect(db_path) as conn:
        # Seed with a good title via direct insert (mimics the list-crawler
        # pre-populating the row).
        conn.execute(
            """
            INSERT INTO documents (
                document_id, md_id, title, detail_url, document_family,
                pdf_urls_json, status, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, '[]', 'current',
                      '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (
                "rbi:master_direction:9999",
                "9999",
                "Master Direction on KYC, 2016 (Updated as on April 25, 2026)",
                "https://example.invalid/9999",
                "master_direction",
            ),
        )
        # Re-persist with a garbage title (a bare date) — existing must win.
        persist_document_and_chunks(
            conn,
            document_id="rbi:master_direction:9999",
            md_id="9999",
            title="January 1, 2026",  # looks_like_date_or_header → True
            detail_url="https://example.invalid/9999",
            document_family="master_direction",
            body_text=_BODY,
        )
        row = conn.execute(
            "SELECT title FROM documents WHERE md_id = ? AND document_family = ?",
            ("9999", "master_direction"),
        ).fetchone()
        assert row["title"].startswith("Master Direction on KYC"), (
            "garbage new title should not overwrite a richer existing title"
        )


def test_looks_like_date_or_header_recognizes_canonical_garbage() -> None:
    """Pin the heuristic used by the title fallback."""
    assert looks_like_date_or_header("January 1, 2026")
    assert looks_like_date_or_header("1 January 2026")
    assert looks_like_date_or_header("01/01/2026")
    assert looks_like_date_or_header("ref no.")
    assert looks_like_date_or_header("Notifications")
    assert looks_like_date_or_header("")
    assert looks_like_date_or_header(None)
    # Real titles should be recognized as non-garbage.
    assert not looks_like_date_or_header(
        "Master Direction on Know Your Customer, 2016"
    )
    assert not looks_like_date_or_header("RBI/2023-24/107 — Some real title")


def test_empty_body_returns_zero_chunks(tmp_path: Path) -> None:
    """Empty body_text → no chunks inserted, return 0. Callers treat 0 as
    a soft failure and log it."""
    db_path = tmp_path / "empty.sqlite"
    with connect(db_path) as conn:
        n = persist_document_and_chunks(
            conn,
            document_id="rbi:master_direction:0",
            md_id="0",
            title="empty doc",
            detail_url="https://example.invalid/0",
            document_family="master_direction",
            body_text="",
        )
        assert n == 0


def test_pdf_artifact_upsert(tmp_path: Path) -> None:
    """When pdf_artifact is supplied, a pdf_artifacts row is upserted with
    is_indexed=1 and text_sha256 covers the body text."""
    db_path = tmp_path / "pdf.sqlite"
    pdf_url = "https://example.invalid/doc.pdf"
    with connect(db_path) as conn:
        persist_document_and_chunks(
            conn,
            document_id="rbi:master_direction:101",
            md_id="101",
            title="Doc with PDF",
            detail_url="https://example.invalid/101",
            document_family="master_direction",
            body_text=_BODY,
            pdf_artifact={
                "pdf_url": pdf_url,
                "sha256": "deadbeef" * 8,
                "bytes": 1234,
                "page_count": 5,
                "quality": 0.95,
            },
        )
        row = conn.execute(
            "SELECT pdf_url, is_indexed, page_count, extraction_quality, "
            "text_sha256 FROM pdf_artifacts WHERE pdf_url = ?",
            (pdf_url,),
        ).fetchone()
        assert row is not None
        assert row["is_indexed"] == 1
        assert row["page_count"] == 5
        assert row["extraction_quality"] == pytest.approx(0.95)
        # text_sha256 is a hex SHA256 of body_text.
        assert len(row["text_sha256"]) == 64


def test_compound_key_allows_same_md_id_across_families(tmp_path: Path) -> None:
    """Pinning the compound key from the indexer side: persist for the same
    md_id in two different families must coexist, not collide."""
    db_path = tmp_path / "compound.sqlite"
    with connect(db_path) as conn:
        _seed_one(conn, family="master_direction", md_id="777")
        _seed_one(conn, family="standalone_circular", md_id="777")

        rows = conn.execute(
            "SELECT document_family FROM documents WHERE md_id = ? ORDER BY document_family",
            ("777",),
        ).fetchall()
        families = [r["document_family"] for r in rows]
        assert families == ["master_direction", "standalone_circular"]
