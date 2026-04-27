"""Tests for the Master Directions list crawler.

These exercise the parser against handwritten fixture HTML that mimics RBI's
ASP.NET layout. They do NOT hit rbi.org.in. Live-fetch tests live in a
separate `test_live_*` module that's skipped in default CI runs.
"""

from __future__ import annotations

from rbi_source_mcp.crawler import md_list

FIXTURE_LIST_HTML = """
<html>
<body>
<h2>Department of Regulation</h2>
<table>
  <tr>
    <td>1</td>
    <td>
      <a href="BS_ViewMasDirections.aspx?id=12550">
        Master Direction - Reserve Bank of India (Know Your Customer (KYC)) Directions, 2016
      </a>
    </td>
    <td>Apr 25, 2026</td>
    <td>
      <a href="https://rbidocs.rbi.org.in/rdocs/notification/PDFs/SAMPLE.PDF">PDF</a>
    </td>
  </tr>
  <tr>
    <td>2</td>
    <td>
      <a href="https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=11947">
        Master Direction on Digital Lending
      </a>
    </td>
    <td>02/09/2022</td>
    <td>&nbsp;</td>
  </tr>
</table>
<h3>Department of Payment and Settlement Systems</h3>
<table>
  <tr>
    <td>1</td>
    <td>
      <a href="/Scripts/BS_ViewMasDirections.aspx?id=11142">
        Master Direction on Prepaid Payment Instruments
      </a>
    </td>
    <td>August 27, 2021</td>
  </tr>
</table>
</body>
</html>
"""


def test_parse_returns_three_records() -> None:
    mds = md_list.parse_list_html(FIXTURE_LIST_HTML)
    assert len(mds) == 3
    ids = {m.md_id for m in mds}
    assert ids == {"12550", "11947", "11142"}


def test_canonicalizes_relative_urls() -> None:
    mds = md_list.parse_list_html(FIXTURE_LIST_HTML)
    by_id = {m.md_id: m for m in mds}
    # Relative href becomes absolute under the LIST_URL base.
    assert by_id["12550"].detail_url.startswith("https://www.rbi.org.in/")
    assert by_id["12550"].detail_url.endswith("BS_ViewMasDirections.aspx?id=12550")


def test_picks_up_pdf_links_in_same_row() -> None:
    mds = md_list.parse_list_html(FIXTURE_LIST_HTML)
    by_id = {m.md_id: m for m in mds}
    assert by_id["12550"].pdf_urls == ["https://rbidocs.rbi.org.in/rdocs/notification/PDFs/SAMPLE.PDF"]
    assert by_id["11947"].pdf_urls == []


def test_extracts_dates_in_multiple_formats() -> None:
    mds = md_list.parse_list_html(FIXTURE_LIST_HTML)
    by_id = {m.md_id: m for m in mds}
    assert by_id["12550"].issued_date == "2026-04-25"
    assert by_id["11947"].issued_date == "2022-09-02"
    assert by_id["11142"].issued_date == "2021-08-27"


def test_extracts_department_heading() -> None:
    mds = md_list.parse_list_html(FIXTURE_LIST_HTML)
    by_id = {m.md_id: m for m in mds}
    # Department comes from the nearest preceding heading.
    assert by_id["12550"].department == "Department of Regulation"
    assert by_id["11142"].department == "Department of Payment and Settlement Systems"


def test_md_id_from_url_round_trips() -> None:
    assert md_list.md_id_from_url("https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12550") == "12550"
    assert md_list.md_id_from_url("/Scripts/BS_ViewMasDirections.aspx?id=11947") == "11947"
    assert md_list.md_id_from_url("https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=999") is None
    assert md_list.md_id_from_url("not a url at all") is None


def test_diff_detects_added_changed_removed() -> None:
    prev = [
        md_list.MasterDirection(md_id="A", title="Old A", detail_url="u/A", issued_date=None),
        md_list.MasterDirection(md_id="B", title="B unchanged", detail_url="u/B", issued_date=None),
        md_list.MasterDirection(md_id="C", title="To remove", detail_url="u/C", issued_date=None),
    ]
    curr = [
        md_list.MasterDirection(md_id="A", title="A retitled", detail_url="u/A", issued_date=None),
        md_list.MasterDirection(md_id="B", title="B unchanged", detail_url="u/B", issued_date=None),
        md_list.MasterDirection(md_id="D", title="New D", detail_url="u/D", issued_date=None),
    ]
    added, changed, removed = md_list.diff_master_directions(prev, curr)
    assert {m.md_id for m in added} == {"D"}
    assert {m.md_id for m in changed} == {"A"}
    assert set(removed) == {"C"}


def test_parse_is_idempotent() -> None:
    """Same HTML in produces identical records out."""
    a = md_list.parse_list_html(FIXTURE_LIST_HTML)
    b = md_list.parse_list_html(FIXTURE_LIST_HTML)
    assert [m.md_id for m in a] == [m.md_id for m in b]
    assert [m.title for m in a] == [m.title for m in b]


def test_handles_unicode_title() -> None:
    """Devanagari script in titles should round-trip cleanly."""
    html = """
    <table>
      <tr>
        <td>
          <a href="BS_ViewMasDirections.aspx?id=99999">
            Master Direction - प्रयोग (test) - 2026
          </a>
        </td>
        <td>Apr 25, 2026</td>
      </tr>
    </table>
    """
    mds = md_list.parse_list_html(html)
    assert len(mds) == 1
    assert "प्रयोग" in mds[0].title


def test_ignores_non_md_anchors() -> None:
    html = """
    <table>
      <tr>
        <td><a href="NotificationUser.aspx?Id=12345">Some notification</a></td>
        <td>Apr 25, 2026</td>
      </tr>
      <tr>
        <td><a href="BS_ViewMasDirections.aspx?id=42">Real MD</a></td>
        <td>Apr 25, 2026</td>
      </tr>
    </table>
    """
    mds = md_list.parse_list_html(html)
    assert len(mds) == 1
    assert mds[0].md_id == "42"
