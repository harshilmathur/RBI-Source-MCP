"""Fetch a PDF from rbidocs.rbi.org.in.

The PDF host has F5/Big-IP `bobcmn` bot protection. Naive curl/httpx requests
return an HTML JavaScript challenge instead of the PDF. The bypass is purely
header-based: passing a browser User-Agent + a Referer pointing back to the
rbi.org.in detail page + the Sec-Fetch-* hints satisfies the WAF without
needing a JS-executing browser.

Discovery: tested with curl using full Chrome headers + Referer; got 200 +
application/pdf instead of 200 + text/html. No cookie session required;
each PDF GET stands alone.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger(__name__)

# A real Chrome 127 User-Agent. The WAF appears to validate User-Agent shape
# and the presence of Sec-Fetch-* hints.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-site",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass(slots=True)
class PdfFetchResult:
    """Result of fetching one PDF."""

    pdf_url: str
    referer: str
    fetched_at: str
    status_code: int
    bytes: int
    pdf_sha256: str
    local_path: Path
    is_pdf: bool         # False if the WAF served HTML instead
    content_type: str
    error: str | None = None


def fetch_pdf(
    pdf_url: str,
    *,
    referer: str,
    out_dir: Path,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> PdfFetchResult:
    """Download a PDF, save to `out_dir`, return metadata.

    The filename on disk is `<sha256-prefix>.pdf` so re-fetches are
    cache-friendly (hash-gating in the refresh pipeline can skip unchanged
    files entirely).

    `referer` MUST be the URL of the rbi.org.in page that links to this PDF.
    Without it, rbidocs serves the WAF challenge HTML instead of the file.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    headers = dict(BROWSER_HEADERS)
    headers["Referer"] = referer

    if client is None:
        client = httpx.Client(timeout=timeout, headers=headers, follow_redirects=True)
        own_client = True
    else:
        own_client = False

    try:
        logger.info("pdf_fetch.start", url=pdf_url, referer=referer)
        # We pass headers per-request because the caller may have given us a
        # client without browser headers configured.
        resp = client.get(pdf_url, headers=headers)
        content = resp.content
        content_type = resp.headers.get("content-type", "")
        is_pdf = content_type.startswith("application/pdf") and content[:4] == b"%PDF"
        sha = hashlib.sha256(content).hexdigest()
        local_path = out_dir / f"{sha[:16]}.pdf"
        local_path.write_bytes(content)
        if is_pdf:
            logger.info("pdf_fetch.ok", url=pdf_url, bytes=len(content), sha=sha[:16])
        else:
            logger.error(
                "pdf_fetch.not_a_pdf",
                url=pdf_url,
                content_type=content_type,
                bytes=len(content),
                hint="WAF challenge likely; verify Referer + browser headers.",
            )
        return PdfFetchResult(
            pdf_url=pdf_url,
            referer=referer,
            fetched_at=datetime.utcnow().isoformat() + "Z",
            status_code=resp.status_code,
            bytes=len(content),
            pdf_sha256=sha,
            local_path=local_path,
            is_pdf=is_pdf,
            content_type=content_type,
            error=None if is_pdf else f"not a PDF (content-type={content_type})",
        )
    finally:
        if own_client:
            client.close()
