"""Tests for the rbi.check_current tool — the headline DX guarantee.

Covers the v0.1 contract (revised after live crawl revealed the real URL
patterns). Every input gets a structured response, nothing silently fails:

    - BS_ViewMasDirections.aspx?id=...           → current / withdrawn / unknown
    - NotificationUser.aspx?Id=...               → withdrawn / not_withdrawn
                                                   (the real headline-demo path)
    - NotificationUserWithdrawnCircular.aspx     → list_page (it IS the list URL)
    - FAQView.aspx / FAQDisplay.aspx             → unsupported_at_v0.1, reason: faq_url
    - Textual "RBI/DOR/2020/106"                 → unsupported_at_v0.1, reason: textual_ref
    - Anything else                              → unsupported_at_v0.1, reason: unknown
    - Empty / whitespace                         → unsupported_at_v0.1, reason: unknown
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from rbi_source_mcp.check_current import check_current
from rbi_source_mcp.db import connect, upsert_md, upsert_withdrawn


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "test.sqlite"
    now = datetime.utcnow().isoformat() + "Z"
    with connect(db_path) as conn:
        # Seed: one current MD
        upsert_md(
            conn,
            md_id="12550",
            title="Master Direction - KYC, 2016 (Updated as on April 25, 2026)",
            detail_url="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12550",
            last_updated_at="2026-04-25",
            department=None,
            pdf_urls=[],
            status="current",
            now=now,
        )
        # Seed: one withdrawn circular
        upsert_withdrawn(
            conn,
            original_ref="RBI/DOR/2018/45",
            original_id="11234",
            title="Some withdrawn circular on prepaid wallets",
            issued_date="2018-03-15",
            withdrawn_date="2020-06-12",
            replacement_ref="RBI/DOR/2020/106",
            detail_url="https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=11234",
            now=now,
        )
    with connect(db_path) as conn:
        yield conn


def test_current_md_returns_status_current(db) -> None:
    result = check_current(
        db,
        "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12550",
    )
    assert result["status"] == "current"
    assert result["document_id"] == "rbi:master_direction:12550"
    assert "KYC" in result["title"]
    assert result["last_updated_at"] == "2026-04-25"


def test_unknown_md_id_returns_status_unknown(db) -> None:
    result = check_current(
        db,
        "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=99999",
    )
    assert result["status"] == "unknown"
    assert result["document_id"] == "rbi:master_direction:99999"
    assert "not in the current corpus" in result["note"]


def test_withdrawn_list_page_url_returns_list_page(db) -> None:
    """The bare list-page URL is not a per-circular URL — say so clearly."""
    result = check_current(
        db,
        "https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx",
    )
    assert result["status"] == "list_page"
    assert "list page" in result["note"].lower()


def test_notification_url_for_withdrawn_circular_returns_withdrawn(db) -> None:
    """The headline magical-moment path: paste a NotificationUser URL whose Id
    appears in the withdrawn-circulars list, get back 'withdrawn'."""
    result = check_current(
        db,
        "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=11234",
    )
    assert result["status"] == "withdrawn"
    assert result["withdrawn_date"] == "2020-06-12"
    # Replacement_ref is None at v0.1 (deferred); test that the field exists.
    assert "replacement_ref" in result


def test_notification_url_for_active_circular_returns_not_withdrawn(db) -> None:
    """Notification ID NOT in the withdrawn list AND not in active corpus →
    'not_withdrawn' with a caveat that the active corpus may not have full
    coverage of all general notifications."""
    result = check_current(
        db,
        "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=99999",
    )
    assert result["status"] == "not_withdrawn"
    # The note explains we couldn't find it; consuming LLMs surface this caveat.
    assert "not found" in result["note"].lower() or "haven't indexed" in result["note"].lower()


def test_faq_url_returns_unsupported(db) -> None:
    result = check_current(db, "https://www.rbi.org.in/Scripts/FAQView.aspx?Id=168")
    assert result["status"] == "unsupported_at_v0.1"
    assert result["reason"] == "faq_url"


def test_textual_ref_returns_unsupported(db) -> None:
    result = check_current(db, "RBI/DOR/2020/106")
    assert result["status"] == "unsupported_at_v0.1"
    assert result["reason"] == "textual_ref"


def test_garbage_input_returns_unsupported(db) -> None:
    result = check_current(db, "not a url, not a ref, just nonsense")
    assert result["status"] == "unsupported_at_v0.1"
    assert result["reason"] == "unknown"


def test_empty_input_never_raises(db) -> None:
    """Empty string must NOT raise; must return a structured response."""
    result = check_current(db, "")
    assert result["status"] == "unsupported_at_v0.1"
    assert result["reason"] == "unknown"


def test_whitespace_only_input_never_raises(db) -> None:
    result = check_current(db, "   \n\t  ")
    assert result["status"] == "unsupported_at_v0.1"


def test_response_always_has_source_url(db) -> None:
    """Every response must carry source_url so the consumer can link back."""
    inputs = [
        "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12550",
        "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=99999",
        "https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx?Id=11234",
        "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=42",
        "RBI/DOR/2020/106",
        "garbage",
        "",
    ]
    for url in inputs:
        result = check_current(db, url)
        assert "source_url" in result, f"missing source_url for input: {url!r}"
