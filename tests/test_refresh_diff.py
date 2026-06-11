"""Tests for refresh.run()'s diff + guarded removal.

Regression target: refresh.py previously never diffed against the existing
corpus — it always upserted status="current" and recorded items_added/
changed/removed = 0. So an MD that dropped off the RBI list stayed "current"
forever, and the audit trail was blind to what changed.

The fix diffs against the seeded DB, records real counts, and marks
disappeared MDs non-current — but only when the crawl looks complete, so a
partial/failed list read can't mass-supersede good documents.
"""

from __future__ import annotations

from pathlib import Path

from rbi_source_mcp.crawler import md_list, refresh, withdrawn_list
from rbi_source_mcp.db import connect, upsert_md


def _md(md_id: str, *, title: str = "Master Direction", pdf_urls: list[str] | None = None):
    return md_list.MasterDirection(
        md_id=md_id,
        title=title,
        detail_url=f"https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id={md_id}",
        last_updated_at=None,
        pdf_urls=pdf_urls or [],
    )


def _md_crawl_result(mds: list[md_list.MasterDirection]) -> md_list.CrawlResult:
    return md_list.CrawlResult(
        fetched_at="2026-01-01T00:00:00Z",
        source_url=md_list.LIST_URL,
        final_url=md_list.LIST_URL,
        status_code=200,
        raw_html="<html></html>",
        raw_html_sha256="deadbeef",
        master_directions=mds,
    )


def _empty_withdrawn() -> withdrawn_list.WithdrawnCrawlResult:
    return withdrawn_list.WithdrawnCrawlResult(
        fetched_at="2026-01-01T00:00:00Z",
        source_url=withdrawn_list.LIST_URL,
        final_url=withdrawn_list.LIST_URL,
        status_code=200,
        raw_html="<html></html>",
        raw_html_sha256="cafe",
        withdrawn_circulars=[],
    )


def _seed_previous(db_path: Path, md_ids: list[str]) -> None:
    """Seed the staging DB as if it were copied from latest-corpus."""
    with connect(db_path) as conn:
        for mid in md_ids:
            upsert_md(
                conn,
                mid,
                # Match _md()'s default title + pdf_urls so survivors don't
                # register as "changed" — the test isolates add/remove counts.
                title="Master Direction",
                detail_url=f"https://x/{mid}",
                last_updated_at=None,
                department=None,
                pdf_urls=[],
                status="current",
                now="2026-01-01T00:00:00Z",
            )


def _status_of(db_path: Path, md_id: str) -> str | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM documents WHERE md_id = ? AND document_family='master_direction'",
            (md_id,),
        ).fetchone()
        return row["status"] if row else None


def _last_crawl_run(db_path: Path) -> dict:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT items_seen, items_added, items_changed, items_removed FROM crawl_runs "
            "WHERE source='md_list' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row)


def test_removed_md_is_marked_superseded_and_counted(tmp_path, monkeypatch) -> None:
    """An MD that disappears from a complete crawl is marked non-current and
    counted in the audit row."""
    db = tmp_path / "db.sqlite.new"
    # Previously knew 5 MDs.
    _seed_previous(db, ["1", "2", "3", "4", "5"])
    # Current crawl returns 4 of them (md 5 dropped off) + 1 brand-new (md 6).
    current = [_md(x) for x in ["1", "2", "3", "4", "6"]]
    monkeypatch.setattr(md_list, "crawl", lambda *a, **k: _md_crawl_result(current))
    monkeypatch.setattr(withdrawn_list, "crawl", lambda *a, **k: _empty_withdrawn())
    monkeypatch.setenv("RBI_SOURCE_DB_NEW", str(db))

    rc = refresh.run()
    assert rc == 0

    # md 5 disappeared from a complete crawl (5/5 ≥ 80%) → superseded.
    assert _status_of(db, "5") == "superseded"
    # surviving + new MDs stay current.
    assert _status_of(db, "1") == "current"
    assert _status_of(db, "6") == "current"

    audit = _last_crawl_run(db)
    assert audit["items_seen"] == 5
    assert audit["items_added"] == 1  # md 6
    assert audit["items_removed"] == 1  # md 5
    assert audit["items_changed"] == 0


def test_partial_crawl_does_not_mass_supersede(tmp_path, monkeypatch) -> None:
    """If the current crawl returns far fewer MDs than known (a likely partial
    read), removals are skipped — good docs must NOT be mass-superseded."""
    db = tmp_path / "db.sqlite.new"
    _seed_previous(db, [str(i) for i in range(1, 11)])  # 10 known
    # Current crawl returns only 3 (a partial read: 3/10 = 30% < 80%).
    current = [_md(x) for x in ["1", "2", "3"]]
    monkeypatch.setattr(md_list, "crawl", lambda *a, **k: _md_crawl_result(current))
    monkeypatch.setattr(withdrawn_list, "crawl", lambda *a, **k: _empty_withdrawn())
    monkeypatch.setenv("RBI_SOURCE_DB_NEW", str(db))

    refresh.run()

    # None of the "missing" 7 may be superseded — the guard tripped.
    for mid in ["4", "5", "6", "7", "8", "9", "10"]:
        assert _status_of(db, mid) == "current", f"md {mid} must NOT be superseded on a partial crawl"
    assert _last_crawl_run(db)["items_removed"] == 0
