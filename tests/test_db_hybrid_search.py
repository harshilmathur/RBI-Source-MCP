"""Tests for hybrid_search RRF merge behavior.

Regression target: prior to PR 1, `hybrid_search` used `setdefault` on a
single `rows_by_id` dict keyed by chunk_id. When the same chunk appeared
in both the sparse (FTS5) and dense (sqlite-vec) result lists — exactly
the case where both rankers AGREE, which is the strongest evidence —
the sparse row "won" the dict insertion and the dense row's `vec_distance`
column was silently dropped from the merged output.

That's a quiet correctness bug: `mcp/check_compliance.py` calibrates
`low_confidence` against the fused score AND `both_rankers_agree`, and
`mcp/search.py` exposes `vec_distance` in its response. Losing the dense
score on the strongest candidates degrades both surfaces.

These tests pin the new contract: both `bm25_score` and `vec_distance`
must be populated on every row that appears in both rankers, and the RRF
ordering must match the documented k=60 formula.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from rbi_source_mcp.db import connect, hybrid_search


def _f32_vec(values: list[float]) -> bytes:
    """sqlite-vec little-endian float32 wire format."""
    return struct.pack(f"<{len(values)}f", *values)


def _seed_chunk(
    conn,
    *,
    chunk_id: str,
    document_id: str,
    md_id: str,
    text: str,
    embedding: list[float] | None,
) -> None:
    cur = conn.execute(
        """
        INSERT INTO chunks (
            chunk_id, document_id, md_id, rbi_ref, section,
            paragraph_anchor, page, text, text_sha256,
            char_start, char_end, pdf_url, indexed_at
        ) VALUES (?, ?, ?, NULL, NULL, NULL, 1, ?, ?, 0, 0, NULL, '2026-01-01T00:00:00Z')
        """,
        (chunk_id, document_id, md_id, text, "x" * 64),
    )
    if embedding is not None:
        try:
            conn.execute(
                "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                (cur.lastrowid, _f32_vec(embedding)),
            )
        except Exception:  # noqa: BLE001
            # sqlite-vec extension missing or dim mismatch — caller handles skip.
            raise


def _seed_document(conn, *, document_id: str, md_id: str) -> None:
    conn.execute(
        """
        INSERT INTO documents (
            document_id, md_id, title, detail_url, document_family,
            pdf_urls_json, status, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, '[]', 'current', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (
            document_id,
            md_id,
            f"Doc {document_id}",
            f"https://example/{document_id}",
            "master_direction",
        ),
    )


def _detect_embedding_dim(conn) -> int | None:
    """Read the chunks_vec dim from the production schema (or return None
    if the sqlite-vec extension isn't loaded)."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row or not row[0]:
        return None
    import re

    m = re.search(r"float\[(\d+)\]", row[0])
    if not m:
        return None
    return int(m.group(1))


def test_chunk_in_both_rankers_keeps_vec_distance(tmp_path: Path) -> None:
    """A chunk appearing in BOTH sparse and dense results retains its
    vec_distance scalar after RRF merge."""
    db_path = tmp_path / "hybrid.sqlite"
    with connect(db_path) as conn:
        dim = _detect_embedding_dim(conn)
        if dim is None:
            pytest.skip("sqlite-vec extension not available — dense path skipped")

        _seed_document(conn, document_id="rbi:master_direction:1", md_id="1")
        _seed_document(conn, document_id="rbi:master_direction:2", md_id="2")

        # Chunk A: matches the FTS5 query AND is near the query vector.
        # Chunk B: matches the FTS5 query but is far from the query vector.
        # Chunk C: doesn't match FTS5 but is near the query vector.
        a_vec = [1.0] + [0.0] * (dim - 1)
        b_vec = [0.0, 1.0] + [0.0] * (dim - 2)
        c_vec = [0.99, 0.01] + [0.0] * (dim - 2)
        query_vec = [1.0] + [0.0] * (dim - 1)

        _seed_chunk(
            conn,
            chunk_id="chunk:a",
            document_id="rbi:master_direction:1",
            md_id="1",
            text="kyc verification customer due diligence",
            embedding=a_vec,
        )
        _seed_chunk(
            conn,
            chunk_id="chunk:b",
            document_id="rbi:master_direction:1",
            md_id="1",
            text="kyc verification quarterly reporting unrelated",
            embedding=b_vec,
        )
        _seed_chunk(
            conn,
            chunk_id="chunk:c",
            document_id="rbi:master_direction:2",
            md_id="2",
            text="non-matching prudential lender ratio",
            embedding=c_vec,
        )
        # FTS5 sync.
        conn.execute(
            "INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')"
        )

        results = hybrid_search(
            conn,
            fts_query="kyc",
            query_embedding=_f32_vec(query_vec),
            limit=5,
            fetch_per_side=5,
        )

        by_id = {r["chunk_id"]: r for r in results}

        # Chunk A is in both rankers (top FTS hit AND nearest vector).
        # It MUST have both bm25_score and vec_distance populated.
        assert "chunk:a" in by_id, "expected chunk A in fused results"
        row_a = by_id["chunk:a"]
        assert row_a["bm25_score"] is not None, (
            "chunk A is the top sparse result — bm25_score must be present"
        )
        assert row_a["vec_distance"] is not None, (
            "chunk A is in the dense top-K — vec_distance must NOT be dropped "
            "on the merge path. This is the PR 1 regression target."
        )
        assert row_a["sparse_rank"] is not None
        assert row_a["dense_rank"] is not None


def test_rrf_formula_uses_k_60(tmp_path: Path) -> None:
    """When a chunk appears at rank 1 in both rankers, fusion_score = 2/61.

    Pins the documented k=60 RRF parameter so a future refactor can't
    silently change the fusion semantics that downstream confidence
    calibration depends on.
    """
    db_path = tmp_path / "rrf.sqlite"
    with connect(db_path) as conn:
        dim = _detect_embedding_dim(conn)
        if dim is None:
            pytest.skip("sqlite-vec extension not available")

        _seed_document(conn, document_id="rbi:master_direction:1", md_id="1")
        vec = [1.0] + [0.0] * (dim - 1)
        _seed_chunk(
            conn,
            chunk_id="chunk:only",
            document_id="rbi:master_direction:1",
            md_id="1",
            text="solitary chunk on prudential norms",
            embedding=vec,
        )
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")

        results = hybrid_search(
            conn,
            fts_query="prudential",
            query_embedding=_f32_vec(vec),
            limit=5,
            fetch_per_side=5,
        )
        assert len(results) == 1
        # Both rankers agree at rank 1: 1/(60+1) + 1/(60+1) = 2/61 ≈ 0.03279.
        expected = 2.0 / 61.0
        assert results[0]["fusion_score"] == pytest.approx(expected, abs=1e-9)


def test_sparse_only_fast_path_populates_bm25_only(tmp_path: Path) -> None:
    """When dense returns nothing, the sparse-only fast path returns
    rows with bm25_score populated and vec_distance None.

    Documents the contract for the `if not dense` branch in hybrid_search.
    """
    db_path = tmp_path / "sparse_only.sqlite"
    with connect(db_path) as conn:
        _seed_document(conn, document_id="rbi:master_direction:1", md_id="1")
        _seed_chunk(
            conn,
            chunk_id="chunk:only",
            document_id="rbi:master_direction:1",
            md_id="1",
            text="chunk text about kyc",
            embedding=None,  # No vector — dense returns empty.
        )
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")

        results = hybrid_search(
            conn,
            fts_query="kyc",
            query_embedding=None,  # Forces sparse-only fast path.
            limit=5,
        )
        assert len(results) == 1
        assert results[0]["bm25_score"] is not None
        assert results[0]["vec_distance"] is None
        assert results[0]["sparse_rank"] == 1
        assert results[0]["dense_rank"] is None
