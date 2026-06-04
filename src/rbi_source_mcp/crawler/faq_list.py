"""Crawl RBI's FAQ list page.

Source: https://www.rbi.org.in/Scripts/FAQView.aspx

Lists ~98 FAQ topic pages, each at FAQView.aspx?Id=NNN. Anchor text
typically includes a publish date prefix ("Nov 12, 2021 Retail Direct
Scheme") which we split out into title + date.

Each FAQ topic page contains multiple Q&A entries. We treat each topic
as a single document and let the chunker break it into Q&A-sized pieces.
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

from ._common import USER_AGENT, get_with_retry

logger = structlog.get_logger(__name__)

LIST_URL = "https://www.rbi.org.in/Scripts/FAQView.aspx"
FAQ_ID_PATTERN = re.compile(r"FAQView\.aspx\?Id=(\d+)", re.IGNORECASE)
DATE_PREFIX_PATTERN = re.compile(
    r"^\s*([A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4})\s+(.+)$"
)


@dataclass(slots=True)
class FaqTopic:
    faq_id: str
    title: str
    detail_url: str
    issued_date: str | None  # parsed from "Nov 12, 2021" prefix in label

    @property
    def document_id(self) -> str:
        return f"rbi:faq:{self.faq_id}"


@dataclass(slots=True)
class FaqCrawlResult:
    fetched_at: str
    source_url: str
    final_url: str
    status_code: int
    raw_html_sha256: str
    faqs: list[FaqTopic] = field(default_factory=list)


def crawl(client: httpx.Client | None = None, *, timeout: float = 30.0) -> FaqCrawlResult:
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
        logger.info("faq_list.fetch.start", url=LIST_URL)
        resp = get_with_retry(client, LIST_URL)
        html = resp.text
        raw_sha = hashlib.sha256(resp.content).hexdigest()
        faqs = parse_list_html(html, base_url=str(resp.url))
        logger.info("faq_list.parse.ok", count=len(faqs))
        return FaqCrawlResult(
            fetched_at=datetime.utcnow().isoformat() + "Z",
            source_url=LIST_URL,
            final_url=str(resp.url),
            status_code=resp.status_code,
            raw_html_sha256=raw_sha,
            faqs=faqs,
        )
    finally:
        if own_client:
            client.close()


def parse_list_html(html: str, base_url: str = LIST_URL) -> list[FaqTopic]:
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    out: list[FaqTopic] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = FAQ_ID_PATTERN.search(href)
        if not m:
            continue
        faq_id = m.group(1)
        if faq_id in seen:
            continue
        label = re.sub(r"\s+", " ", a.get_text(strip=True))
        if not label or len(label) < 5:
            continue
        # Strip the "Nov 12, 2021 " date prefix if present.
        date_iso: str | None = None
        title = label
        m2 = DATE_PREFIX_PATTERN.match(label)
        if m2:
            date_iso = _parse_date(m2.group(1))
            title = m2.group(2).strip()
        if not title or len(title) < 5:
            continue
        seen.add(faq_id)
        out.append(
            FaqTopic(
                faq_id=faq_id,
                title=title,
                detail_url=_canonicalize(urljoin(base_url, href)),
                issued_date=date_iso,
            )
        )
    return out


def _canonicalize(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host in {"rbi.org.in", "m.rbi.org.in"}:
        host = "www.rbi.org.in"
    return parsed._replace(netloc=host).geturl()


def _parse_date(text: str) -> str | None:
    pairs: list[tuple[str, list[str]]] = [
        (r"\b([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})\b", ["%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"]),
    ]
    for pattern, formats in pairs:
        m = re.search(pattern, text)
        if not m:
            continue
        for fmt in formats:
            try:
                dt = datetime.strptime(m.group(0).replace(",", ", "), fmt)
            except ValueError:
                try:
                    dt = datetime.strptime(m.group(0), fmt)
                except ValueError:
                    continue
            return dt.date().isoformat()
    return None


def fetch_topic_html(faq_id: str, *, client: httpx.Client | None = None, timeout: float = 30.0) -> tuple[str, str]:
    """Fetch a single FAQ topic page. Returns (final_url, html)."""
    url = f"https://www.rbi.org.in/Scripts/FAQView.aspx?Id={faq_id}"
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
        resp = get_with_retry(client, url)
        return str(resp.url), resp.text
    finally:
        if own_client:
            client.close()
