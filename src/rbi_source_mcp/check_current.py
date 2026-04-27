"""The headline tool: rbi.check_current.

Given an RBI URL or textual reference, return current/withdrawn/superseded
plus the replacement document when known. Locked v0.1 contract:

    Supported URL patterns (returns a definitive answer):
        - https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=<MD_ID>
        - https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx?Id=<NOTIF_ID>

    Unsupported patterns (returns structured "unsupported_at_v0.1"):
        - NotificationUser.aspx?Id=... (regular notifications, not in MVP)
        - FAQView.aspx, FAQDisplay.aspx (FAQs, not in MVP)
        - Textual `RBI/...` references (parser ships at v1.0)
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
                "issued_date": str | None,         # ISO 8601 if known
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
            "issued_date": row["issued_date"],
            "withdrawn_date": None,  # MDs don't have a single withdrawn_date in v0.1
            "replacement_ref": None,
            "as_of": now,
            "source_url": raw,
        }

    # Branch 2: looks like a withdrawn-circular URL
    if withdrawn_list.is_withdrawn_url(raw):
        # The withdrawn list page itself uses NotificationUserWithdrawnCircular.aspx?Id=
        # to identify the withdrawal record. We treat that ID as the original circular ID
        # for v0.1 — the page links from each withdrawal record back to the original
        # NotificationUser.aspx page using the same ID family.
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        ids = qs.get("Id") or qs.get("id") or qs.get("ID") or []
        if not ids:
            return _unsupported(
                reason="unknown",
                suggestion="Withdrawn-circular URL has no Id parameter.",
                source_url=raw,
            )
        original_id = ids[0]
        row = find_withdrawn_by_original_id(conn, original_id)
        if row is None:
            return {
                "status": "unknown",
                "document_id": None,
                "title": None,
                "official_url": raw,
                "issued_date": None,
                "withdrawn_date": None,
                "replacement_ref": None,
                "as_of": now,
                "source_url": raw,
                "note": "This withdrawal-circular ID is not in the current corpus.",
            }
        return {
            "status": "withdrawn",
            "document_id": None,  # Withdrawn circulars are not "documents" in the v0.1 schema
            "title": row["title"],
            "official_url": row["detail_url"] or raw,
            "issued_date": row["issued_date"],
            "withdrawn_date": row["withdrawn_date"],
            "replacement_ref": row["replacement_ref"],
            "as_of": now,
            "source_url": raw,
        }

    # Branch 3: regular notification URL (out of v0.1 scope)
    if NOTIF_URL_PATTERN.search(raw):
        return _unsupported(
            reason="notification_url",
            suggestion=(
                "MVP covers Master Directions only. Notifications and standalone "
                "circulars are scoped for v1.0. The URL points at an active RBI "
                "notification page."
            ),
            source_url=raw,
        )

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
                "Textual reference parsing ships at v1.0. For now, paste the RBI URL "
                "(BS_ViewMasDirections.aspx?id=... or NotificationUserWithdrawnCircular.aspx?Id=...) "
                "instead of the reference number."
            ),
            source_url=raw,
        )

    # Branch 6: anything else
    return _unsupported(
        reason="unknown",
        suggestion=(
            "Unrecognized input. v0.1 supports two URL patterns: "
            "BS_ViewMasDirections.aspx?id=... (Master Directions) and "
            "NotificationUserWithdrawnCircular.aspx?Id=... (withdrawn circulars). "
            "Other RBI URL types and textual references ship at v1.0."
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
