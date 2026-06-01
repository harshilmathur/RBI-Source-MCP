"""Tests for the withdrawn-circulars list parser."""

from __future__ import annotations

from rbi_source_mcp.crawler import withdrawn_list

# Two-section fixture mimicking the real rbi.org.in withdrawn-circulars page:
# - Section 1: 3 cells (date, title, department) — "Circulars Withdrawn"
# - Section 2: 4 cells (sno, ref, title, date)   — "RRA 2.0" en-masse withdrawals
FIXTURE_HTML = """
<html>
<body>
<table>
  <tr>
    <td>August 21, 2019</td>
    <td>
      <a href="NotificationUser.aspx?Id=11234">
        Some withdrawn circular on prepaid wallets
      </a>
    </td>
    <td>Department of Payment and Settlement Systems</td>
  </tr>
  <tr>
    <td>848.</td>
    <td>DNBS.(PD).CC.No.39/SCRC/26.03.001/2014-2015</td>
    <td>Notification on securitisation companies and reconstruction companies</td>
    <td>July 1, 2014</td>
  </tr>
</table>
</body>
</html>
"""


def test_parse_returns_two_records() -> None:
    rows = withdrawn_list.parse_list_html(FIXTURE_HTML)
    assert len(rows) == 2


def test_section_1_extracts_original_id_from_anchor() -> None:
    """Section 1 rows have a NotificationUser anchor → original_id captured."""
    rows = withdrawn_list.parse_list_html(FIXTURE_HTML)
    section_1 = next((r for r in rows if r.original_id == "11234"), None)
    assert section_1 is not None
    assert "prepaid wallets" in section_1.title


def test_section_2_extracts_ref_when_no_anchor() -> None:
    """Section 2 rows are anchor-less; the agency reference goes in original_ref."""
    rows = withdrawn_list.parse_list_html(FIXTURE_HTML)
    section_2 = next(
        (r for r in rows if r.original_ref and r.original_ref.startswith("DNBS")),
        None,
    )
    assert section_2 is not None
    assert section_2.original_id is None  # No anchor.
    assert section_2.original_ref == "DNBS.(PD).CC.No.39/SCRC/26.03.001/2014-2015"


def test_extracts_dates_from_either_section() -> None:
    """Date column is at index 0 in section 1, index 3 in section 2 — both work."""
    rows = withdrawn_list.parse_list_html(FIXTURE_HTML)
    by_id = {r.original_id: r for r in rows if r.original_id}
    assert by_id["11234"].withdrawn_date == "2019-08-21"
    section_2 = next(r for r in rows if r.original_ref and r.original_ref.startswith("DNBS"))
    assert section_2.withdrawn_date == "2014-07-01"


def test_replacement_ref_is_none_at_v0_1() -> None:
    """Replacement ref ships at v1.0 (requires detail-page crawls)."""
    rows = withdrawn_list.parse_list_html(FIXTURE_HTML)
    assert all(r.replacement_ref is None for r in rows)


def test_row_with_two_dates_populates_issued_and_withdrawn() -> None:
    """RRA 2.0 rows can carry both an issue date and a withdrawal date.

    Regression: prior to the fix, `issued_date` was hard-coded to `None` on
    every withdrawn row, so the "earlier date → issued, later date → withdrawn"
    information from `_extract_two_dates` was discarded.
    """
    fixture = """
    <html><body><table>
      <tr>
        <td>123.</td>
        <td>DOR.NBFC.FIN.012/03.10.001/2014</td>
        <td>Master Direction withdrawn under the RRA 2.0 review</td>
        <td>March 1, 2014 (issued); September 30, 2022 (withdrawn)</td>
      </tr>
    </table></body></html>
    """
    rows = withdrawn_list.parse_list_html(fixture)
    assert len(rows) == 1
    row = rows[0]
    assert row.issued_date == "2014-03-01"
    assert row.withdrawn_date == "2022-09-30"


def test_extract_two_dates_helper_orders_chronologically() -> None:
    """The helper itself should return (earlier, later) when two dates present."""
    earlier, later = withdrawn_list._extract_two_dates(
        "issued 5 January 2010; withdrawn 30 September 2022"
    )
    assert earlier == "2010-01-05"
    assert later == "2022-09-30"


def test_url_predicates() -> None:
    assert withdrawn_list.is_withdrawn_url(
        "https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx?Id=42"
    )
    assert not withdrawn_list.is_withdrawn_url(
        "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=42"
    )
    assert withdrawn_list.is_notification_url(
        "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=42"
    )
    assert not withdrawn_list.is_notification_url(
        "https://www.rbi.org.in/Scripts/NotificationUserWithdrawnCircular.aspx?Id=42"
    )
    assert withdrawn_list.is_master_direction_url(
        "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=12550"
    )
