"""SITCA monthly size and TDCC weekly holder parsing (design 044 §4.2.2)."""

import httpx2
import pytest

from taiwan_stock.catalog import etf_scale_sources as sources

HEADER = [
    "序號",
    "證券代號",
    "證券簡稱",
    "基金規模(台幣)",
    "單位淨值(台幣)",
    "受益權單位數",
    "總受益人數",
]


def sitca_table(rows, header=HEADER):
    cells = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows
    )
    head = "<tr>" + "".join(f"<th>{c}</th>" for c in header) + "</tr>"
    return f"<html><body><table>{head}{cells}</table></body></html>"


def sitca_client(table, months=("202608", "202609")):
    options = "".join(f'<option value="{m}">{m}</option>' for m in months)
    requested = []

    def handler(request):
        requested.append(request.url)
        if request.url.path.endswith("etf_statement.aspx"):
            return httpx2.Response(200, text=f'<select name="ddlYM">{options}</select>')
        return httpx2.Response(200, text=table)

    return httpx2.Client(transport=httpx2.MockTransport(handler)), requested


def tdcc_client(text):
    body = ("﻿" + text).encode("utf-8")
    return httpx2.Client(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, content=body)
        )
    )


TDCC_HEAD = "資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"


def test_sitca_uses_latest_month_and_header_positions():
    size, nav, units = "2,469,507,995,378", "111.6600", "22,115,500,000"
    table = sitca_table(
        [
            ["1", "0050", "元大台灣50", size, nav, units, "3,794,877"],
            ["2", "00940", "高息", "66,780,183,903", "13.18", "1", "317,988"],
            ["", "-", "合計", "16,754,000,000,000", "-", "-", "-"],
        ]
    )
    client, requested = sitca_client(table)
    period, rows, url, skipped = sources.fetch_sitca_sizes(client)
    assert period == "2026-09"
    assert requested[1].params["txtYM"] == "202609"
    assert "etf_statement2.aspx" in url
    assert rows["0050"] == {
        "net_assets_twd": 2_469_507_995_378,
        "nav_twd": "111.6600",
        "units": 22_115_500_000,
    }
    assert set(rows) == {"0050", "00940"}
    assert skipped == 0


def test_sitca_skips_a_placeholder_row_but_keeps_the_month():
    table = sitca_table(
        [
            ["1", "0050", "元大台灣50", "2,469,507,995,378", "111.66", "1", "1"],
            ["2", "00999", "新基金", "-", "-", "-", "-"],
        ]
    )
    period, rows, _, skipped = sources.fetch_sitca_sizes(sitca_client(table)[0])
    assert set(rows) == {"0050"} and skipped == 1


@pytest.mark.parametrize(
    ("table", "message"),
    [
        (
            sitca_table([["1", "0050", "x", "1"]], header=["序號", "證券代號", "規模"]),
            "格式變更",
        ),
        (sitca_table([]), "為空"),
        (
            sitca_table(
                [
                    ["1", "0050", "x", "1", "1", "1", "1"],
                    ["2", "0050", "x", "2", "1", "1", "1"],
                ]
            ),
            "代碼重複",
        ),
    ],
)  # fmt: skip
def test_sitca_rejects_changed_empty_or_conflicting_tables(table, message):
    with pytest.raises(ValueError, match=message):
        sources.fetch_sitca_sizes(sitca_client(table)[0])


def test_sitca_without_a_month_menu_fails():
    with pytest.raises(ValueError, match="報表月份"):
        sources.fetch_sitca_sizes(sitca_client(sitca_table([]), months=())[0])


def test_tdcc_reads_total_level_and_strips_padded_codes():
    text = TDCC_HEAD + (
        "20261022,0050  ,1,100,100,0.00\n"
        "20261022,0050  ,17,3720414,23055000000,100.00\n"
        "20261022,00940 ,17,307579,5000365000,100.00\n"
        "20261022,2330  ,17,1000,1000,100.00\n"
        "20261022,000218,17,1,422278902,100.00\n"
    )
    day, rows, _, skipped = sources.fetch_tdcc_holders(tdcc_client(text))
    assert day == "2026-10-22"
    assert rows["0050"] == {"holders": 3_720_414, "units": 23_055_000_000}
    assert rows["00940"]["holders"] == 307_579
    # Stocks share the file but are not ETFs; every ETF code starts with 00.
    assert "2330" not in rows and "000218" in rows
    assert skipped == 0


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("代號,人數\n0050,1\n", "欄位格式"),
        (TDCC_HEAD, "資料日期"),
        (
            TDCC_HEAD + "20261022,0050,17,1,1,100.00\n20261015,0056,17,1,1,100.00\n",
            "資料日期",
        ),
        (
            TDCC_HEAD + "20261022,0050,17,1,1,100.00\n20261022,0050 ,17,2,2,100.00\n",
            "代碼重複",
        ),
    ],
)
def test_tdcc_rejects_changed_empty_mixed_or_duplicated_files(text, message):
    with pytest.raises(ValueError, match=message):
        sources.fetch_tdcc_holders(tdcc_client(text))
