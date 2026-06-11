"""Fetch a single press-release detail page (BS_PressReleaseDisplay.aspx?prid=N).

Reuses the PDF-extraction + HTML-fallback patterns from notif_detail; the
list of "what to look for" is identical (rbidocs PDFs, optional HTML body).
We just point at a different URL template.
"""

from __future__ import annotations

import hashlib

import httpx
import structlog

from .._time import iso_utc_now
from ._common import USER_AGENT, get_with_retry
from .notif_detail import (
    NotifDetailResult,
    _extract_html_body,
    _extract_pdf_urls,
    _extract_title,
)

logger = structlog.get_logger(__name__)

DETAIL_URL_TEMPLATE = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid={prid}"


def fetch_detail(prid: str, *, client: httpx.Client | None = None, timeout: float = 30.0) -> NotifDetailResult:
    """Reuses NotifDetailResult shape so the indexer can be polymorphic."""
    url = DETAIL_URL_TEMPLATE.format(prid=prid)
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
        logger.info("press_release_detail.fetch.start", prid=prid, url=url)
        resp = get_with_retry(client, url)
        html = resp.text
        title = _extract_title(html)
        primary, annexures = _extract_pdf_urls(html, base_url=str(resp.url))
        body_text = _extract_html_body(html) if not primary else None
        return NotifDetailResult(
            notif_id=prid,
            detail_url=url,
            final_url=str(resp.url),
            fetched_at=iso_utc_now(),
            status_code=resp.status_code,
            raw_html=html,
            raw_html_sha256=hashlib.sha256(resp.content).hexdigest(),
            title=title,
            primary_pdf_url=primary,
            annexure_pdf_urls=annexures,
            html_body_text=body_text,
        )
    finally:
        if own_client:
            client.close()
