"""Bulk indexer — index every Master Direction in the documents table.

For each MD that hasn't been successfully indexed yet (or whose pdf_url
hash has changed since last index), runs the full single-MD pipeline:
detail page → PDF fetch → text extract → quality gate → chunk → embed →
store. Failures are logged but don't abort the run; the script collects
all errors and reports a summary at the end.

Resume-friendly: by default skips MDs already in pdf_artifacts with
is_indexed=1. Pass --force to re-index everything.

Run with:
    rbi-source-index-all
    rbi-source-index-all --force
    rbi-source-index-all --limit 50      # cap (useful for smoke tests)
    rbi-source-index-all --md-id 12896   # single MD (same as rbi-source-index)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import structlog

from ..db import connect
from ._bulk import BulkResult
from .build_md_index import index_md

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"

# Re-export so callers can still do ``from .build_all import BulkResult``.
__all__ = ["BulkResult", "list_md_ids", "already_indexed_ids", "run", "main"]


def list_md_ids(db_path: Path) -> list[str]:
    """All Master Direction IDs from the list-page crawl, ordered by ID.

    v0.7.1 (review fix): filter by document_family. Earlier this returned
    every md_id in the documents table, which was harmless when the staging
    DB was always empty before crawl. Now that the GHA workflow seeds the
    staging DB from the previous corpus release, the documents table holds
    rows from every family — feeding circular/PR/FAQ ids into index_md()
    fails the entire bulk run.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT md_id FROM documents "
            "WHERE md_id IS NOT NULL AND document_family = 'master_direction' "
            "ORDER BY md_id"
        ).fetchall()
    return [r["md_id"] for r in rows]


def already_indexed_ids(db_path: Path) -> set[str]:
    """MDs whose PDFs we already indexed AND whose list-page hasn't claimed
    a change since we fetched.

    v0.7 tightening (autoplan review #3): the prior implementation returned
    every md_id with ANY is_indexed=1 row. When the GHA workflow seeds the
    staging DB from the previous corpus release, that set covers every MD
    in the corpus, and an MD whose list-page `last_updated_at` was just
    bumped by the new crawl pass would be skipped silently — chunks would
    stay stale forever.

    v0.7.1 (review fix): join via `pa.document_id = d.document_id` (the
    family-aware unique key) instead of bare `md_id`. The documents UNIQUE
    constraint is `(md_id, document_family)`, so an MD and a press release
    with the same numeric id are distinct rows; bare-md_id JOIN would let a
    PR's pdf_artifacts row falsely satisfy the "indexed" check for an MD.

    v0.7.1 (review fix): coerce `pa.fetched_at` to a calendar date for the
    timestamp comparison. `fetched_at` is full ISO 8601 with Z (e.g.,
    `2025-04-22T14:33:11.123456Z`); `d.last_updated_at` is `YYYY-MM-DD`
    (or `YYYY` as a fallback for rarely-amended MDs). Mixed-precision lex
    comparison gave wrong answers — `date(...)` normalizes both sides.

    The fix joins `documents.last_updated_at` (refreshed by the crawler this
    run) against `pdf_artifacts.fetched_at` (stamped at the previous index
    pass). We only skip when:
      - we have at least one indexed pdf_artifacts row for the doc, AND
      - either the list page has no `last_updated_at` signal at all
        (rare; rarely-amended MDs), OR
      - the date of our fetch is at-or-after the list page's
        last_updated_at (no claimed change since we last touched the PDF).

    This catches the "list page bumped → re-fetch + re-extract" case while
    still skipping the no-change case fast (no PDF GET, no extract, no
    embed). The monthly full rebuild safety net (corpus-release.yml,
    `mode: full`) catches silent re-uploads RBI does without bumping the
    list-page date.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT pa.md_id
            FROM pdf_artifacts pa
            JOIN documents d ON d.document_id = pa.document_id
            WHERE pa.is_indexed = 1
              AND d.document_family = 'master_direction'
              AND (
                  d.last_updated_at IS NULL
                  OR date(pa.fetched_at) >= d.last_updated_at
              )
            """
        ).fetchall()
    return {r["md_id"] for r in rows}


def run(
    *,
    db_path: Path,
    pdf_dir: Path | None = None,
    force: bool = False,
    limit: int | None = None,
    only_md_id: str | None = None,
    sleep_between: float = 1.0,
) -> BulkResult:
    """Index every MD. Returns a BulkResult with success/skipped/failed lists."""
    result = BulkResult(started_at=time.time())

    targets = [only_md_id] if only_md_id else list_md_ids(db_path)
    skip_set: set[str] = set() if force else already_indexed_ids(db_path)
    if limit is not None:
        targets = targets[:limit]

    logger.info(
        "bulk_index.start",
        total=len(targets),
        already_indexed=len(skip_set),
        force=force,
    )

    for i, md_id in enumerate(targets, start=1):
        if md_id in skip_set:
            result.skipped.append(md_id)
            continue
        prefix = f"[{i}/{len(targets)}]"
        logger.info("bulk_index.indexing", md_id=md_id, progress=prefix)
        try:
            exit_code = index_md(md_id, db_path=db_path, pdf_dir=pdf_dir)
            if exit_code == 0:
                result.success.append(md_id)
            else:
                result.failed.append((md_id, f"index_md exit={exit_code}"))
        except Exception as exc:  # noqa: BLE001
            result.failed.append((md_id, f"{type(exc).__name__}: {exc}"))
            logger.error("bulk_index.exception", md_id=md_id, error=str(exc))
        # Light rate-limit between requests so we don't pound rbi.org.in
        # or trigger the rbidocs WAF too hard. The crawler already uses
        # browser-like headers, but politeness is cheap.
        if sleep_between > 0 and i < len(targets):
            time.sleep(sleep_between)

    # Retry pass: rbi.org.in occasionally returns 504 Gateway Timeout under
    # load. These are transient — retrying after a short cooldown almost
    # always succeeds. We do ONE retry per failed MD with a longer sleep.
    if result.failed:
        retry_targets = [md_id for md_id, _ in result.failed]
        logger.info(
            "bulk_index.retry_start",
            count=len(retry_targets),
            cooldown_seconds=15,
        )
        time.sleep(15.0)
        new_failed: list[tuple[str, str]] = []
        for j, md_id in enumerate(retry_targets, start=1):
            logger.info(
                "bulk_index.retry",
                md_id=md_id,
                progress=f"[{j}/{len(retry_targets)}]",
            )
            try:
                exit_code = index_md(md_id, db_path=db_path, pdf_dir=pdf_dir)
                if exit_code == 0:
                    result.success.append(md_id)
                else:
                    new_failed.append((md_id, f"retry: index_md exit={exit_code}"))
            except Exception as exc:  # noqa: BLE001
                new_failed.append((md_id, f"retry: {type(exc).__name__}: {exc}"))
            if sleep_between > 0 and j < len(retry_targets):
                time.sleep(sleep_between)
        result.failed = new_failed

    elapsed = result.elapsed()
    logger.info(
        "bulk_index.done",
        success=len(result.success),
        skipped=len(result.skipped),
        failed=len(result.failed),
        elapsed_seconds=int(elapsed),
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index every Master Direction (or a subset).",
    )
    parser.add_argument(
        "--db", default=None,
        help="SQLite path (default: $RBI_SOURCE_DB or ./data/db.sqlite)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-index even MDs already marked is_indexed=1",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap the number of MDs to process (useful for smoke tests)",
    )
    parser.add_argument(
        "--md-id", default=None,
        help="Index just one MD (same as rbi-source-index <md_id>)",
    )
    parser.add_argument(
        "--sleep", type=float, default=1.0,
        help="Seconds to sleep between MDs (default 1.0)",
    )
    args = parser.parse_args()

    db_path = Path(
        args.db or os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)
    ).expanduser().resolve()

    result = run(
        db_path=db_path,
        force=args.force,
        limit=args.limit,
        only_md_id=args.md_id,
        sleep_between=args.sleep,
    )

    # Print a summary report.
    print()
    print("=" * 60)
    print(f"BULK INDEX COMPLETE in {int(result.elapsed())}s")
    print("=" * 60)
    print(f"  succeeded: {len(result.success):>4}")
    print(f"  skipped:   {len(result.skipped):>4} (already indexed; --force to redo)")
    print(f"  failed:    {len(result.failed):>4}")
    print(f"  total:     {result.total:>4}")
    if result.failed:
        print()
        print("FAILED MDs:")
        for md_id, reason in result.failed[:30]:
            print(f"  {md_id:>6}  {reason[:100]}")
        if len(result.failed) > 30:
            print(f"  ... and {len(result.failed) - 30} more")

    sys.exit(0 if not result.failed else 2)


if __name__ == "__main__":
    main()
