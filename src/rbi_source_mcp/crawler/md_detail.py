"""Crawl an individual Master Direction's detail page.

Source: https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=<MD_ID>

Each detail page links to one or more PDFs on rbidocs.rbi.org.in. The first
unlabeled PDF is conventionally the main MD body; subsequent links are
annexures. v0.1.5 fetches only the primary body for a single MD; annexures
ship at v0.5.

Notes on the page:
    - Some metadata (notably 'Updated as on <date>') is rendered into the
      detail page even when the list-page title doesn't carry it.
    - The PDF URLs live on rbidocs.rbi.org.in which has F5/Big-IP bot
      protection. Fetching them requires browser-like headers + a Referer
      pointing back to this detail page. See `pdf_fetch.py`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

from .._time import iso_utc_now
from ._common import USER_AGENT, get_with_retry

logger = structlog.get_logger(__name__)

DETAIL_URL_TEMPLATE = "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id={md_id}"


@dataclass(slots=True)
class DetailResult:
    """Result of one detail-page crawl."""

    md_id: str
    detail_url: str
    final_url: str
    fetched_at: str
    status_code: int
    raw_html: str
    raw_html_sha256: str
    primary_pdf_url: str | None
    annexure_pdf_urls: list[str] = field(default_factory=list)
    updated_as_on: str | None = None  # ISO 8601 if found on page


def fetch_detail(md_id: str, *, client: httpx.Client | None = None, timeout: float = 30.0) -> DetailResult:
    """Fetch and parse one Master Direction detail page."""
    url = DETAIL_URL_TEMPLATE.format(md_id=md_id)
    if client is None:
        client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            follow_redirects=True,
        )
        own_client = True
    else:
        own_client = False
    try:
        logger.info("md_detail.fetch.start", md_id=md_id, url=url)
        resp = get_with_retry(client, url)
        html = resp.text
        raw_sha = hashlib.sha256(resp.content).hexdigest()
        primary, annexures = _extract_pdf_urls(html, base_url=str(resp.url))
        updated_as_on = _extract_updated_as_on_from_page(html)
        logger.info(
            "md_detail.fetch.ok",
            md_id=md_id,
            status=resp.status_code,
            primary_pdf=bool(primary),
            annexure_count=len(annexures),
            updated_as_on=updated_as_on,
        )
        return DetailResult(
            md_id=md_id,
            detail_url=url,
            final_url=str(resp.url),
            fetched_at=iso_utc_now(),
            status_code=resp.status_code,
            raw_html=html,
            raw_html_sha256=raw_sha,
            primary_pdf_url=primary,
            annexure_pdf_urls=annexures,
            updated_as_on=updated_as_on,
        )
    finally:
        if own_client:
            client.close()


def _extract_pdf_urls(html: str, base_url: str) -> tuple[str | None, list[str]]:
    """Find PDF links on the detail page.

    Strategy:
        1. Collect all anchors whose href ends in .PDF/.pdf or points at
           rbidocs.rbi.org.in.
        2. The "primary" PDF is the first one whose anchor text is empty
           (RBI's convention for the main body link). If no anchor is empty,
           fall back to the first PDF link.
        3. Everything else is treated as an annexure.
    """
    soup = BeautifulSoup(html, "lxml")
    candidates: list[tuple[str, str]] = []  # (label, url)
    seen_urls: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        lower = href.lower()
        if not (lower.endswith(".pdf") or "rbidocs.rbi.org.in" in lower):
            continue
        full_url = urljoin(base_url, href)
        full_url = _canonicalize_url(full_url)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        label = re.sub(r"\s+", " ", anchor.get_text(strip=True))
        candidates.append((label, full_url))

    if not candidates:
        return None, []

    primary: str | None = None
    annexures: list[str] = []
    for label, url in candidates:
        if not label and primary is None:
            primary = url
        elif primary is None and url == candidates[0][1]:
            # Fallback: first PDF if no unlabeled one exists.
            primary = url
        else:
            annexures.append(url)
    if primary is None:
        primary = candidates[0][1]
        annexures = [u for _, u in candidates[1:]]
    return primary, annexures


def _canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host in {"rbi.org.in", "m.rbi.org.in"}:
        host = "www.rbi.org.in"
    return parsed._replace(netloc=host).geturl()


def _extract_updated_as_on_from_page(html: str) -> str | None:
    """Look for an 'Updated as on <date>' phrase in the detail page body."""
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    m = re.search(
        r"Updated\s+as\s+on\s+([A-Za-z]+\s+\d{1,2},?\s*\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    raw_date = m.group(1)
    pairs: list[tuple[str, list[str]]] = [
        (r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", ["%d %B %Y", "%d %b %Y"]),
        (r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b", ["%B %d, %Y", "%b %d, %Y"]),
    ]
    for pattern, formats in pairs:
        match = re.search(pattern, raw_date)
        if not match:
            continue
        for fmt in formats:
            try:
                dt = datetime.strptime(match.group(0), fmt)
            except ValueError:
                continue
            return dt.date().isoformat()
    return None
