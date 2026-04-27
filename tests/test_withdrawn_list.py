"""Tests for the withdrawn-circulars list parser."""

from __future__ import annotations

from rbi_source_mcp.crawler import withdrawn_list

FIXTURE_HTML = """
<html>
<body>
<table>
  <tr>
    <th>Sr</th><th>Ref</th><th>Subject</th><th>Issue Date</th><th>Withdrawn</th>
  </tr>
  <tr>
    <td>1</td>
    <td>RBI/DOR/2018/45</td>
    <td>
      <a href="NotificationUser.aspx?Id=11234">
        Some withdrawn circular on prepaid wallets
      </a>
      (withdrawn vide RBI/DOR/2020/106)
    </td>
    <td>15/03/2018</td>
    <td>12/06/2020</td>
  </tr>
  <tr>
    <td>2</td>
    <td>RBI/DPSS/2015/22</td>
    <td>
      <a href="https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=9876">
        Older payment-systems circular
      </a>
    </td>
    <td>10 Jan 2015</td>
    <td>5 May 2019</td>
  </tr>
</table>
</body>
</html>
"""


def test_parse_returns_two_records() -> None:
    rows = withdrawn_list.parse_list_html(FIXTURE_HTML)
    assert len(rows) == 2


def test_extracts_original_id() -> None:
    rows = withdrawn_list.parse_list_html(FIXTURE_HTML)
    ids = {r.original_id for r in rows}
    assert ids == {"11234", "9876"}


def test_extracts_dates() -> None:
    rows = withdrawn_list.parse_list_html(FIXTURE_HTML)
    by_id = {r.original_id: r for r in rows}
    assert by_id["11234"].issued_date == "2018-03-15"
    assert by_id["11234"].withdrawn_date == "2020-06-12"
    assert by_id["9876"].issued_date == "2015-01-10"
    assert by_id["9876"].withdrawn_date == "2019-05-05"


def test_extracts_replacement_ref_when_present() -> None:
    rows = withdrawn_list.parse_list_html(FIXTURE_HTML)
    by_id = {r.original_id: r for r in rows}
    # Row 1 mentions a replacement ref; row 2 does not.
    assert by_id["11234"].replacement_ref == "RBI/DOR/2020/106"
    assert by_id["9876"].replacement_ref is None


def test_extracts_original_ref() -> None:
    rows = withdrawn_list.parse_list_html(FIXTURE_HTML)
    by_id = {r.original_id: r for r in rows}
    assert by_id["11234"].original_ref == "RBI/DOR/2018/45"
    assert by_id["9876"].original_ref == "RBI/DPSS/2015/22"


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
