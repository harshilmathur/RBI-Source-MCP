"""Crawl a single NotificationUser.aspx?Id= detail page.

Source: https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=<NOTIF_ID>

Detail-page shape varies by circular:
    - Circulars from 2020+ usually have a primary PDF attached
      (rbidocs.rbi.org.in/...PDF) plus optional annexures.
    - Older or shorter circulars sometimes have NO PDF — the body lives
      directly in HTML on the detail page.
    - Some pages have neither a PDF nor a substantive HTML body (e.g.,
      dead links). The indexer skips those with `extract.empty`.

This module returns whatever it can find. The indexer downstream picks
between PDF extraction and HTML-body extraction.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

from .._time import iso_utc_now
from ._common import USER_AGENT, get_with_retry

logger = structlog.get_logger(__name__)

DETAIL_URL_TEMPLATE = "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id={notif_id}"


@dataclass(slots=True)
class NotifDetailResult:
    notif_id: str
    detail_url: str
    final_url: str
    fetched_at: str
    status_code: int
    raw_html: str
    raw_html_sha256: str
    title: str | None
    primary_pdf_url: str | None
    annexure_pdf_urls: list[str] = field(default_factory=list)
    html_body_text: str | None = None  # Falls back to this when no PDF exists


def fetch_detail(notif_id: str, *, client: httpx.Client | None = None, timeout: float = 30.0) -> NotifDetailResult:
    """Fetch + parse one NotificationUser detail page."""
    url = DETAIL_URL_TEMPLATE.format(notif_id=notif_id)
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
        logger.info("notif_detail.fetch.start", notif_id=notif_id, url=url)
        resp = get_with_retry(client, url)
        html = resp.text
        raw_sha = hashlib.sha256(resp.content).hexdigest()
        title = _extract_title(html)
        primary, annexures = _extract_pdf_urls(html, base_url=str(resp.url))
        body_text = _extract_html_body(html) if not primary else None
        logger.info(
            "notif_detail.fetch.ok",
            notif_id=notif_id,
            status=resp.status_code,
            primary_pdf=bool(primary),
            annexure_count=len(annexures),
            html_body_chars=len(body_text or ""),
        )
        return NotifDetailResult(
            notif_id=notif_id,
            detail_url=url,
            final_url=str(resp.url),
            fetched_at=iso_utc_now(),
            status_code=resp.status_code,
            raw_html=html,
            raw_html_sha256=raw_sha,
            title=title,
            primary_pdf_url=primary,
            annexure_pdf_urls=annexures,
            html_body_text=body_text,
        )
    finally:
        if own_client:
            client.close()


def _extract_title(html: str) -> str | None:
    """Detail pages put the circular title in a heading or strong tag near the top."""
    soup = BeautifulSoup(html, "lxml")
    # Strategy: look for the largest heading-ish element in the first 5 KB.
    head = soup.find("h1") or soup.find("h2")
    if head:
        text = re.sub(r"\s+", " ", head.get_text(" ", strip=True))
        if 5 < len(text) < 300:
            return text
    title_tag = soup.find("title")
    if title_tag:
        text = re.sub(r"\s+", " ", title_tag.get_text(" ", strip=True))
        if 5 < len(text) < 300:
            return text
    return None


def _extract_pdf_urls(html: str, base_url: str) -> tuple[str | None, list[str]]:
    """Find PDF links on the detail page.

    Same heuristic as md_detail: anchors ending in .PDF/.pdf or pointing at
    rbidocs.rbi.org.in. The "primary" is the first unlabeled-text anchor;
    fallback is the first PDF link.
    """
    soup = BeautifulSoup(html, "lxml")
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        lower = href.lower()
        if not (lower.endswith(".pdf") or "rbidocs.rbi.org.in" in lower):
            continue
        full_url = _canonicalize_url(urljoin(base_url, href))
        if full_url in seen:
            continue
        seen.add(full_url)
        label = re.sub(r"\s+", " ", anchor.get_text(strip=True))
        candidates.append((label, full_url))

    if not candidates:
        return None, []

    primary: str | None = None
    annexures: list[str] = []
    for label, url in candidates:
        if not label and primary is None:
            primary = url
        else:
            annexures.append(url)
    if primary is None:
        primary = candidates[0][1]
        annexures = [u for _, u in candidates[1:]]
    return primary, annexures


def _extract_html_body(html: str) -> str | None:
    """Pull a usable text body from the detail page when no PDF is attached.

    RBI detail pages wrap the circular body in various nested div/table
    structures. Strategy: find the longest text block that looks like prose
    (multiple sentences, > 200 chars). Returns None if nothing qualifies.
    """
    soup = BeautifulSoup(html, "lxml")
    # Strip noisy elements.
    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "form"]):
        tag.decompose()
    # Try a few common content containers first.
    for selector in ["#PageContent", "#contentDiv", "#tablecontainer", "main", ".tablecontainer"]:
        node = soup.select_one(selector)
        if node:
            text = re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True))
            if len(text) > 200:
                return text
    # Fallback: grab all text and trim repeated whitespace.
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return text if len(text) > 200 else None


def _canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host in {"rbi.org.in", "m.rbi.org.in"}:
        host = "www.rbi.org.in"
    return parsed._replace(netloc=host).geturl()
