"""Refresh pipeline: orchestrates crawls and writes the corpus DB.

This is the script the weekly GitHub Action runs. It:

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

import os
import sys
from datetime import datetime
from pathlib import Path

import structlog

from ..db import connect, record_crawl_run, upsert_md, upsert_withdrawn
from . import md_list, withdrawn_list

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"


def run() -> int:
    """Execute one refresh pass. Returns process exit code (0 = success)."""
    db_path = Path(os.environ.get("RBI_SOURCE_DB_NEW", DEFAULT_DB_PATH)).expanduser().resolve()
    logger.info("refresh.start", db=str(db_path))

    md_started = datetime.utcnow().isoformat() + "Z"
    md_error: str | None = None
    md_result: md_list.CrawlResult | None = None
    try:
        md_result = md_list.crawl()
    except Exception as exc:  # noqa: BLE001 — top-level catch is intentional
        md_error = f"{type(exc).__name__}: {exc}"
        logger.error("refresh.md_list.fail", error=md_error)

    wd_started = datetime.utcnow().isoformat() + "Z"
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

    now = datetime.utcnow().isoformat() + "Z"

    with connect(db_path) as conn:
        if md_result is not None:
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
            record_crawl_run(
                conn,
                started_at=md_started,
                finished_at=now,
                source="md_list",
                source_url=md_list.LIST_URL,
                status_code=md_result.status_code,
                raw_html_sha256=md_result.raw_html_sha256,
                items_seen=len(md_result.master_directions),
            )
            logger.info("refresh.md_list.ok", count=len(md_result.master_directions))
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
