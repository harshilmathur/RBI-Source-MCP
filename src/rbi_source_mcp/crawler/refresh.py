"""Refresh pipeline: orchestrates crawls and writes the corpus DB.

This is the script the corpus-release GitHub Action runs (daily diff +
monthly full). It:

    1. Crawls the Master Directions list page.
    2. Crawls the withdrawn-circulars list page.
    3. Diffs against the existing corpus (hash-gating: unchanged rows are no-ops).
    4. Upserts changes into a new SQLite at $RBI_SOURCE_DB_NEW (defaults to data/db.sqlite.new).
    5. Records crawl_runs audit rows.

It does NOT do PDF extraction, smoke tests, or the atomic-swap. Those are
separate steps in the Action workflow (extract.py, smoke.py, deploy.sh) which
ship alongside v1.0 features.

Usage:
    python -m rbi_source_mcp.crawler.refresh
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import structlog

from .._time import iso_utc_now
from ..db import connect, record_crawl_run, upsert_md, upsert_withdrawn
from . import md_list, withdrawn_list

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"

# Only act on disappeared-from-list MDs when the current crawl returned at
# least this fraction of the previously-known count. A partial/failed list
# crawl (silent short read) would otherwise mass-mark good documents as gone,
# which is a worse corpus corruption than the stale-"current" bug this guards.
REMOVAL_SANE_FRACTION = 0.8


def _load_previous_mds(conn) -> list[md_list.MasterDirection]:  # noqa: ANN001
    """Reconstruct the previously-known MD set from the corpus for diffing.

    Only md_id / title / pdf_urls are needed — diff_master_directions compares
    on those. In diff mode the DB is seeded from latest-corpus (real previous
    state); in full mode it's empty (everything is 'added')."""
    rows = conn.execute(
        "SELECT md_id, title, pdf_urls_json FROM documents "
        "WHERE document_family = 'master_direction'"
    ).fetchall()
    return [
        md_list.MasterDirection(
            md_id=r["md_id"],
            title=r["title"],
            detail_url="",
            last_updated_at=None,
            pdf_urls=json.loads(r["pdf_urls_json"] or "[]"),
        )
        for r in rows
    ]


def run() -> int:
    """Execute one refresh pass. Returns process exit code (0 = success)."""
    db_path = Path(os.environ.get("RBI_SOURCE_DB_NEW", DEFAULT_DB_PATH)).expanduser().resolve()
    logger.info("refresh.start", db=str(db_path))

    md_started = iso_utc_now()
    md_error: str | None = None
    md_result: md_list.CrawlResult | None = None
    try:
        md_result = md_list.crawl()
    except Exception as exc:  # noqa: BLE001 — top-level catch is intentional
        md_error = f"{type(exc).__name__}: {exc}"
        logger.error("refresh.md_list.fail", error=md_error)

    wd_started = iso_utc_now()
    wd_error: str | None = None
    wd_result: withdrawn_list.WithdrawnCrawlResult | None = None
    try:
        wd_result = withdrawn_list.crawl()
    except Exception as exc:  # noqa: BLE001
        wd_error = f"{type(exc).__name__}: {exc}"
        logger.error("refresh.withdrawn.fail", error=wd_error)

    if md_result is None and wd_result is None:
        logger.error("refresh.both_failed", md_error=md_error, wd_error=wd_error)
        return 1

    now = iso_utc_now()

    with connect(db_path) as conn:
        if md_result is not None:
            # Diff against the existing corpus: real audit counts + detection of
            # MDs that dropped off the RBI list (which previously stayed
            # status="current" forever).
            previous = _load_previous_mds(conn)
            added, changed, removed_ids = md_list.diff_master_directions(
                previous, md_result.master_directions
            )

            for md in md_result.master_directions:
                upsert_md(
                    conn,
                    md.md_id,
                    title=md.title,
                    detail_url=md.detail_url,
                    last_updated_at=md.last_updated_at,
                    department=md.department,
                    pdf_urls=md.pdf_urls,
                    status="current",
                    now=now,
                    raw_list_sha256=md_result.raw_html_sha256,
                )

            # Mark disappeared MDs non-current — but only when the crawl looks
            # complete, so a partial list read can't mass-supersede good docs.
            removed_marked = 0
            current_count = len(md_result.master_directions)
            if removed_ids and previous:
                if current_count >= REMOVAL_SANE_FRACTION * len(previous):
                    for mid in removed_ids:
                        conn.execute(
                            "UPDATE documents SET status = 'superseded', last_seen_at = ? "
                            "WHERE md_id = ? AND document_family = 'master_direction' "
                            "AND status = 'current'",
                            (now, mid),
                        )
                    removed_marked = len(removed_ids)
                    logger.info("refresh.md.removed", count=removed_marked, ids=removed_ids)
                else:
                    logger.warning(
                        "refresh.md.removal_skipped",
                        removed=len(removed_ids),
                        current=current_count,
                        previous=len(previous),
                        note="current MD list too small vs previous — possible partial "
                        "crawl; NOT marking disappeared docs as superseded",
                    )

            record_crawl_run(
                conn,
                started_at=md_started,
                finished_at=now,
                source="md_list",
                source_url=md_list.LIST_URL,
                status_code=md_result.status_code,
                raw_html_sha256=md_result.raw_html_sha256,
                items_seen=current_count,
                items_added=len(added),
                items_changed=len(changed),
                items_removed=removed_marked,
            )
            logger.info(
                "refresh.md_list.ok",
                count=current_count,
                added=len(added),
                changed=len(changed),
                removed=removed_marked,
            )
        else:
            record_crawl_run(
                conn,
                started_at=md_started,
                finished_at=now,
                source="md_list",
                source_url=md_list.LIST_URL,
                status_code=None,
                raw_html_sha256=None,
                items_seen=0,
                error=md_error,
            )

        if wd_result is not None:
            for wc in wd_result.withdrawn_circulars:
                upsert_withdrawn(
                    conn,
                    original_ref=wc.original_ref,
                    original_id=wc.original_id,
                    title=wc.title,
                    issued_date=wc.issued_date,
                    withdrawn_date=wc.withdrawn_date,
                    replacement_ref=wc.replacement_ref,
                    detail_url=wc.detail_url,
                    now=now,
                )
            record_crawl_run(
                conn,
                started_at=wd_started,
                finished_at=now,
                source="withdrawn_list",
                source_url=withdrawn_list.LIST_URL,
                status_code=wd_result.status_code,
                raw_html_sha256=wd_result.raw_html_sha256,
                items_seen=len(wd_result.withdrawn_circulars),
            )
            logger.info("refresh.withdrawn.ok", count=len(wd_result.withdrawn_circulars))
        else:
            record_crawl_run(
                conn,
                started_at=wd_started,
                finished_at=now,
                source="withdrawn_list",
                source_url=withdrawn_list.LIST_URL,
                status_code=None,
                raw_html_sha256=None,
                items_seen=0,
                error=wd_error,
            )

    if md_error or wd_error:
        # Partial success: one source crawled, one failed. Exit non-zero so CI alerts.
        return 2

    logger.info("refresh.ok")
    return 0


def main() -> None:
    """Entry point for the `rbi-source-crawl` console script."""
    sys.exit(run())


if __name__ == "__main__":
    main()
