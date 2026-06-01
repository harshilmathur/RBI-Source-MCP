"""Crawl RBI's Press Releases list page.

Source: https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx

The default landing shows the most-recent ~54 press releases. The page
displays them as <tr> rows with 2 cells: [title, file-size]. Each anchor
links to a detail page at BS_PressReleaseDisplay.aspx?prid=NNNNN.

Date is NOT in the list-page row (only on the detail page or in the press-
release body). v0.5 indexes the visible 54; full archive walk via date-
filter POST forms is deferred to v1.1.
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

from ._common import USER_AGENT

logger = structlog.get_logger(__name__)

LIST_URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"
PRID_PATTERN = re.compile(r"BS_PressReleaseDisplay\.aspx\?prid=(\d+)", re.IGNORECASE)


@dataclass(slots=True)
class PressRelease:
    prid: str
    title: str
    detail_url: str

    @property
    def document_id(self) -> str:
        return f"rbi:press_release:{self.prid}"


@dataclass(slots=True)
class PressReleaseCrawlResult:
    fetched_at: str
    source_url: str
    final_url: str
    status_code: int
    raw_html_sha256: str
    press_releases: list[PressRelease] = field(default_factory=list)


def crawl(client: httpx.Client | None = None, *, timeout: float = 30.0) -> PressReleaseCrawlResult:
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
        logger.info("press_release_list.fetch.start", url=LIST_URL)
        resp = client.get(LIST_URL)
        resp.raise_for_status()
        html = resp.text
        raw_sha = hashlib.sha256(resp.content).hexdigest()
        prs = parse_list_html(html, base_url=str(resp.url))
        logger.info("press_release_list.parse.ok", count=len(prs))
        return PressReleaseCrawlResult(
            fetched_at=datetime.utcnow().isoformat() + "Z",
            source_url=LIST_URL,
            final_url=str(resp.url),
            status_code=resp.status_code,
            raw_html_sha256=raw_sha,
            press_releases=prs,
        )
    finally:
        if own_client:
            client.close()


def parse_list_html(html: str, base_url: str = LIST_URL) -> list[PressRelease]:
    """Each press-release row has exactly one <a href="...prid=NNNNN">title</a>
    plus a sibling cell with the file size. Skip rows where len(anchors) != 1
    (handles the navigation-index <tr> that contains many anchors)."""
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    out: list[PressRelease] = []

    for tr in soup.find_all("tr"):
        anchors = [a for a in tr.find_all("a", href=True) if "BS_PressReleaseDisplay.aspx?prid=" in a.get("href", "")]
        if len(anchors) != 1:
            continue
        anchor = anchors[0]
        m = PRID_PATTERN.search(anchor.get("href", ""))
        if not m:
            continue
        prid = m.group(1)
        if prid in seen:
            continue
        title = re.sub(r"\s+", " ", anchor.get_text(strip=True))
        if not title or len(title) < 5:
            continue
        seen.add(prid)
        out.append(
            PressRelease(
                prid=prid,
                title=title,
                detail_url=_canonicalize(urljoin(base_url, anchor.get("href", ""))),
            )
        )
    return out


def _canonicalize(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host in {"rbi.org.in", "m.rbi.org.in"}:
        host = "www.rbi.org.in"
    return parsed._replace(netloc=host).geturl()
