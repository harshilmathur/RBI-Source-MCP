"""Bulk-index every Standalone Circular from RBI's list page.

Walks BS_ViewListofstandalonecirculars.aspx, then runs index_circular for
each notification ID, with auto-retry on transient 504s (mirrors the
behaviour of build_all.py for Master Directions).

Usage:
    rbi-source-index-all-circulars [--limit N] [--skip-already-indexed]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import structlog

from ..crawler import circular_list
from ..db import connect, init_db
from ._bulk import BulkResult
from .build_circular_index import index_circular

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"

# Back-compat alias — earlier code used a per-family class name.
CircularBulkResult = BulkResult


def run(
    *,
    db_path: Path | None = None,
    skip_already_indexed: bool = True,
    limit: int | None = None,
    sleep_between: float = 0.4,
) -> CircularBulkResult:
    """Crawl the list page, then index every circular it contains."""
    db_path = Path(db_path or os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)).expanduser().resolve()

    logger.info("circ_bulk.start", db=str(db_path), skip=skip_already_indexed, limit=limit)

    crawl_result = circular_list.crawl()
    targets = crawl_result.circulars
    if limit:
        targets = targets[:limit]
    logger.info("circ_bulk.discovered", count=len(targets))

    init_db(db_path)
    skip_set: set[str] = set()
    if skip_already_indexed:
        # v0.7 tightening: join pdf_artifacts.fetched_at vs
        # documents.last_updated_at so a list-page bump on an existing
        # circular forces a re-fetch instead of silently keeping stale
        # chunks. Most circulars don't carry a meaningful last_updated_at
        # (they're issued once, not re-issued), so the OR-IS-NULL branch
        # preserves the current "skip if indexed" behavior for them. The
        # monthly full-rebuild safety net (corpus-release.yml mode=full)
        # catches silent RBI re-uploads.
        #
        # v0.7.1 review fix: join on document_id (family-aware unique key)
        # not bare md_id (which collides across families). Coerce
        # fetched_at to a calendar date so mixed-precision timestamps
        # (full ISO vs YYYY-MM-DD) compare correctly.
        with connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT pa.md_id
                FROM pdf_artifacts pa
                JOIN documents d ON d.document_id = pa.document_id
                WHERE pa.is_indexed = 1
                  AND d.document_family IN ('standalone_circular','notification')
                  AND (
                      d.last_updated_at IS NULL
                      OR date(pa.fetched_at) >= d.last_updated_at
                  )
                """
            )
            skip_set = {r[0] for r in rows}
        logger.info("circ_bulk.skip_already", count=len(skip_set))

    result = CircularBulkResult(started_at=time.time())

    for i, c in enumerate(targets, start=1):
        if c.notif_id in skip_set:
            result.skipped.append(c.notif_id)
            continue
        prefix = f"[{i}/{len(targets)}]"
        logger.info(
            "circ_bulk.indexing",
            notif_id=c.notif_id,
            ref=c.rbi_ref,
            progress=prefix,
        )
        try:
            exit_code = index_circular(
                c.notif_id,
                document_family="standalone_circular",
                rbi_ref=c.rbi_ref,
                title_hint=c.title,
                issued_date_hint=c.issued_date,
                db_path=db_path,
            )
            if exit_code == 0:
                result.success.append(c.notif_id)
            else:
                result.failed.append((c.notif_id, f"index_circular exit={exit_code}"))
        except Exception as exc:  # noqa: BLE001
            result.failed.append((c.notif_id, f"{type(exc).__name__}: {exc}"))
            logger.error("circ_bulk.exception", notif_id=c.notif_id, error=str(exc))
        if sleep_between > 0 and i < len(targets):
            time.sleep(sleep_between)

    # Retry pass for transient 504s, same pattern as MD bulk runner.
    if result.failed:
        retry_targets = [(nid, e) for nid, e in result.failed]
        targets_by_id = {c.notif_id: c for c in crawl_result.circulars}
        logger.info("circ_bulk.retry_start", count=len(retry_targets), cooldown_seconds=15)
        time.sleep(15.0)
        new_failed: list[tuple[str, str]] = []
        for j, (notif_id, _) in enumerate(retry_targets, start=1):
            c = targets_by_id.get(notif_id)
            if not c:
                new_failed.append((notif_id, "missing from list snapshot"))
                continue
            logger.info("circ_bulk.retry", notif_id=notif_id, progress=f"[{j}/{len(retry_targets)}]")
            try:
                exit_code = index_circular(
                    notif_id,
                    document_family="standalone_circular",
                    rbi_ref=c.rbi_ref,
                    title_hint=c.title,
                    issued_date_hint=c.issued_date,
                    db_path=db_path,
                )
                if exit_code == 0:
                    result.success.append(notif_id)
                else:
                    new_failed.append((notif_id, f"retry: index_circular exit={exit_code}"))
            except Exception as exc:  # noqa: BLE001
                new_failed.append((notif_id, f"retry: {type(exc).__name__}: {exc}"))
            if sleep_between > 0 and j < len(retry_targets):
                time.sleep(sleep_between)
        result.failed = new_failed

    elapsed = result.elapsed()
    logger.info(
        "circ_bulk.done",
        success=len(result.success),
        skipped=len(result.skipped),
        failed=len(result.failed),
        elapsed_seconds=int(elapsed),
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl + index every Standalone Circular from RBI's list."
    )
    parser.add_argument("--db", default=None)
    parser.add_argument("--limit", type=int, default=None, help="Index only first N (for testing)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-index even if a circular is already in the corpus",
    )
    args = parser.parse_args()
    result = run(
        db_path=Path(args.db) if args.db else None,
        skip_already_indexed=not args.force,
        limit=args.limit,
    )
    print()
    print("=" * 60)
    print(f"BULK CIRCULAR INDEX COMPLETE in {int(result.elapsed())}s")
    print("=" * 60)
    print(f"  succeeded:  {len(result.success)}")
    print(f"  skipped:    {len(result.skipped):>4} (already indexed; --force to redo)")
    print(f"  failed:     {len(result.failed):>4}")
    if result.failed:
        print()
        print("FAILED IDs:")
        for nid, err in result.failed[:10]:
            print(f"  {nid}  {err[:80]}")
        if len(result.failed) > 10:
            print(f"  ... and {len(result.failed) - 10} more")
    sys.exit(0 if not result.failed else 1)


if __name__ == "__main__":
    main()
