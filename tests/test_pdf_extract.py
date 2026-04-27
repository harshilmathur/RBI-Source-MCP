"""Tests for PDF text extraction helpers (RBI ref + issue date parsing)."""

from __future__ import annotations

from rbi_source_mcp.extractor.pdf import (
    parse_issue_date_from_pdf_text,
    parse_rbi_ref_from_pdf_text,
)

# Real header structure from the Payment Aggregator Master Direction PDF.
PA_HEADER = """\
                                   भारतीय �रज़वर् बैंक
                                RESERVE BANK OF INDIA
                                          www.rbi.org.in


RBI/DPSS/2025-26/141
CO.DPSS.POLC.No.S-633/02-14-008/2025-26                                 September 15, 2025

All Payment System Providers and Payment System Participants /
All Authorised Dealer banks /
All Scheduled Commercial Banks
"""


def test_parse_rbi_ref_finds_canonical_form() -> None:
    assert parse_rbi_ref_from_pdf_text(PA_HEADER) == "RBI/DPSS/2025-26/141"


def test_parse_rbi_ref_handles_alternate_format() -> None:
    text = "Some preamble\n\nRBI/2024-25/108 DOR.RET.REC.55/12.01.001/2024-25\nMore text"
    assert parse_rbi_ref_from_pdf_text(text) == "RBI/2024-25/108"


def test_parse_rbi_ref_returns_none_when_absent() -> None:
    assert parse_rbi_ref_from_pdf_text("no reference at all here") is None


def test_parse_issue_date_full_month_with_comma() -> None:
    """The PA MD format: 'September 15, 2025' next to the ref number."""
    assert parse_issue_date_from_pdf_text(PA_HEADER) == "2025-09-15"


def test_parse_issue_date_short_month() -> None:
    text = "RBI/DPSS/2024-25/100\nXYZ.123  Sep 5, 2024\nDear Sir,"
    assert parse_issue_date_from_pdf_text(text) == "2024-09-05"


def test_parse_issue_date_dmy_with_full_month() -> None:
    text = "RBI/2023-24/55\nABC.987   15 March 2024\nMadam"
    assert parse_issue_date_from_pdf_text(text) == "2024-03-15"


def test_parse_issue_date_slash_format() -> None:
    text = "RBI/2022-23/22  XYZ.111   15/09/2022   To"
    assert parse_issue_date_from_pdf_text(text) == "2022-09-15"


def test_parse_issue_date_returns_none_when_no_date() -> None:
    text = "RBI/2024-25/100\nNo date in this header at all\nDear Sir"
    assert parse_issue_date_from_pdf_text(text) is None


def test_parse_issue_date_only_scans_header() -> None:
    """Don't pick up a date that appears deep inside the body text."""
    body = "x" * 3000 + "\nJanuary 1, 2030"  # fake date well past header band
    assert parse_issue_date_from_pdf_text(body) is None


def test_parse_issue_date_rejects_implausible_year() -> None:
    """A bare 'January 1, 1850' shouldn't parse — RBI MDs are 1990+."""
    text = "RBI/x/1\nABC   January 1, 1850   Dear Sir"
    assert parse_issue_date_from_pdf_text(text) is None
