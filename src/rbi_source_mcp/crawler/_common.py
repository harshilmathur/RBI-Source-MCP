"""Shared crawler helpers.

Centralizes the bits that were copy-pasted across every `crawler/*.py`
module — USER_AGENT (one of 9 copies carried a typo), basic text cleanup,
URL canonicalization, and date parsing.

Each crawler family still owns its own page-parsing logic (the RBI list
pages have wildly different shapes); this file only holds the truly
repeatable helpers.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlparse

import httpx
import structlog

logger = structlog.get_logger(__name__)

#: Canonical User-Agent string identifying this client to rbi.org.in.
#: Prior to PR 2, eight crawler modules carried the right value and one
#: (`md_detail.py`) had a typo (`"messaging.rbi-source-mcp"`). Pinning here
#: removes the drift surface entirely.
USER_AGENT = "rbi-source-mcp/0.1 (+https://github.com/harshilmathur/RBI-Source-MCP)"

#: HTTP status codes worth retrying. RBI's WAF/anti-bot layer intermittently
#: returns 418 ("I'm a teapot" / unknown) or 403 to requests from datacenter
#: IP ranges (this is what breaks scheduled CI corpus builds — a runner IP gets
#: flagged on one attempt and clears on the next); 429 is rate limiting; 5xx are
#: transient server errors. Non-retryable 4xx (404, 410, …) raise immediately.
_RETRYABLE_STATUS: frozenset[int] = frozenset({403, 408, 418, 425, 429, 500, 502, 503, 504})


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    attempts: int = 3,
    backoff_base: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: object,
) -> httpx.Response:
    """GET ``url`` with retry + exponential backoff on transient failures.

    Retries on `_RETRYABLE_STATUS` responses (RBI WAF 418/403, 429, 5xx) and on
    `httpx.TransportError` (connect/read/timeout). Backoff is
    ``backoff_base * 2**attempt`` seconds (~2s, ~4s by default for 3 attempts).

    The final attempt — whether a retryable status or the last transport error —
    raises exactly as before (`raise_for_status()` / the transport exception),
    so callers keep their existing fail-closed behavior; a non-retryable status
    (e.g. 404) raises on the first attempt without waiting. `sleep` is injectable
    so tests don't actually block; extra kwargs forward to `client.get` (e.g. a
    per-request `headers=`).
    """
    for attempt in range(attempts):
        is_last = attempt == attempts - 1
        try:
            resp = client.get(url, **kwargs)  # type: ignore[arg-type]
        except httpx.TransportError as exc:
            if is_last:
                raise
            logger.warning(
                "crawler.fetch.retry",
                url=url,
                attempt=attempt + 1,
                attempts=attempts,
                error=str(exc),
            )
            sleep(backoff_base * (2**attempt))
            continue
        if resp.status_code in _RETRYABLE_STATUS and not is_last:
            logger.warning(
                "crawler.fetch.retry",
                url=url,
                attempt=attempt + 1,
                attempts=attempts,
                status=resp.status_code,
            )
            sleep(backoff_base * (2**attempt))
            continue
        resp.raise_for_status()
        return resp
    raise AssertionError("get_with_retry: loop exhausted without return/raise")  # unreachable


def clean_text(text: str | None) -> str:
    """Collapse all whitespace runs to a single space and strip ends.

    Several crawler modules defined a private `_clean_text` / `_clean`
    helper that did exactly this; they all stay backwards-compatible via
    a one-line wrapper around this function.
    """
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def canonicalize_url(url: str) -> str:
    """Normalize an rbi.org.in URL by collapsing alternate host names.

    The RBI site is reachable as `rbi.org.in`, `www.rbi.org.in`, and
    `m.rbi.org.in`; we canonicalize all three to `www.rbi.org.in` so
    downstream dedup keys (URL → document_id) don't fragment.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host in {"rbi.org.in", "m.rbi.org.in"}:
        host = "www.rbi.org.in"
    elif host == "":
        return url
    return parsed._replace(netloc=host).geturl()


#: Pattern → list of strptime format strings to try in order.
#: Union of every date variant the per-module parsers were attempting
#: independently (full month, abbreviated month, with/without comma, ISO).
_DATE_PATTERNS: list[tuple[str, list[str]]] = [
    (r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", ["%d %B %Y", "%d %b %Y"]),
    (r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b", ["%B %d, %Y", "%b %d, %Y"]),
    (r"\b([A-Za-z]+)\s+(\d{1,2})\s+(\d{4})\b", ["%B %d %Y", "%b %d %Y"]),
    (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", ["%d/%m/%Y"]),
    (r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", ["%d-%m-%Y"]),
]


def parse_date_anywhere(text: str) -> str | None:
    """Try every known RBI date format in `text`. Returns ISO 8601 or None.

    Per-module copies of this helper had drifted (one variant added a
    no-comma fallback; another did a `.replace(',', ', ')` repair pass)
    so the union of formats lives here. If a caller needs a stricter
    contract — e.g. "must be a single date occupying the whole string" —
    it should layer that check on top of this helper, not redefine it.
    """
    if not text:
        return None
    for pattern, formats in _DATE_PATTERNS:
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


def extract_all_dates(text: str) -> list[str]:
    """Return every ISO date found in `text`, ordered by appearance, deduped.

    Useful when a row carries multiple dates (e.g. issue date + withdrawal
    date) and the caller wants to assign them by chronological order.
    """
    out: list[str] = []
    if not text:
        return out
    for pattern, formats in _DATE_PATTERNS:
        for m in re.finditer(pattern, text):
            for fmt in formats:
                try:
                    dt = datetime.strptime(m.group(0), fmt)
                except ValueError:
                    continue
                iso = dt.date().isoformat()
                if iso not in out:
                    out.append(iso)
                break
    return out
