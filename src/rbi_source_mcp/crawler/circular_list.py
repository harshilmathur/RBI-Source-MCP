"""Crawl RBI's Standalone Circulars list page.

Source: https://www.rbi.org.in/Scripts/BS_ViewListofstandalonecirculars.aspx

The list page is a clean 4-column table:
    [serial #, RBI ref number, issued date, circular title]

Each row's anchor wraps the RBI ref text and points at
NotificationUser.aspx?Id=NNNN&Mode=0 — i.e., the same notification-detail
URL used everywhere else. So a "standalone circular" is just a notification
that's been categorized into this list. The only RBI-side identity is the
numeric Id; the document_family field on our side records which list
surfaced it.

Limitations:
    - The page is static HTML for all 271 entries (no pagination, no JS
      filter). We get the full set in one fetch.
    - Some entries link to detail pages that have only an HTML body and no
      PDF attachment; the indexer handles that downstream.
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

logger = structlog.get_logger(__name__)

LIST_URL = "https://www.rbi.org.in/Scripts/BS_ViewListofstandalonecirculars.aspx"
NOTIF_ID_PATTERN = re.compile(r"NotificationUser\.aspx\?(?:Id|id)=(\d+)", re.IGNORECASE)
USER_AGENT = "rbi-source-mcp/0.1 (+https://github.com/harshilmathur/RBI-Source-MCP)"


@dataclass(slots=True)
class StandaloneCircular:
    """One row from the standalone-circulars list page."""

    notif_id: str               # Numeric ID from NotificationUser.aspx?Id=
    rbi_ref: str                # e.g., "DOR.AML.REC.61/14.06.001/2025-26"
    title: str                  # Subject line of the circular
    detail_url: str             # Canonical NotificationUser.aspx URL
    issued_date: str | None     # ISO 8601 if parseable, else None

    @property
    def document_id(self) -> str:
        return f"rbi:standalone_circular:{self.notif_id}"


@dataclass(slots=True)
class CircularCrawlResult:
    fetched_at: str
    source_url: str
    final_url: str
    status_code: int
    raw_html: str
    raw_html_sha256: str
    circulars: list[StandaloneCircular] = field(default_factory=list)


def fetch_list_page(
    client: httpx.Client | None = None,
    *,
    timeout: float = 30.0,
) -> tuple[httpx.Response, str]:
    """Fetch the Standalone Circulars list page."""
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
        logger.info("circular_list.fetch.start", url=LIST_URL)
        resp = client.get(LIST_URL)
        resp.raise_for_status()
        logger.info(
            "circular_list.fetch.ok",
            status=resp.status_code,
            bytes=len(resp.content),
        )
        return resp, resp.text
    finally:
        if own_client:
            client.close()


def parse_list_html(html: str, base_url: str = LIST_URL) -> list[StandaloneCircular]:
    """Parse the list HTML into StandaloneCircular records.

    Anchor on rows that contain a NotificationUser.aspx?Id= link. Skip the
    header row (which has the same URL as a "Notifications" nav link but no
    real Id). For valid rows: cells[0]=serial, cells[1]=ref, cells[2]=date,
    cells[3]=title.
    """
    soup = BeautifulSoup(html, "lxml")
    seen_ids: set[str] = set()
    results: list[StandaloneCircular] = []

    for tr in soup.find_all("tr"):
        anchor = tr.find("a", href=lambda h: h and "NotificationUser.aspx?" in h)
        if not anchor:
            continue
        m = NOTIF_ID_PATTERN.search(anchor.get("href", ""))
        if not m:
            continue
        notif_id = m.group(1)
        if notif_id in seen_ids:
            continue

        cells = tr.find_all(["td", "th"])
        if len(cells) < 4:
            continue

        ref = _clean(cells[1].get_text(" ", strip=True))
        date_text = _clean(cells[2].get_text(" ", strip=True))
        title = _clean(cells[3].get_text(" ", strip=True))
        if not ref or not title:
            continue
        # Header row's "ref" cell often says "Circular Number"; skip.
        if ref.lower() in {"circular number", "ref no."}:
            continue

        detail_url = _canonicalize_url(urljoin(base_url, anchor.get("href", "")))
        issued_date = _parse_date(date_text)

        seen_ids.add(notif_id)
        results.append(
            StandaloneCircular(
                notif_id=notif_id,
                rbi_ref=ref,
                title=title,
                detail_url=detail_url,
                issued_date=issued_date,
            )
        )

    logger.info("circular_list.parse.ok", count=len(results))
    return results


def crawl(client: httpx.Client | None = None) -> CircularCrawlResult:
    """Fetch + parse the standalone-circulars list."""
    resp, html = fetch_list_page(client=client)
    raw_sha = hashlib.sha256(resp.content).hexdigest()
    circulars = parse_list_html(html, base_url=str(resp.url))
    return CircularCrawlResult(
        fetched_at=datetime.utcnow().isoformat() + "Z",
        source_url=LIST_URL,
        final_url=str(resp.url),
        status_code=resp.status_code,
        raw_html=html,
        raw_html_sha256=raw_sha,
        circulars=circulars,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host in {"rbi.org.in", "m.rbi.org.in"}:
        host = "www.rbi.org.in"
    return parsed._replace(netloc=host).geturl()


_DATE_PAIRS: list[tuple[str, list[str]]] = [
    (r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b", ["%B %d, %Y", "%b %d, %Y"]),
    (r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", ["%d %B %Y", "%d %b %Y"]),
    (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", ["%d/%m/%Y"]),
    (r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", ["%d-%m-%Y"]),
]


def _parse_date(text: str) -> str | None:
    if not text:
        return None
    for pattern, formats in _DATE_PAIRS:
        m = re.search(pattern, text)
        if not m:
            continue
        for fmt in formats:
            try:
                dt = datetime.strptime(m.group(0), fmt)
            except ValueError:
                continue
            return dt.date().isoformat()
    return None
