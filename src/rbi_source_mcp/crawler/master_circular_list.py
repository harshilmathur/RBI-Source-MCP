"""Crawl RBI's Master Circulars list across all departments.

Source: https://www.rbi.org.in/Scripts/BS_ViewMasterCirculardetails.aspx

Top-level page is a category index — 12 department-categorized links of the
form ?did=NNN. Each category page lists individual master circulars whose
PDFs link DIRECTLY to rbidocs.rbi.org.in (no detail-page hop).

Identity: Master Circulars don't have a stable numeric ID like notifications.
We derive a stable ID from a sha1 of the canonical PDF URL (first 12 chars).

Returns a flat list across all categories, with category captured as the
'department' field on each circular.
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

INDEX_URL = "https://www.rbi.org.in/Scripts/BS_ViewMasterCirculardetails.aspx"
CATEGORY_PATTERN = re.compile(r"BS_ViewMasterCirculardetails\.aspx\?did=(\d+)", re.IGNORECASE)

# Skip these footer/sidebar PDFs that appear on every category page.
NOISE_PDF_LABELS = {"rbi's vision and values", "accessibility statement"}
NOISE_PDF_PATTERNS = ["Utkarsh", "Accessibility20012026"]


@dataclass(slots=True)
class MasterCircular:
    mc_id: str           # sha1(pdf_url)[:12] — derived stable ID
    title: str
    pdf_url: str
    department: str      # category name (e.g., "Commercial Banking")
    detail_url: str      # the category page URL (the closest thing to a "detail page")

    @property
    def document_id(self) -> str:
        return f"rbi:master_circular:{self.mc_id}"


@dataclass(slots=True)
class MasterCircularCrawlResult:
    fetched_at: str
    source_url: str
    raw_html_sha256: str
    master_circulars: list[MasterCircular] = field(default_factory=list)
    categories_total: int = 0
    failed_categories: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """True only if categories were discovered and every one was fetched.

        The consumer (build_master_circular_index) checks this and fails closed
        rather than indexing a silently-partial master-circular family."""
        return self.categories_total > 0 and not self.failed_categories


def crawl(client: httpx.Client | None = None, *, timeout: float = 30.0) -> MasterCircularCrawlResult:
    """Fetch index page → 12 category pages → flatten master circulars."""
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
        logger.info("master_circular_list.fetch.index", url=INDEX_URL)
        resp = get_with_retry(client, INDEX_URL)
        index_html = resp.text
        raw_sha = hashlib.sha256(resp.content).hexdigest()

        categories = _parse_category_index(index_html, base_url=str(resp.url))
        logger.info("master_circular_list.categories", count=len(categories))
        if not categories:
            # Zero categories = the index layout changed or a WAF challenge was
            # served instead of the page. Returning an empty result would
            # silently ship an empty master-circular family; fail closed.
            raise RuntimeError(
                f"master-circular index yielded 0 categories from {INDEX_URL} "
                "(layout change or WAF block) — refusing to ship a partial corpus"
            )

        all_circulars: list[MasterCircular] = []
        seen_pdfs: set[str] = set()
        failed: list[str] = []
        for cat_label, cat_url in categories:
            try:
                cat_resp = get_with_retry(client, cat_url)
            except httpx.HTTPError as exc:
                # ERROR (not warning): a dropped category silently shrinks the
                # corpus. is_complete=False so the consumer can fail closed.
                logger.error(
                    "master_circular_list.category_fail", url=cat_url, label=cat_label, error=str(exc)
                )
                failed.append(cat_label)
                continue
            cat_circulars = _parse_category_page(
                cat_resp.text,
                base_url=str(cat_resp.url),
                department=cat_label,
                detail_url=cat_url,
                seen_pdfs=seen_pdfs,
            )
            all_circulars.extend(cat_circulars)
            logger.info("master_circular_list.category_done", department=cat_label, count=len(cat_circulars))

        if failed:
            logger.error("master_circular_list.partial", failed=failed, total=len(categories))

        return MasterCircularCrawlResult(
            fetched_at=iso_utc_now(),
            source_url=INDEX_URL,
            raw_html_sha256=raw_sha,
            master_circulars=all_circulars,
            categories_total=len(categories),
            failed_categories=failed,
        )
    finally:
        if own_client:
            client.close()


def _parse_category_index(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return [(category_label, full_url)] for the 12 department categories."""
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = CATEGORY_PATTERN.search(href)
        if not m:
            continue
        did = m.group(1)
        if did in seen:
            continue
        label = re.sub(r"\s+", " ", a.get_text(strip=True))
        if not label or len(label) < 3:
            continue
        seen.add(did)
        out.append((label, _canonicalize(urljoin(base_url, href))))
    return out


def _parse_category_page(
    html: str,
    *,
    base_url: str,
    department: str,
    detail_url: str,
    seen_pdfs: set[str],
) -> list[MasterCircular]:
    """A category page has rows where each row contains one master circular's
    PDF link plus a title in the row text. We scan rows; for any row with
    exactly one rbidocs PDF anchor and a substantive title in the row text,
    emit a MasterCircular."""
    soup = BeautifulSoup(html, "lxml")
    out: list[MasterCircular] = []
    for tr in soup.find_all("tr"):
        pdf_anchors = [
            a for a in tr.find_all("a", href=True)
            if "rbidocs.rbi.org.in" in a.get("href", "").lower()
            or a.get("href", "").lower().endswith(".pdf")
        ]
        if len(pdf_anchors) != 1:
            continue
        anchor = pdf_anchors[0]
        pdf_url = _canonicalize(urljoin(base_url, anchor.get("href", "")))
        if pdf_url in seen_pdfs:
            continue
        # Filter out noise PDFs (Utkarsh, Accessibility footer links).
        if any(noise in pdf_url for noise in NOISE_PDF_PATTERNS):
            continue
        # Title comes from the row text minus the file-size suffix.
        row_text = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
        title = re.sub(r"\s+\d{1,5}\s*kb\s*$", "", row_text, flags=re.IGNORECASE).strip()
        if not title or len(title) < 8:
            continue
        if title.lower() in NOISE_PDF_LABELS:
            continue
        seen_pdfs.add(pdf_url)
        mc_id = hashlib.sha1(pdf_url.encode("utf-8")).hexdigest()[:12]
        out.append(
            MasterCircular(
                mc_id=mc_id,
                title=title,
                pdf_url=pdf_url,
                department=department,
                detail_url=detail_url,
            )
        )
    return out


def _canonicalize(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host in {"rbi.org.in", "m.rbi.org.in"}:
        host = "www.rbi.org.in"
    # rbidocs sometimes serves http; force https.
    scheme = "https" if parsed.scheme in ("", "http", "https") else parsed.scheme
    return parsed._replace(netloc=host, scheme=scheme).geturl()
