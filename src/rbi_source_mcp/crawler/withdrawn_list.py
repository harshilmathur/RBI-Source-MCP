"""Crawl the RBI Withdrawn Circulars list page.

Source: https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx

This is the data source that powers the entire `check_current` "is this
withdrawn?" capability. Without this list, the product has no version
awareness moat.

The page lists withdrawn circulars / notifications with:
    - Original RBI reference number (e.g., RBI/DOR/2018/45)
    - Title / subject
    - Original issue date
    - Date of withdrawal
    - Reference to the circular that withdrew it (when present)

v0.1 returns a flat list of withdrawn entries. The replacement-document
linkage (when present in the page) is captured best-effort; cases where
the page only states "withdrawn" without naming a successor are returned
with `replacement_ref=None`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

logger = structlog.get_logger(__name__)

LIST_URL = "https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx"
DETAIL_URL_PATTERN = re.compile(
    r"NotificationUserWithdrawnCircular\.aspx\?Id=(\d+)", re.IGNORECASE
)
NOTIF_URL_PATTERN = re.compile(r"NotificationUser\.aspx\?Id=(\d+)", re.IGNORECASE)
RBI_REF_PATTERN = re.compile(r"RBI[/\s][A-Za-z0-9./\s\-]+\d{4}[/\s\-]+\d+", re.IGNORECASE)
USER_AGENT = "rbi-source-mcp/0.1 (+https://github.com/harshilmathur/RBI-Source-MCP)"


@dataclass(slots=True)
class WithdrawnCircular:
    """One row from the withdrawn-circulars list page."""

    withdrawn_id: str | None  # ID of the withdrawal record itself, if a separate page
    original_ref: str | None  # e.g., "RBI/DOR/2018/45"
    original_id: str | None  # numeric ID from NotificationUser.aspx?Id= (the original circular)
    title: str
    issued_date: str | None
    withdrawn_date: str | None
    replacement_ref: str | None  # Best-effort: ref of circular that withdrew this
    detail_url: str | None  # URL to the original circular if available


@dataclass(slots=True)
class WithdrawnCrawlResult:
    """Result of one crawl of the withdrawn-circulars list."""

    fetched_at: str
    source_url: str
    final_url: str
    status_code: int
    raw_html: str
    raw_html_sha256: str
    withdrawn_circulars: list[WithdrawnCircular]


def fetch_list_page(
    client: httpx.Client | None = None,
    *,
    timeout: float = 30.0,
) -> tuple[httpx.Response, str]:
    """Fetch the Withdrawn Circulars list page."""
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


def parse_list_html(html: str, base_url: str = LIST_URL) -> list[WithdrawnCircular]:
    """Parse the withdrawn-circulars list HTML.

    The page is table-rendered, similar to other RBI list pages. Each row
    typically holds: original ref, title, issue date, withdrawn date, plus
    optional links to the original circular and/or the withdrawing circular.

    We anchor on table rows and extract whatever we can; missing fields are
    None rather than raising.
    """
    soup = BeautifulSoup(html, "lxml")

    results: list[WithdrawnCircular] = []
    seen_signatures: set[tuple[str | None, str]] = set()

    # Look at every <tr>; many will be header/spacer rows that we'll skip
    # by requiring at least one date or one RBI ref to be present.
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        cell_texts = [_clean_text(c.get_text(" ", strip=True)) for c in cells]
        joined = " | ".join(cell_texts)

        # Heuristic: a real data row mentions a year (issue or withdrawal date).
        if not re.search(r"\b(19|20)\d{2}\b", joined):
            continue

        # Extract anchors in this row.
        original_id: str | None = None
        detail_url: str | None = None
        for anchor in row.find_all("a", href=True):
            href = anchor.get("href", "")
            notif_match = NOTIF_URL_PATTERN.search(href)
            if notif_match and original_id is None:
                original_id = notif_match.group(1)
                detail_url = _canonicalize_url(urljoin(base_url, href))

        title = _pick_longest_text(cell_texts)
        if not title:
            continue

        original_ref = _extract_rbi_ref(joined)
        issued_date, withdrawn_date = _extract_two_dates(joined)
        replacement_ref = _extract_replacement_ref(joined, original_ref)

        signature = (original_id, title.lower())
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)

        results.append(
            WithdrawnCircular(
                withdrawn_id=None,
                original_ref=original_ref,
                original_id=original_id,
                title=title,
                issued_date=issued_date,
                withdrawn_date=withdrawn_date,
                replacement_ref=replacement_ref,
                detail_url=detail_url,
            )
        )

    logger.info("parse.ok", count=len(results))
    return results


def crawl(client: httpx.Client | None = None) -> WithdrawnCrawlResult:
    """Fetch + parse the withdrawn-circulars list page."""
    response, html = fetch_list_page(client=client)
    raw_sha = hashlib.sha256(response.content).hexdigest()
    withdrawn = parse_list_html(html, base_url=str(response.url))
    return WithdrawnCrawlResult(
        fetched_at=datetime.utcnow().isoformat() + "Z",
        source_url=LIST_URL,
        final_url=str(response.url),
        status_code=response.status_code,
        raw_html=html,
        raw_html_sha256=raw_sha,
        withdrawn_circulars=withdrawn,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host in {"rbi.org.in", "m.rbi.org.in"}:
        host = "www.rbi.org.in"
    elif host == "":
        return url
    return parsed._replace(netloc=host).geturl()


def _pick_longest_text(texts: list[str]) -> str:
    """The title is usually the longest cell in the row."""
    return max(texts, key=len, default="")


def _extract_rbi_ref(text: str) -> str | None:
    """Find the first RBI reference number in a string."""
    match = RBI_REF_PATTERN.search(text)
    if not match:
        return None
    return _clean_text(match.group(0))


def _extract_two_dates(text: str) -> tuple[str | None, str | None]:
    """Try to extract two dates from a row (issue, withdrawal).

    Tries every known RBI date format (with both full and abbreviated month
    names). If only one date is found, it's returned as the issue date and
    withdrawal is None. If none are found, returns (None, None).

    Heuristic: earlier date = issue, later date = withdrawal. Usually correct.
    """
    # Each regex pattern + a list of strptime formats to try (full + abbreviated months).
    pairs: list[tuple[str, list[str]]] = [
        (r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", ["%d %B %Y", "%d %b %Y"]),
        (r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b", ["%B %d, %Y", "%b %d, %Y"]),
        (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", ["%d/%m/%Y"]),
        (r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", ["%d-%m-%Y"]),
    ]
    iso_dates: list[str] = []
    for pattern, formats in pairs:
        for m in re.finditer(pattern, text):
            for fmt in formats:
                try:
                    dt = datetime.strptime(m.group(0), fmt)
                except ValueError:
                    continue
                iso = dt.date().isoformat()
                if iso not in iso_dates:
                    iso_dates.append(iso)
                break  # accepted; don't try more formats for this match

    if not iso_dates:
        return (None, None)
    if len(iso_dates) == 1:
        return (iso_dates[0], None)
    iso_dates.sort()
    return (iso_dates[0], iso_dates[-1])


def _extract_replacement_ref(text: str, original_ref: str | None) -> str | None:
    """Find an RBI ref that isn't the original — likely the withdrawing circular."""
    refs = RBI_REF_PATTERN.findall(text)
    cleaned = [_clean_text(r) for r in refs]
    if original_ref:
        cleaned = [r for r in cleaned if r != original_ref]
    if not cleaned:
        return None
    return cleaned[0]


def is_withdrawn_url(url: str) -> bool:
    """Quick predicate: does this URL look like a withdrawn-circulars page?"""
    return "NotificationUserWithdrawnCircular.aspx" in url


def is_notification_url(url: str) -> bool:
    """Quick predicate: does this URL look like a regular notification page?"""
    return "NotificationUser.aspx" in url and "Withdrawn" not in url


def is_master_direction_url(url: str) -> bool:
    """Quick predicate: does this URL look like a Master Directions detail page?"""
    return "BS_ViewMasDirections.aspx" in url
