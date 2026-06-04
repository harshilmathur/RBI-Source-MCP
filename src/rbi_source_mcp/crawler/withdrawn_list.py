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
from bs4 import BeautifulSoup, Tag

from ._common import USER_AGENT, get_with_retry

logger = structlog.get_logger(__name__)

LIST_URL = "https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx"
DETAIL_URL_PATTERN = re.compile(
    r"NotificationUserWithdrawnCircular\.aspx\?Id=(\d+)", re.IGNORECASE
)
NOTIF_URL_PATTERN = re.compile(r"NotificationUser\.aspx\?Id=(\d+)", re.IGNORECASE)
RBI_REF_PATTERN = re.compile(r"RBI[/\s][A-Za-z0-9./\s\-]+\d{4}[/\s\-]+\d+", re.IGNORECASE)


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
        response = get_with_retry(client, LIST_URL)
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

    The real rbi.org.in page is multi-section. Section layouts vary:
        - "Circulars Withdrawn" section (oldest): 3 cells [date, title, department]
        - "RRA 2.0" sections (en-masse withdrawals): 4 cells [sno, ref, title, date]

    Common across all sections: a real data row contains EITHER a
    NotificationUser.aspx anchor (the link back to the original circular) OR
    an identifiable RBI/agency reference number. Header rows have neither.

    For v0.1 we extract whatever we can and accept partial coverage:
        - original_id      from NotificationUser.aspx?Id= anchor when present
        - original_ref     from a cell that looks like an agency reference
        - title            longest cell
        - withdrawn_date   first cell that's primarily a date (or only date in row)
        - replacement_ref  deferred to v1.0 (requires detail-page crawls)
    """
    soup = BeautifulSoup(html, "lxml")

    results: list[WithdrawnCircular] = []
    # Dedupe key: prefer original_id; fall back to title hash for ID-less rows.
    seen_keys: set[tuple[str, str]] = set()

    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        cell_texts = [_clean_text(c.get_text(" ", strip=True)) for c in cells]
        joined = " ".join(cell_texts)

        # Heuristic: a real data row mentions at least one year.
        if not re.search(r"\b(19|20)\d{2}\b", joined):
            continue

        # Extract anchor → original_id, detail_url.
        original_id: str | None = None
        detail_url: str | None = None
        for anchor in row.find_all("a", href=True):
            href = anchor.get("href", "")
            notif_match = NOTIF_URL_PATTERN.search(href)
            if notif_match and original_id is None:
                original_id = notif_match.group(1)
                detail_url = _canonicalize_url(urljoin(base_url, href))
                break

        # Reference number: scan each cell for one. RBI/agency refs vary widely:
        #   "RBI/2015-16/100"
        #   "DNBS.(PD).CC.No.39/SCRC/26.03.001/2014-2015"
        #   "IECD.No.3/04.02.02/2002-03"
        # We accept any cell that looks ref-shaped and isn't a date or title.
        original_ref: str | None = None
        for cell_text in cell_texts:
            if _is_ref_like(cell_text):
                original_ref = cell_text
                break

        # Dates: prefer the two-dates path, then fall back to first date-shaped
        # cell. `_extract_two_dates` scans the joined row text for ALL dates and
        # returns (earliest, latest). RRA 2.0 sections typically have both
        # (issue + withdrawal); the older "Circulars Withdrawn" section has
        # only one date per row, which is the withdrawal date — since this is
        # a withdrawn-circulars list, a single date is semantically a withdrawal.
        earlier, later = _extract_two_dates(joined)
        if earlier and later:
            issued_date: str | None = earlier
            withdrawn_date: str | None = later
        elif earlier:
            issued_date = None
            withdrawn_date = earlier
        else:
            issued_date = None
            withdrawn_date = None
            # Last-resort: try cell-by-cell parse for unusual layouts.
            for cell_text in cell_texts:
                iso = _parse_date_anywhere(cell_text)
                if iso and _looks_like_date_cell(cell_text):
                    withdrawn_date = iso
                    break

        # Title: prefer the cell that contains the NotificationUser anchor;
        # else longest non-date, non-ref, non-serial cell.
        title = _pick_title(cells, cell_texts, original_ref=original_ref)
        if not title:
            continue

        # Dedup: prefer original_id; otherwise (title, ref) tuple as a stable key.
        dedupe_key: tuple[str, str] = (
            ("id", original_id) if original_id else ("title_ref", f"{title.lower()}|{original_ref or ''}")
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        results.append(
            WithdrawnCircular(
                withdrawn_id=None,
                original_ref=original_ref,
                original_id=original_id,
                title=title,
                issued_date=issued_date,
                withdrawn_date=withdrawn_date,
                replacement_ref=None,  # ships at v1.0
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


def _is_ref_like(text: str) -> bool:
    """Does this cell look like an RBI/agency reference number?

    Heuristics: short-ish (<200 chars), contains at least one '/' or '.', has
    digits and uppercase letters mixed in characteristic patterns. Excludes
    plain dates and title-shaped text.
    """
    if not text or len(text) > 200:
        return False
    if " " in text and len(text.split()) > 6:  # Long phrase = title, not ref.
        return False
    if not re.search(r"[A-Z]{2,}", text):  # Refs always have ALLCAPS chunks.
        return False
    if not re.search(r"[/.]", text):  # Refs always have / or . separators.
        return False
    if _parse_date_anywhere(text) and not re.search(r"[A-Z]{2,}.*[/.].*\d", text):
        return False
    # Recognizable patterns:
    #   "RBI/..."
    #   "DNBS.(PD).CC.No.39/..."
    #   "IECD.No.3/04.02.02/2002-03"
    if re.match(r"^[A-Z][A-Z0-9.()/_\-]*[/.]", text.strip()):
        return True
    return bool(text.upper().startswith("RBI"))


def _looks_like_date_cell(text: str) -> bool:
    """A cell that's mostly just a date (not a title that happens to mention a year)."""
    if not text:
        return False
    parsed = _parse_date_anywhere(text)
    if not parsed:
        return False
    # Date-only cells are short. Titles with embedded years are longer.
    return len(text) <= 25


def _parse_date_anywhere(text: str) -> str | None:
    """Try every known RBI date format in `text`. Returns ISO 8601 or None."""
    if not text:
        return None
    pairs: list[tuple[str, list[str]]] = [
        (r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", ["%d %B %Y", "%d %b %Y"]),
        (r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b", ["%B %d, %Y", "%b %d, %Y"]),
        (r"\b([A-Za-z]+)\s+(\d{1,2})\s+(\d{4})\b", ["%B %d %Y", "%b %d %Y"]),
        (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", ["%d/%m/%Y"]),
        (r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", ["%d-%m-%Y"]),
    ]
    for pattern, formats in pairs:
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


def _pick_title(cells: list[Tag], texts: list[str], *, original_ref: str | None) -> str:
    """Pick the title cell.

    Strategy:
      1. If a cell wraps an `<a href="...NotificationUser.aspx...">` anchor,
         prefer that cell's text (section 1 layout: date, title, dept).
      2. Otherwise pick the longest cell that isn't a date, ref, or serial
         (section 2 layout: sno, ref, title, date — title is the longest cell).
    """
    # 1. Prefer the cell containing the NotificationUser anchor.
    for cell, text in zip(cells, texts, strict=True):
        if not text:
            continue
        for anchor in cell.find_all("a", href=True):
            if NOTIF_URL_PATTERN.search(anchor.get("href", "")):
                return text

    # 2. Longest non-date, non-ref, non-serial cell.
    candidates = []
    for t in texts:
        if not t:
            continue
        if _looks_like_date_cell(t):
            continue
        if original_ref and t == original_ref:
            continue
        if _is_ref_like(t):  # Catches refs even if not the chosen original_ref.
            continue
        if re.match(r"^\d+\.?$", t):  # Serial number cells like "848." or "3286.".
            continue
        candidates.append(t)
    if not candidates:
        return ""
    return max(candidates, key=len)


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
