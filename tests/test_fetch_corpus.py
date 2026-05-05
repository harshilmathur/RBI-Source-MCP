"""Tests for fetch_corpus + doctor.

The fetch path is mocked end-to-end via pytest-httpx: we set up a fake
GitHub Releases API response, fake asset bytes, build a real corpus
SQLite + xz it on the fly, and exercise the verify + decompress + atomic
install path.
"""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import sqlite3
import sys
from pathlib import Path

import pytest

from rbi_source_mcp import doctor, fetch_corpus
from rbi_source_mcp.db import (
    CORPUS_SCHEMA_VERSION,
    connect,
    init_db,
    set_meta,
)

# ---------------------------------------------------------------------------
# Fixtures: a tiny but real corpus, compressed and SHA'd in-memory.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_corpus(tmp_path: Path) -> Path:
    """Build a minimal but legitimate corpus SQLite at tmp_path/db.sqlite."""
    db = tmp_path / "real-corpus.sqlite"
    init_db(db)
    with connect(db) as c:
        c.execute(
            """INSERT INTO documents (
                document_id, md_id, title, detail_url, document_family,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "rbi:master_direction:test",
                "test",
                "Test MD",
                "https://example.com/x",
                "master_direction",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        set_meta(c, "embedding_provider", "cloudflare")
        set_meta(c, "embedding_model", "@cf/baai/bge-base-en-v1.5")
        set_meta(c, "embedding_dim", "768")
        set_meta(c, "embedding_revision", "n/a-cloudflare")
        set_meta(c, "build_timestamp", "2026-05-04T12:00:00Z")
        set_meta(c, "build_commit", "abc1234")
        set_meta(c, "build_mode", "full")
    return db


@pytest.fixture
def corpus_xz(real_corpus: Path, tmp_path: Path) -> tuple[bytes, str]:
    """Return (xz_bytes, sha256_hex_of_xz)."""
    raw = real_corpus.read_bytes()
    buf = io.BytesIO()
    with lzma.open(buf, "wb") as f:
        f.write(raw)
    xz_bytes = buf.getvalue()
    sha = hashlib.sha256(xz_bytes).hexdigest()
    return xz_bytes, sha


@pytest.fixture
def github_release_json() -> dict:
    """Minimal GitHub Releases API response shape — only the fields we read."""
    return {
        "tag_name": "latest-corpus",
        "assets": [
            {
                "name": "corpus.sqlite.xz",
                "browser_download_url": "https://example.com/dl/corpus.sqlite.xz",
            },
            {
                "name": "corpus.sqlite.xz.sha256",
                "browser_download_url": "https://example.com/dl/corpus.sqlite.xz.sha256",
            },
        ],
    }


# ---------------------------------------------------------------------------
# fetch_corpus tests.
# ---------------------------------------------------------------------------


def test_default_destination_honors_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RBI_SOURCE_DB", str(tmp_path / "custom" / "db.sqlite"))
    assert fetch_corpus._default_destination() == (tmp_path / "custom" / "db.sqlite").resolve()


def test_default_destination_falls_back_to_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RBI_SOURCE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    expected = (tmp_path / "xdg" / "rbi-source-mcp" / "db.sqlite").resolve()
    assert fetch_corpus._default_destination() == expected


def test_fetch_happy_path(
    httpx_mock,
    tmp_path: Path,
    corpus_xz: tuple[bytes, str],
    github_release_json: dict,
) -> None:
    """End-to-end: API → download → SHA → decompress → schema check → install."""
    xz_bytes, sha = corpus_xz
    httpx_mock.add_response(
        url="https://api.github.com/repos/harshilmathur/RBI-Source-MCP/releases/tags/latest-corpus",
        json=github_release_json,
    )
    httpx_mock.add_response(
        url="https://example.com/dl/corpus.sqlite.xz",
        content=xz_bytes,
    )
    httpx_mock.add_response(
        url="https://example.com/dl/corpus.sqlite.xz.sha256",
        content=f"{sha}  corpus.sqlite.xz\n".encode(),
    )
    dest = tmp_path / "out" / "db.sqlite"
    meta = fetch_corpus.fetch(destination=dest, verify_sigstore=False)
    assert dest.exists()
    assert meta["schema_version"] == CORPUS_SCHEMA_VERSION
    assert meta["embedding_provider"] == "cloudflare"
    # Reopen to confirm it's a real SQLite and has our document.
    with sqlite3.connect(str(dest)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert n == 1


def test_fetch_sha256_mismatch_aborts(
    httpx_mock,
    tmp_path: Path,
    corpus_xz: tuple[bytes, str],
    github_release_json: dict,
) -> None:
    xz_bytes, _ = corpus_xz
    httpx_mock.add_response(
        url="https://api.github.com/repos/harshilmathur/RBI-Source-MCP/releases/tags/latest-corpus",
        json=github_release_json,
    )
    httpx_mock.add_response(
        url="https://example.com/dl/corpus.sqlite.xz",
        content=xz_bytes,
    )
    # Wrong checksum.
    httpx_mock.add_response(
        url="https://example.com/dl/corpus.sqlite.xz.sha256",
        content=b"deadbeef  corpus.sqlite.xz\n",
    )
    with pytest.raises(SystemExit) as exc_info:
        fetch_corpus.fetch(
            destination=tmp_path / "out" / "db.sqlite", verify_sigstore=False
        )
    assert exc_info.value.code == 2  # _abort uses exit code 2


def test_fetch_404_release_aborts(httpx_mock, tmp_path: Path) -> None:
    httpx_mock.add_response(
        url="https://api.github.com/repos/harshilmathur/RBI-Source-MCP/releases/tags/nope",
        status_code=404,
        json={"message": "Not Found"},
    )
    with pytest.raises(SystemExit) as exc_info:
        fetch_corpus.fetch(
            tag="nope",
            destination=tmp_path / "out" / "db.sqlite",
            verify_sigstore=False,
        )
    assert exc_info.value.code == 2


def test_fetch_rate_limit_aborts(httpx_mock, tmp_path: Path) -> None:
    httpx_mock.add_response(
        url="https://api.github.com/repos/harshilmathur/RBI-Source-MCP/releases/tags/latest-corpus",
        status_code=403,
        text="API rate limit exceeded for 1.2.3.4",
    )
    with pytest.raises(SystemExit):
        fetch_corpus.fetch(
            destination=tmp_path / "out" / "db.sqlite", verify_sigstore=False
        )


def test_fetch_missing_required_asset_aborts(httpx_mock, tmp_path: Path) -> None:
    """Release exists but is missing corpus.sqlite.xz — bail."""
    httpx_mock.add_response(
        url="https://api.github.com/repos/harshilmathur/RBI-Source-MCP/releases/tags/latest-corpus",
        json={"tag_name": "latest-corpus", "assets": []},
    )
    with pytest.raises(SystemExit):
        fetch_corpus.fetch(
            destination=tmp_path / "out" / "db.sqlite", verify_sigstore=False
        )


def test_fetch_schema_mismatch_aborts(
    httpx_mock,
    tmp_path: Path,
    monkeypatch,
    github_release_json: dict,
) -> None:
    """Build a corpus stamped with a future schema_version, confirm fetch refuses."""
    db = tmp_path / "wrong-schema.sqlite"
    init_db(db)
    with connect(db) as c:
        # Overwrite the auto-stamped schema_version.
        c.execute("UPDATE corpus_meta SET value = '999' WHERE key = 'schema_version'")
    raw = db.read_bytes()
    buf = io.BytesIO()
    with lzma.open(buf, "wb") as f:
        f.write(raw)
    xz_bytes = buf.getvalue()
    sha = hashlib.sha256(xz_bytes).hexdigest()
    httpx_mock.add_response(
        url="https://api.github.com/repos/harshilmathur/RBI-Source-MCP/releases/tags/latest-corpus",
        json=github_release_json,
    )
    httpx_mock.add_response(url="https://example.com/dl/corpus.sqlite.xz", content=xz_bytes)
    httpx_mock.add_response(
        url="https://example.com/dl/corpus.sqlite.xz.sha256",
        content=f"{sha}  corpus.sqlite.xz\n".encode(),
    )
    with pytest.raises(SystemExit) as exc_info:
        fetch_corpus.fetch(
            destination=tmp_path / "out" / "db.sqlite", verify_sigstore=False
        )
    assert exc_info.value.code == 2


def test_fetch_no_verify_schema_skips_compat_check(
    httpx_mock,
    tmp_path: Path,
    github_release_json: dict,
) -> None:
    """--no-verify-schema should let a mismatched corpus through."""
    db = tmp_path / "wrong-schema.sqlite"
    init_db(db)
    with connect(db) as c:
        c.execute("UPDATE corpus_meta SET value = '999' WHERE key = 'schema_version'")
    raw = db.read_bytes()
    buf = io.BytesIO()
    with lzma.open(buf, "wb") as f:
        f.write(raw)
    xz_bytes = buf.getvalue()
    sha = hashlib.sha256(xz_bytes).hexdigest()
    httpx_mock.add_response(
        url="https://api.github.com/repos/harshilmathur/RBI-Source-MCP/releases/tags/latest-corpus",
        json=github_release_json,
    )
    httpx_mock.add_response(url="https://example.com/dl/corpus.sqlite.xz", content=xz_bytes)
    httpx_mock.add_response(
        url="https://example.com/dl/corpus.sqlite.xz.sha256",
        content=f"{sha}  corpus.sqlite.xz\n".encode(),
    )
    dest = tmp_path / "out" / "db.sqlite"
    fetch_corpus.fetch(destination=dest, verify_sigstore=False, verify_schema=False)
    assert dest.exists()


# ---------------------------------------------------------------------------
# doctor tests.
# ---------------------------------------------------------------------------


def test_doctor_python_check_ok() -> None:
    c = doctor.check_python_version()
    assert c.status == "OK"
    assert "Python" in c.detail


def test_doctor_corpus_missing(tmp_path: Path) -> None:
    c = doctor.check_corpus(tmp_path / "does-not-exist.sqlite")
    assert c.status == "FAIL"
    assert "rbi-source-fetch-corpus" in c.fix


def test_doctor_corpus_present(real_corpus: Path) -> None:
    c = doctor.check_corpus(real_corpus)
    assert c.status == "OK"
    assert "1 documents" in c.detail
    assert c.extra["documents"] == 1
    assert c.extra["schema_version"] == CORPUS_SCHEMA_VERSION


def test_doctor_corpus_invalid_sqlite(tmp_path: Path) -> None:
    fake = tmp_path / "fake.sqlite"
    fake.write_bytes(b"not a sqlite database")
    c = doctor.check_corpus(fake)
    assert c.status == "FAIL"


def test_doctor_sqlite_vec_loads() -> None:
    c = doctor.check_sqlite_vec()
    assert c.status == "OK"


def test_doctor_cf_creds_skipped_when_local(monkeypatch) -> None:
    monkeypatch.setenv("RBI_EMBEDDING_PROVIDER", "local")
    c = doctor.check_cf_creds_when_provider_cf()
    assert c.status == "OK"
    assert "skipped" in c.detail


def test_doctor_cf_creds_fail_when_cf_no_creds(monkeypatch) -> None:
    monkeypatch.setenv("RBI_EMBEDDING_PROVIDER", "cloudflare")
    monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CF_API_TOKEN", raising=False)
    # Also redirect the creds file lookup to a path that doesn't exist.
    monkeypatch.setenv("HOME", "/nonexistent/test/home")
    c = doctor.check_cf_creds_when_provider_cf()
    assert c.status == "FAIL"


def test_doctor_run_returns_list(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RBI_SOURCE_DB", str(tmp_path / "missing.sqlite"))
    checks = doctor.run(quick=True)
    assert len(checks) >= 4  # python + sqlite_vec + corpus + hf_cache + cf_creds
    assert any(c.name == "python" for c in checks)
    assert any(c.name == "corpus" for c in checks)


def test_doctor_json_output(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("RBI_SOURCE_DB", str(tmp_path / "missing.sqlite"))
    monkeypatch.setattr(sys, "argv", ["rbi-source-doctor", "--json", "--quick"])
    with pytest.raises(SystemExit) as exc:
        doctor.main()
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert all("name" in c and "status" in c for c in parsed)
    # corpus is missing, should be FAIL
    assert any(c["name"] == "corpus" and c["status"] == "FAIL" for c in parsed)
    assert exc.value.code == 1
