"""Index a single press release (BS_PressReleaseDisplay.aspx?prid=N) + its bulk runner."""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from ..crawler import press_release_detail, press_release_list
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


def index_press_release(
    prid: str,
    *,
    title_hint: str | None = None,
    db_path: Path | None = None,
    pdf_dir: Path | None = None,
) -> int:
    db_path = Path(db_path or os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)).expanduser().resolve()
    pdf_dir = Path(pdf_dir or db_path.parent / "pdfs").resolve()
    logger.info("pr_index.start", prid=prid)

    try:
        detail = press_release_detail.fetch_detail(prid)
    except Exception as exc:  # noqa: BLE001
        logger.error("pr_index.detail.fail", prid=prid, error=str(exc))
        return 1

    body_text: str | None = None
    pdf_artifact: dict | None = None
    rbi_ref: str | None = None
    issued_date: str | None = None

    if detail.primary_pdf_url:
        pf = fetch_pdf(detail.primary_pdf_url, referer=detail.detail_url, out_dir=pdf_dir)
        if pf.is_pdf:
            ext = extract_text(pf.local_path)
            if ext.passes_quality_gate:
                body_text = ext.text
                rbi_ref = parse_rbi_ref_from_pdf_text(body_text)
                issued_date = parse_issue_date_from_pdf_text(body_text)
                pdf_artifact = {
                    "pdf_url": detail.primary_pdf_url,
                    "sha256": pf.pdf_sha256,
                    "bytes": pf.bytes,
                    "page_count": ext.page_count,
                    "quality": ext.extraction_quality,
                }

    if body_text is None and detail.html_body_text:
        body_text = detail.html_body_text

    if not body_text:
        logger.error("pr_index.no_body", prid=prid)
        return 1

    init_db(db_path)
    title = title_hint or detail.title or f"Press Release {prid}"
    pdf_urls = [detail.primary_pdf_url] if detail.primary_pdf_url else []
    pdf_urls.extend(detail.annexure_pdf_urls)

    with connect(db_path) as conn:
        n = persist_document_and_chunks(
            conn,
            document_id=f"rbi:press_release:{prid}",
            md_id=prid,
            title=title,
            detail_url=detail.detail_url,
            document_family="press_release",
            body_text=body_text,
            rbi_ref=rbi_ref,
            issued_date=issued_date,
            pdf_urls=pdf_urls,
            pdf_artifact=pdf_artifact,
        )

    logger.info("pr_index.ok", prid=prid, chunks=n, source="pdf" if pdf_artifact else "html_body")
    return 0 if n > 0 else 1


@dataclass(slots=True)
class _BulkResult:
    started_at: float
    success: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def run_bulk(*, db_path: Path | None = None, skip_already: bool = True, sleep_between: float = 0.4) -> _BulkResult:
    db_path = Path(db_path or os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)).expanduser().resolve()
    crawl_result = press_release_list.crawl()
    targets = crawl_result.press_releases
    logger.info("pr_bulk.discovered", count=len(targets))

    init_db(db_path)
    skip_set: set[str] = set()
    if skip_already:
        # Press releases are issued once and don't change after publication;
        # doc-existence is a sufficient skip signal. If RBI ever corrects a
        # typo on an existing PR, the monthly full-rebuild safety net
        # (corpus-release.yml mode=full) catches it. See autoplan review
        # finding #2 for the MD-family equivalent of this concern.
        with connect(db_path) as conn:
            rows = conn.execute(
                "SELECT md_id FROM documents WHERE document_family='press_release'"
            )
            skip_set = {r[0] for r in rows}

    result = _BulkResult(started_at=time.time())
    for i, pr in enumerate(targets, start=1):
        if pr.prid in skip_set:
            result.skipped.append(pr.prid)
            continue
        logger.info("pr_bulk.indexing", prid=pr.prid, progress=f"[{i}/{len(targets)}]")
        try:
            exit_code = index_press_release(pr.prid, title_hint=pr.title, db_path=db_path)
            (result.success if exit_code == 0 else result.failed).append(
                pr.prid if exit_code == 0 else (pr.prid, f"exit={exit_code}")
            )
        except Exception as exc:  # noqa: BLE001
            result.failed.append((pr.prid, f"{type(exc).__name__}: {exc}"))
        if sleep_between > 0 and i < len(targets):
            time.sleep(sleep_between)

    if result.failed:
        time.sleep(15)
        retry = list(result.failed)
        result.failed = []
        for prid, _ in retry:
            try:
                pr = next((p for p in targets if p.prid == prid), None)
                title = pr.title if pr else None
                exit_code = index_press_release(prid, title_hint=title, db_path=db_path)
                if exit_code == 0:
                    result.success.append(prid)
                else:
                    result.failed.append((prid, f"retry exit={exit_code}"))
            except Exception as exc:  # noqa: BLE001
                result.failed.append((prid, f"retry: {type(exc).__name__}: {exc}"))
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("prid", nargs="?", help="(optional) single prid to index")
    p.add_argument("--bulk", action="store_true", help="index all from list page")
    p.add_argument("--force", action="store_true")
    p.add_argument("--db", default=None)
    p.add_argument("--title")
    args = p.parse_args()
    if args.bulk:
        r = run_bulk(db_path=Path(args.db) if args.db else None, skip_already=not args.force)
        elapsed = int(time.time() - r.started_at)
        print(f"\nBULK PRESS RELEASES DONE in {elapsed}s: success={len(r.success)} skipped={len(r.skipped)} failed={len(r.failed)}")
        if r.failed:
            for prid, err in r.failed[:5]:
                print(f"  {prid}: {err}")
        sys.exit(0 if not r.failed else 1)
    if not args.prid:
        p.error("either pass a prid or --bulk")
    sys.exit(index_press_release(
        args.prid,
        title_hint=args.title,
        db_path=Path(args.db) if args.db else None,
    ))


if __name__ == "__main__":
    main()
