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
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from ..db import connect
from .build_md_index import index_md

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"


@dataclass(slots=True)
class BulkResult:
    started_at: float
    success: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.success) + len(self.skipped) + len(self.failed)

    def elapsed(self) -> float:
        return time.time() - self.started_at


def list_md_ids(db_path: Path) -> list[str]:
    """All Master Direction IDs from the list-page crawl, ordered by ID."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT md_id FROM documents WHERE md_id IS NOT NULL ORDER BY md_id"
        ).fetchall()
    return [r["md_id"] for r in rows]


def already_indexed_ids(db_path: Path) -> set[str]:
    """MDs that have at least one is_indexed=1 PDF artifact."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT md_id FROM pdf_artifacts WHERE is_indexed = 1"
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
