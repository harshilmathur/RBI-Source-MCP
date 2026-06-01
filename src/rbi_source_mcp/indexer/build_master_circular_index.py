"""Index Master Circulars (direct PDF, no detail-page hop) + bulk runner.

Master Circulars are linked DIRECTLY from category pages — there's no
intermediate detail page like notifications have. So this indexer takes a
(pdf_url, title, department) tuple and runs the same fetch+extract+chunk
pipeline.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import structlog

from ..crawler import master_circular_list
from ..crawler.master_circular_list import MasterCircular
from ..crawler.pdf_fetch import fetch_pdf
from ..db import connect, init_db
from ..extractor.pdf import (
    extract_text,
    parse_issue_date_from_pdf_text,
    parse_rbi_ref_from_pdf_text,
)
from .persist import persist_document_and_chunks

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"


def index_master_circular(mc: MasterCircular, *, db_path: Path | None = None, pdf_dir: Path | None = None) -> int:
    db_path = Path(db_path or os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)).expanduser().resolve()
    pdf_dir = Path(pdf_dir or db_path.parent / "pdfs").resolve()
    logger.info("mc_index.start", mc_id=mc.mc_id, title=mc.title[:60])

    pf = fetch_pdf(mc.pdf_url, referer=mc.detail_url, out_dir=pdf_dir)
    if not pf.is_pdf:
        logger.error("mc_index.pdf_fetch.fail", mc_id=mc.mc_id, error=pf.error)
        return 1

    ext = extract_text(pf.local_path)
    if not ext.passes_quality_gate:
        logger.error(
            "mc_index.extract.fail_quality",
            mc_id=mc.mc_id,
            quality=ext.extraction_quality,
        )
        return 1

    body_text = ext.text
    rbi_ref = parse_rbi_ref_from_pdf_text(body_text)
    issued_date = parse_issue_date_from_pdf_text(body_text)

    init_db(db_path)
    with connect(db_path) as conn:
        n = persist_document_and_chunks(
            conn,
            document_id=mc.document_id,
            md_id=mc.mc_id,
            title=mc.title,
            detail_url=mc.detail_url,
            document_family="master_circular",
            body_text=body_text,
            rbi_ref=rbi_ref,
            issued_date=issued_date,
            department=mc.department,
            pdf_urls=[mc.pdf_url],
            pdf_artifact={
                "pdf_url": mc.pdf_url,
                "sha256": pf.pdf_sha256,
                "bytes": pf.bytes,
                "page_count": ext.page_count,
                "quality": ext.extraction_quality,
            },
        )

    logger.info("mc_index.ok", mc_id=mc.mc_id, chunks=n)
    return 0 if n > 0 else 1


from ._bulk import BulkResult as _BulkResult  # noqa: E402  (legacy private alias)


def run_bulk(*, db_path: Path | None = None, skip_already: bool = True, sleep_between: float = 0.4) -> _BulkResult:
    db_path = Path(db_path or os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)).expanduser().resolve()
    crawl_result = master_circular_list.crawl()
    targets = crawl_result.master_circulars
    logger.info("mc_bulk.discovered", count=len(targets))

    init_db(db_path)
    skip_set: set[str] = set()
    if skip_already:
        # Master Circulars are re-issued yearly with new mc_ids, so the
        # doc-existence skip rarely misfires. Within a year a Master
        # Circular doesn't get amended; if it ever does, the monthly full
        # rebuild (corpus-release.yml mode=full) catches it.
        with connect(db_path) as conn:
            rows = conn.execute("SELECT md_id FROM documents WHERE document_family='master_circular'")
            skip_set = {r[0] for r in rows}

    result = _BulkResult(started_at=time.time())
    for i, mc in enumerate(targets, start=1):
        if mc.mc_id in skip_set:
            result.skipped.append(mc.mc_id)
            continue
        logger.info("mc_bulk.indexing", mc_id=mc.mc_id, progress=f"[{i}/{len(targets)}]", title=mc.title[:50])
        try:
            exit_code = index_master_circular(mc, db_path=db_path)
            (result.success if exit_code == 0 else result.failed).append(
                mc.mc_id if exit_code == 0 else (mc.mc_id, f"exit={exit_code}")
            )
        except Exception as exc:  # noqa: BLE001
            result.failed.append((mc.mc_id, f"{type(exc).__name__}: {exc}"))
        if sleep_between > 0 and i < len(targets):
            time.sleep(sleep_between)
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--bulk", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--db", default=None)
    args = p.parse_args()
    if args.bulk:
        r = run_bulk(db_path=Path(args.db) if args.db else None, skip_already=not args.force)
        elapsed = int(time.time() - r.started_at)
        print(f"\nBULK MASTER CIRCULARS DONE in {elapsed}s: success={len(r.success)} skipped={len(r.skipped)} failed={len(r.failed)}")
        if r.failed:
            for mc_id, err in r.failed[:5]:
                print(f"  {mc_id}: {err}")
        sys.exit(0 if not r.failed else 1)
    p.error("--bulk required (single-shot indexing of master circulars not exposed yet)")


if __name__ == "__main__":
    main()
