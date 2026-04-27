"""The headline tool: rbi.check_current.

Given an RBI URL or textual reference, return current/withdrawn/superseded
plus the replacement document when known. v0.1 contract (revised after live
crawl revealed the real URL pattern of withdrawn circulars):

    Supported URL patterns (returns a definitive answer):
        - BS_ViewMasDirections.aspx?id=<MD_ID>          -- Master Direction lookup
        - NotificationUser.aspx?Id=<NOTIF_ID>           -- circular: checked against
                                                          the withdrawn list

    Note: NotificationUserWithdrawnCircular.aspx?Id=... is the LIST page URL,
    not a per-record URL. Those URLs return a structured "list_page" response.

    Unsupported patterns (returns structured "unsupported_at_v0.1"):
        - FAQView.aspx, FAQDisplay.aspx (FAQs, not in MVP)
        - Textual `RBI/...` or `DOR/...` references (parser ships at v1.0)
        - Anything else

The function NEVER raises and NEVER silently fails. Every input gets a
structured response. This is the load-bearing DX guarantee from the design doc.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlparse

from .crawler import md_list, withdrawn_list
from .db import find_md_by_id, find_withdrawn_by_original_id

NOTIF_URL_PATTERN = re.compile(r"NotificationUser\.aspx\?Id=(\d+)", re.IGNORECASE)
FAQ_URL_PATTERN = re.compile(r"FAQ(View|Display)\.aspx", re.IGNORECASE)
RBI_REF_PATTERN = re.compile(r"^\s*RBI[/][A-Za-z0-9./\s\-]+\d{4}[/\s\-]+\d+\s*$", re.IGNORECASE)


def check_current(conn: sqlite3.Connection, url_or_ref: str) -> dict[str, Any]:
    """Return a structured status response for an RBI URL or reference.

    Response shape (loose v0.1 envelope, hardens to the structured envelope at v1.0):

        Success / definitive:
            {
                "status": "current" | "withdrawn" | "superseded" | "unknown",
                "document_id": str | None,
                "title": str | None,
                "official_url": str,
                "last_updated_at": str | None,     # MD: "Updated as on" date (ISO 8601)
                "issued_date": str | None,         # withdrawn: original issue date (ISO 8601)
                "withdrawn_date": str | None,
                "replacement_ref": str | None,
                "as_of": str,                      # ISO 8601 of this lookup
                "source_url": str,                 # the URL the user passed in
            }

        Unsupported pattern:
            {
                "status": "unsupported_at_v0.1",
                "reason": "notification_url" | "faq_url" | "textual_ref" | "unknown",
                "suggestion": str,
                "source_url": str,
            }
    """
    from datetime import datetime

    now = datetime.utcnow().isoformat() + "Z"
    raw = (url_or_ref or "").strip()

    if not raw:
        return _unsupported(
            reason="unknown",
            suggestion="Empty input. Pass an RBI URL or RBI reference.",
            source_url=raw,
        )

    # Branch 1: looks like a Master Direction detail URL
    if md_list.DETAIL_URL_PATTERN.search(raw):
        md_id = md_list.md_id_from_url(raw)
        if not md_id:
            return _unsupported(
                reason="unknown",
                suggestion="URL looks like a Master Directions page but the ID could not be parsed.",
                source_url=raw,
            )
        row = find_md_by_id(conn, md_id)
        if row is None:
            return {
                "status": "unknown",
                "document_id": f"rbi:master_direction:{md_id}",
                "title": None,
                "official_url": raw,
                "issued_date": None,
                "withdrawn_date": None,
                "replacement_ref": None,
                "as_of": now,
                "source_url": raw,
                "note": "This Master Direction ID is not in the current corpus. It may be very new (not yet crawled) or never existed.",
            }
        return {
            "status": row["status"],
            "document_id": row["document_id"],
            "title": row["title"],
            "official_url": row["detail_url"],
            "last_updated_at": row["last_updated_at"],
            "withdrawn_date": None,  # MDs don't have a single withdrawn_date in v0.1
            "replacement_ref": None,
            "as_of": now,
            "source_url": raw,
        }

    # Branch 2: the withdrawn-list page URL itself (no per-record ID)
    if withdrawn_list.is_withdrawn_url(raw):
        return {
            "status": "list_page",
            "official_url": raw,
            "as_of": now,
            "source_url": raw,
            "note": (
                "This is the RBI Withdrawn Circulars list page, not a per-circular "
                "URL. To check a specific circular, paste its NotificationUser.aspx URL."
            ),
        }

    # Branch 3: a notification / circular URL — check it against the withdrawn list.
    # This is the real headline demo path: someone pastes a 2017 RBI URL, gets back
    # a "withdrawn" verdict if the circular is in our corpus.
    if NOTIF_URL_PATTERN.search(raw):
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        ids = qs.get("Id") or qs.get("id") or qs.get("ID") or []
        if not ids:
            return _unsupported(
                reason="unknown",
                suggestion="NotificationUser URL has no Id parameter.",
                source_url=raw,
            )
        original_id = ids[0]
        row = find_withdrawn_by_original_id(conn, original_id)
        if row is not None:
            return {
                "status": "withdrawn",
                "document_id": None,
                "title": row["title"],
                "official_url": row["detail_url"] or raw,
                "issued_date": row["issued_date"],
                "withdrawn_date": row["withdrawn_date"],
                "replacement_ref": row["replacement_ref"],
                "as_of": now,
                "source_url": raw,
                "note": (
                    "This circular is in RBI's withdrawn-circulars list. "
                    "Replacement reference and exact supersession chain ship at v1.0."
                ),
            }
        # Not found in withdrawn list. v0.1 doesn't have the full active-circular
        # corpus (only Master Directions), so we can't confirm it's CURRENT — we
        # can only say it's not in our withdrawn list.
        return {
            "status": "not_withdrawn",
            "document_id": None,
            "title": None,
            "official_url": raw,
            "as_of": now,
            "source_url": raw,
            "note": (
                "This circular is NOT in RBI's withdrawn-circulars list as of the "
                "last refresh, so it likely remains active. v0.1 does not crawl the "
                "active-circulars corpus, so we cannot definitively confirm 'current' "
                "status — only that it has not been withdrawn. Active-circulars "
                "coverage ships at v1.0."
            ),
        }

    # Branch 4: FAQ URL (out of v0.1 scope)
    if FAQ_URL_PATTERN.search(raw):
        return _unsupported(
            reason="faq_url",
            suggestion="FAQs are scoped for v1.1. The URL is a valid RBI FAQ page.",
            source_url=raw,
        )

    # Branch 5: looks like a textual RBI reference (out of v0.1 scope)
    if RBI_REF_PATTERN.match(raw):
        return _unsupported(
            reason="textual_ref",
            suggestion=(
                "Textual reference parsing ships at v1.0. For now, paste the RBI URL: "
                "BS_ViewMasDirections.aspx?id=... for a Master Direction, or "
                "NotificationUser.aspx?Id=... for a circular (we'll check whether it's "
                "been withdrawn)."
            ),
            source_url=raw,
        )

    # Branch 6: anything else
    return _unsupported(
        reason="unknown",
        suggestion=(
            "Unrecognized input. v0.1 supports two URL patterns: "
            "BS_ViewMasDirections.aspx?id=... (Master Directions) and "
            "NotificationUser.aspx?Id=... (circulars — checked against the "
            "withdrawn-circulars list). Other RBI URL types and textual "
            "references ship at v1.0."
        ),
        source_url=raw,
    )


def _unsupported(*, reason: str, suggestion: str, source_url: str) -> dict[str, Any]:
    """Build a v0.1 unsupported-pattern response."""
    return {
        "status": "unsupported_at_v0.1",
        "reason": reason,
        "suggestion": suggestion,
        "source_url": source_url,
    }
