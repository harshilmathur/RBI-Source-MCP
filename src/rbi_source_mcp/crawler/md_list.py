"""Crawl the RBI Master Directions list page.

Source: https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx

The list page is ASP.NET-rendered; layout consists of department-grouped tables
with rows of (title, link, date) per Master Direction.

This module is intentionally narrow at v0.1: it returns a flat list of MD
records, suitable for hash-gating and downstream PDF fetching.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup, Tag

from ._common import USER_AGENT

logger = structlog.get_logger(__name__)

LIST_URL = "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx"
DETAIL_URL_PATTERN = re.compile(r"BS_ViewMasDirections\.aspx\?id=(\d+)", re.IGNORECASE)


@dataclass(slots=True)
class MasterDirection:
    """One row from the Master Directions list page.

    Fields:
        md_id            -- numeric ID from ?id= query param. Source of truth for identity.
        title            -- raw title text from the anchor; usually contains an
                            "(Updated as on <date>)" parenthetical.
        detail_url       -- canonical detail-page URL.
        last_updated_at  -- best-effort parse of the "Updated as on" date inside
                            the title or in the row. None if not parseable.
        pdf_urls         -- v0.1 always empty: the list page only links to detail
                            pages, not PDFs. PDF URLs are populated in v1.0
                            after the detail-page crawler lands.
        department       -- always None at v0.1: the list page renders department
                            headings client-side via JavaScript, so static crawl
                            doesn't see them. Populated in v1.0 if/when needed.
    """

    md_id: str
    title: str
    detail_url: str
    last_updated_at: str | None  # ISO 8601 if parseable, else None
    pdf_urls: list[str] = field(default_factory=list)
    department: str | None = None  # always None at v0.1; reserved for v1.0

    @property
    def document_id(self) -> str:
        """Stable canonical ID."""
        return f"rbi:master_direction:{self.md_id}"

    # Backwards-compat alias for the old field name.
    @property
    def issued_date(self) -> str | None:  # pragma: no cover — alias only
        """Deprecated alias for last_updated_at. Will be removed at v1.0."""
        return self.last_updated_at


@dataclass(slots=True)
class CrawlResult:
    """Result of one crawl pass over the list page."""

    fetched_at: str  # ISO 8601 UTC timestamp
    source_url: str
    final_url: str
    status_code: int
    raw_html: str
    raw_html_sha256: str
    master_directions: list[MasterDirection]


def fetch_list_page(
    client: httpx.Client | None = None,
    *,
    timeout: float = 30.0,
) -> tuple[httpx.Response, str]:
    """Fetch the Master Directions list page.

    Returns the response and the raw HTML text. Caller is responsible for
    hashing / persisting if needed.

    Raises httpx.HTTPError on transport failure (caller decides retry).
    """
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
        logger.info("fetch.start", url=LIST_URL)
        response = client.get(LIST_URL)
        response.raise_for_status()
        logger.info(
            "fetch.ok",
            status=response.status_code,
            final_url=str(response.url),
            bytes=len(response.content),
        )
        return response, response.text
    finally:
        if own_client:
            client.close()


def parse_list_html(html: str, base_url: str = LIST_URL) -> list[MasterDirection]:
    """Parse the list HTML into MasterDirection records.

    The page is ASP.NET-rendered; we look for <a> tags that link to detail
    pages matching BS_ViewMasDirections.aspx?id=NNNNN, then walk up to the
    nearest table row to pull title and date.

    This parser tolerates layout drift by relying on the URL pattern as the
    primary anchor. If the URL pattern changes, the parser fails loudly.
    """
    soup = BeautifulSoup(html, "lxml")

    seen_ids: set[str] = set()
    results: list[MasterDirection] = []

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        match = DETAIL_URL_PATTERN.search(href)
        if not match:
            continue

        md_id = match.group(1)
        if md_id in seen_ids:
            continue
        seen_ids.add(md_id)

        title = _clean_text(anchor.get_text())
        if not title:
            continue

        detail_url = urljoin(base_url, href)
        # Canonicalize to www.rbi.org.in (RBI sometimes serves rbi.org.in / m.rbi.org.in).
        detail_url = _canonicalize_url(detail_url)

        # Walk up to find the row; pull "Updated as on" date from title or row.
        row = _nearest_row(anchor)
        last_updated_at = _extract_updated_as_on(title) or (
            _extract_date(row) if row else None
        )

        # Look for sibling PDF links in the same row. v0.1 list pages don't
        # carry direct PDF links (only detail-page links), but the helper is
        # cheap and forward-compatible with detail-page crawls.
        pdf_urls: list[str] = []
        if row:
            for pdf_anchor in row.find_all("a", href=True):
                pdf_href = pdf_anchor.get("href", "")
                if pdf_href.lower().endswith(".pdf") or "rbidocs" in pdf_href.lower():
                    pdf_urls.append(_canonicalize_url(urljoin(base_url, pdf_href)))

        results.append(
            MasterDirection(
                md_id=md_id,
                title=title,
                detail_url=detail_url,
                last_updated_at=last_updated_at,
                pdf_urls=pdf_urls,
                department=None,  # JS-rendered; not available in static HTML
            )
        )

    logger.info("parse.ok", count=len(results))
    return results


def crawl(client: httpx.Client | None = None) -> CrawlResult:
    """Fetch + parse the Master Directions list page.

    This is the single public entry point for the v0.1 MD list crawler.
    """
    response, html = fetch_list_page(client=client)
    raw_sha = hashlib.sha256(response.content).hexdigest()
    mds = parse_list_html(html, base_url=str(response.url))
    return CrawlResult(
        fetched_at=datetime.utcnow().isoformat() + "Z",
        source_url=LIST_URL,
        final_url=str(response.url),
        status_code=response.status_code,
        raw_html=html,
        raw_html_sha256=raw_sha,
        master_directions=mds,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_text(text: str | None) -> str:
    """Collapse whitespace and strip."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _canonicalize_url(url: str) -> str:
    """Normalize host to www.rbi.org.in for rbi.org.in / m.rbi.org.in variants.

    rbidocs.rbi.org.in is preserved (different host, holds PDFs).
    Path casing is preserved (RBI mixes cases; round-trip safety wins).
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host in {"rbi.org.in", "m.rbi.org.in"}:
        host = "www.rbi.org.in"
    elif host == "":
        # Already relative; return as-is.
        return url
    return parsed._replace(netloc=host).geturl()


def _nearest_row(tag: Tag) -> Tag | None:
    """Find the nearest enclosing table row (<tr>)."""
    return tag.find_parent("tr")


def _extract_date(row: Tag) -> str | None:
    """Look for a date in a row's cells.

    RBI dates are inconsistent. We iterate cell-by-cell rather than scanning
    the whole row's joined text so a year embedded in a title (e.g.,
    "...KYC) Directions, 2016") doesn't beat the actual date cell.

    Returns ISO 8601 if any cell parses cleanly. Falls back to a 4-digit year
    only if no cell yielded a full date. Returns None if nothing matches.
    """
    if not row:
        return None

    cells = row.find_all(["td", "th"])
    cell_texts = [_clean_text(c.get_text(" ", strip=True)) for c in cells] or [
        _clean_text(row.get_text(" ", strip=True))
    ]

    # Try each cell separately; first full-date hit wins.
    for cell_text in cell_texts:
        iso = _parse_date_string(cell_text)
        if iso:
            return iso

    # Fallback: look for any 4-digit year across the whole row.
    text = " ".join(cell_texts)
    year_match = re.search(r"\b(20\d{2})\b", text)
    return year_match.group(1) if year_match else None


# Attempt format pairs: each regex pattern with a list of strptime formats to
# try (covers full and abbreviated month names; "Jan" vs "January", "Apr" vs "April").
_DATE_FORMAT_PAIRS: list[tuple[str, list[str]]] = [
    (r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", ["%d %B %Y", "%d %b %Y"]),
    (r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b", ["%B %d, %Y", "%b %d, %Y"]),
    (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", ["%d/%m/%Y"]),
    (r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", ["%d-%m-%Y"]),
]


def _parse_date_string(text: str) -> str | None:
    """Try every known RBI date format. Returns ISO 8601 or None."""
    if not text:
        return None
    for pattern, formats in _DATE_FORMAT_PAIRS:
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


def _extract_updated_as_on(title: str) -> str | None:
    """Pull an 'Updated as on <date>' parenthetical out of an MD title.

    RBI titles commonly look like:
        "Master Direction - Reserve Bank of India (KYC) Directions, 2016
         (Updated as on April 22, 2025)"

    Returns ISO 8601 if parseable; None if no Updated-as-on phrase found.
    """
    if not title:
        return None
    match = re.search(
        r"Updated\s+as\s+on\s+([A-Za-z]+\s+\d{1,2},?\s*\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        title,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return _parse_date_string(match.group(1))


def md_id_from_url(url: str) -> str | None:
    """Extract the numeric MD ID from a BS_ViewMasDirections.aspx URL.

    Returns None for URLs that don't match the pattern.
    """
    match = DETAIL_URL_PATTERN.search(url)
    if match:
        return match.group(1)
    # Fallback: try parse_qs for malformed but recoverable cases.
    parsed = urlparse(url)
    if "BS_ViewMasDirections.aspx" not in parsed.path:
        return None
    qs = parse_qs(parsed.query)
    ids = qs.get("id") or qs.get("Id") or qs.get("ID")
    return ids[0] if ids else None


def diff_master_directions(
    previous: Iterable[MasterDirection],
    current: Iterable[MasterDirection],
) -> tuple[list[MasterDirection], list[MasterDirection], list[str]]:
    """Three-way diff for hash-gating.

    Returns (added, changed, removed_ids).

    "Changed" is determined by title or pdf_urls differing for the same md_id.
    PDF content hashes are the responsibility of a separate stage (this stage
    only sees the list page, not the PDFs themselves).
    """
    prev_by_id = {m.md_id: m for m in previous}
    curr_by_id = {m.md_id: m for m in current}

    added = [m for m_id, m in curr_by_id.items() if m_id not in prev_by_id]
    removed = [m_id for m_id in prev_by_id if m_id not in curr_by_id]
    changed = [
        m
        for m_id, m in curr_by_id.items()
        if m_id in prev_by_id
        and (
            m.title != prev_by_id[m_id].title
            or sorted(m.pdf_urls) != sorted(prev_by_id[m_id].pdf_urls)
        )
    ]
    return added, changed, removed
