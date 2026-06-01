"""The headline tool: rbi_check_current.

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


#: Canonical key set for every success/definitive response. `_response()`
#: guarantees these keys are present on every dict it returns, so callers
#: never have to branch on `status` to know which fields exist.
_RESPONSE_KEYS: tuple[str, ...] = (
    "status",
    "document_id",
    "title",
    "official_url",
    "rbi_ref",
    "issued_date",
    "document_family",
    "last_updated_at",
    "withdrawn_date",
    "replacement_ref",
    "as_of",
    "source_url",
    "note",
)


def _response(**fields: Any) -> dict[str, Any]:
    """Build a success/definitive response with the canonical key set.

    Every key in `_RESPONSE_KEYS` is present in the returned dict; unspecified
    fields are filled with `None`. This eliminates the "which keys exist for
    which status?" branching that v0.1 leaked to callers.
    """
    out: dict[str, Any] = {k: None for k in _RESPONSE_KEYS}
    out.update(fields)
    # Unknown keys are still allowed (forward-compat for callers passing
    # extra context); we just guarantee the canonical set is always present.
    return out


def check_current(conn: sqlite3.Connection, url_or_ref: str) -> dict[str, Any]:
    """Return a structured status response for an RBI URL or reference.

    Response shape:

        Success / definitive (canonical envelope — every key always present;
        unknown fields are `None`):
            {
                "status": "current" | "withdrawn" | "superseded" | "unknown" | "list_page" | "not_withdrawn",
                "document_id": str | None,
                "title": str | None,
                "official_url": str | None,
                "rbi_ref": str | None,
                "issued_date": str | None,         # ISO 8601
                "document_family": str | None,
                "last_updated_at": str | None,     # MD: "Updated as on" date (ISO 8601)
                "withdrawn_date": str | None,
                "replacement_ref": str | None,
                "as_of": str,                      # ISO 8601 of this lookup
                "source_url": str,                 # the URL the user passed in
                "note": str | None,
            }

        Unsupported pattern (separate envelope, intentionally different shape):
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
            return _response(
                status="unknown",
                document_id=f"rbi:master_direction:{md_id}",
                official_url=raw,
                document_family="master_direction",
                as_of=now,
                source_url=raw,
                note=(
                    "This Master Direction ID is not in the current corpus. "
                    "It may be very new (not yet crawled) or never existed."
                ),
            )
        return _response(
            status=row["status"],
            document_id=row["document_id"],
            title=row["title"],
            official_url=row["detail_url"],
            document_family="master_direction",
            last_updated_at=row["last_updated_at"],
            as_of=now,
            source_url=raw,
        )

    # Branch 2: the withdrawn-list page URL itself (no per-record ID)
    if withdrawn_list.is_withdrawn_url(raw):
        return _response(
            status="list_page",
            official_url=raw,
            as_of=now,
            source_url=raw,
            note=(
                "This is the RBI Withdrawn Circulars list page, not a per-circular "
                "URL. To check a specific circular, paste its NotificationUser.aspx URL."
            ),
        )

    # Branch 3: a notification / circular URL.
    # Lookup priority:
    #   1. withdrawn list — definitive 'withdrawn'
    #   2. active circulars in our corpus (standalone_circular / notification) — 'current'
    #   3. neither — 'not_withdrawn' with a caveat that active coverage may be partial
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

        # Step 1: withdrawn list.
        row = find_withdrawn_by_original_id(conn, original_id)
        if row is not None:
            return _response(
                status="withdrawn",
                title=row["title"],
                official_url=row["detail_url"] or raw,
                rbi_ref=row["original_ref"],
                issued_date=row["issued_date"],
                document_family="notification",
                withdrawn_date=row["withdrawn_date"],
                replacement_ref=row["replacement_ref"],
                as_of=now,
                source_url=raw,
                note="This circular is in RBI's withdrawn-circulars list.",
            )

        # Step 2: active circulars indexed under standalone_circular / notification.
        active = conn.execute(
            """
            SELECT document_id, title, detail_url, rbi_ref, issued_date,
                   document_family, last_seen_at
            FROM documents
            WHERE md_id = ?
              AND document_family IN ('standalone_circular', 'notification')
              AND status = 'current'
            LIMIT 1
            """,
            (original_id,),
        ).fetchone()
        if active is not None:
            return _response(
                status="current",
                document_id=active["document_id"],
                title=active["title"],
                official_url=active["detail_url"],
                rbi_ref=active["rbi_ref"],
                issued_date=active["issued_date"],
                document_family=active["document_family"],
                as_of=now,
                source_url=raw,
                note=(
                    "Found in our indexed active-circulars corpus. Last seen in the "
                    f"source list on {active['last_seen_at']}."
                ),
            )

        # Step 3: nothing matched. Honest about what we know vs. don't.
        return _response(
            status="not_withdrawn",
            official_url=raw,
            as_of=now,
            source_url=raw,
            note=(
                "Not found in our indexed corpus (neither withdrawn list nor active "
                "Standalone Circulars). The circular may be a general notification "
                "we haven't indexed yet, or may have been issued before our crawl "
                "window. Visit the official_url to read the source directly."
            ),
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
