"""Test bootstrap: a populated stub corpus at a tempdir, autouse.

A few HTTP-transport tests hit `/health`, which is a deep liveness check
that returns 503 when the DB has zero documents. On the dev machine that
is rarely an issue (the indexed corpus sits at ./data/db.sqlite); in CI
it is, because the corpus isn't checked in. Without this fixture the
tests pass on dev and fail on CI silently — exactly the wrong shape.

This fixture creates a fresh SQLite at a session-scoped tempdir, lets
db.connect() build the schema, inserts one stub document so /health
flips to 200, and points $RBI_SOURCE_DB at it for every test in the
session.

Tests that explicitly construct their own DB path (test_check_compliance,
test_schema_compound_key, etc.) pass `db_path` to `connect()` directly
and aren't affected — the env var is only the default fallback.

The eval-gate test (test_eval.py) opts out via its own skip logic when
the corpus is too small to be meaningful (<7 distinct MDs), so it isn't
poisoned by this fixture either.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from rbi_source_mcp.db import connect


def _seed_stub_corpus(db_path: Path) -> None:
    """Insert one document so the deep /health probe returns 200."""
    now = "2026-01-01T00:00:00Z"
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO documents (
                document_id, md_id, title, detail_url, document_family,
                pdf_urls_json, status, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rbi:master_direction:00000",
                "00000",
                "Stub Master Direction (test fixture only)",
                "https://example.invalid/stub",
                "master_direction",
                "[]",
                "current",
                now,
                now,
            ),
        )


@pytest.fixture(scope="session", autouse=True)
def stub_corpus_env() -> Iterator[None]:
    """Create a session-wide stub DB and point $RBI_SOURCE_DB at it.

    Snapshots and restores the env var so individual tests can still
    override it (e.g., to test the corpus_empty failure mode explicitly).
    """
    with tempfile.TemporaryDirectory(prefix="rbi-test-corpus-") as td:
        db_path = Path(td) / "stub.sqlite"
        _seed_stub_corpus(db_path)

        prev = os.environ.get("RBI_SOURCE_DB")
        os.environ["RBI_SOURCE_DB"] = str(db_path)
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop("RBI_SOURCE_DB", None)
            else:
                os.environ["RBI_SOURCE_DB"] = prev
