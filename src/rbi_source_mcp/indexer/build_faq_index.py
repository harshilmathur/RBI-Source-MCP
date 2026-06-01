"""Index a single FAQ topic page (FAQView.aspx?Id=N) + its bulk runner.

FAQs are HTML-only — no PDF attachments. Each topic page contains multiple
Q&As, which the chunker splits into citable pieces.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import structlog
from bs4 import BeautifulSoup

from ..crawler import faq_list
from ..db import connect, init_db
from .persist import persist_document_and_chunks

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/db.sqlite"


def index_faq(
    faq_id: str,
    *,
    title_hint: str | None = None,
    issued_date_hint: str | None = None,
    db_path: Path | None = None,
) -> int:
    db_path = Path(db_path or os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)).expanduser().resolve()
    logger.info("faq_index.start", faq_id=faq_id)

    try:
        final_url, html = faq_list.fetch_topic_html(faq_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("faq_index.fetch.fail", faq_id=faq_id, error=str(exc))
        return 1

    body_text = _extract_faq_body(html)
    if not body_text or len(body_text) < 100:
        logger.error("faq_index.body.empty", faq_id=faq_id, chars=len(body_text or ""))
        return 1

    title = title_hint or _extract_title(html) or f"FAQ {faq_id}"
    detail_url = f"https://www.rbi.org.in/Scripts/FAQView.aspx?Id={faq_id}"

    init_db(db_path)
    with connect(db_path) as conn:
        n = persist_document_and_chunks(
            conn,
            document_id=f"rbi:faq:{faq_id}",
            md_id=faq_id,
            title=title,
            detail_url=detail_url,
            document_family="faq",
            body_text=body_text,
            issued_date=issued_date_hint,
            pdf_urls=[],
            pdf_artifact=None,
        )

    logger.info("faq_index.ok", faq_id=faq_id, chunks=n)
    return 0 if n > 0 else 1


def _extract_title(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for tag in ("h1", "h2", "title"):
        el = soup.find(tag)
        if el:
            text = re.sub(r"\s+", " ", el.get_text(strip=True))
            if 5 < len(text) < 250 and text.lower() not in {"frequently asked questions", "faqs"}:
                return text
    return None


def _extract_faq_body(html: str) -> str | None:
    """FAQ topic pages bundle Q&A entries inside ASP.NET <form> wrappers.

    Critically: do NOT strip <form> like notif_detail does — RBI's ASP.NET
    pages wrap the entire content area in <form>, so removing it nukes the
    body. We only strip script/style/nav/header/footer.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    # Try the common content containers first.
    for selector in [
        "#PageContent", "#contentDiv", "#tablecontainer",
        "#tdmainqry", ".tablecontainer", "main", "#divscroll",
    ]:
        node = soup.select_one(selector)
        if node:
            text = re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True))
            if len(text) > 200:
                return text
    # Fallback: full document text minus the noise we already stripped.
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return text if len(text) > 200 else None


from ._bulk import BulkResult as _BulkResult  # noqa: E402  (legacy private alias)


def run_bulk(*, db_path: Path | None = None, skip_already: bool = True, sleep_between: float = 0.3) -> _BulkResult:
    db_path = Path(db_path or os.environ.get("RBI_SOURCE_DB", DEFAULT_DB_PATH)).expanduser().resolve()
    crawl_result = faq_list.crawl()
    targets = crawl_result.faqs
    logger.info("faq_bulk.discovered", count=len(targets))

    init_db(db_path)
    skip_set: set[str] = set()
    if skip_already:
        # FAQ pages don't carry a reliable last_updated_at signal in the
        # list-page HTML, so we keep the simpler doc-existence skip. If
        # RBI updates a Q&A on an existing FAQ topic, the monthly full
        # rebuild (corpus-release.yml mode=full) picks it up.
        with connect(db_path) as conn:
            rows = conn.execute("SELECT md_id FROM documents WHERE document_family='faq'")
            skip_set = {r[0] for r in rows}

    result = _BulkResult(started_at=time.time())
    for i, faq in enumerate(targets, start=1):
        if faq.faq_id in skip_set:
            result.skipped.append(faq.faq_id)
            continue
        logger.info("faq_bulk.indexing", faq_id=faq.faq_id, progress=f"[{i}/{len(targets)}]")
        try:
            exit_code = index_faq(
                faq.faq_id,
                title_hint=faq.title,
                issued_date_hint=faq.issued_date,
                db_path=db_path,
            )
            (result.success if exit_code == 0 else result.failed).append(
                faq.faq_id if exit_code == 0 else (faq.faq_id, f"exit={exit_code}")
            )
        except Exception as exc:  # noqa: BLE001
            result.failed.append((faq.faq_id, f"{type(exc).__name__}: {exc}"))
        if sleep_between > 0 and i < len(targets):
            time.sleep(sleep_between)
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("faq_id", nargs="?")
    p.add_argument("--bulk", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--db", default=None)
    p.add_argument("--title")
    args = p.parse_args()
    if args.bulk:
        r = run_bulk(db_path=Path(args.db) if args.db else None, skip_already=not args.force)
        elapsed = int(time.time() - r.started_at)
        print(f"\nBULK FAQs DONE in {elapsed}s: success={len(r.success)} skipped={len(r.skipped)} failed={len(r.failed)}")
        if r.failed:
            for fid, err in r.failed[:5]:
                print(f"  {fid}: {err}")
        sys.exit(0 if not r.failed else 1)
    if not args.faq_id:
        p.error("either pass a faq_id or --bulk")
    sys.exit(index_faq(args.faq_id, title_hint=args.title, db_path=Path(args.db) if args.db else None))


if __name__ == "__main__":
    main()
