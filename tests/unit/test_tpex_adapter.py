"""Unit tests for pure TPEx row-normalization logic (no network)."""

from taiwan_stock.catalog import tpex_adapter

# Trimmed but real field values from a live call to
# /openapi/v1/mopsfin_t187ap03_O, made while writing the adapter — see
# the module docstring.
MORNSUN_ROW = {
    "Date": "1150910",
    "SecuritiesCompanyCode": "1240",
    "CompanyName": "茂生農經股份有限公司",
    "CompanyAbbreviation": "茂生農經",
    "SecuritiesIndustryCode": "33",
    "StockTransferAgent": "元大證券股份有限公司股務代理部",
    "StockTransferAgentTelephone": "02-2586-5859",
    # Plain Gregorian (design 005 §4.5 follow-up), confirmed live
    # 2026-09-13 — 茂生農經 actually listed 2018-08-08.
    "DateOfListing": "20180808",
}


def test_normalize_stock_maps_core_fields():
    security = tpex_adapter.normalize_stock(MORNSUN_ROW)

    assert security == {
        "market": "TPEx",
        "code": "1240",
        "name": "茂生農經",
        "security_type": "stock",
        "industry": "33",
        "transfer_agent_name": "元大證券股份有限公司股務代理部",
        "transfer_agent_phone": "02-2586-5859",
        "status": "active",
        "listed_date": "2018-08-08",
    }


def test_normalize_stock_missing_listed_date_becomes_none():
    row = {**MORNSUN_ROW, "DateOfListing": ""}

    assert tpex_adapter.normalize_stock(row)["listed_date"] is None


def test_normalize_stock_falls_back_to_full_name_when_short_name_blank():
    row = {**MORNSUN_ROW, "CompanyAbbreviation": ""}

    security = tpex_adapter.normalize_stock(row)

    assert security["name"] == "茂生農經股份有限公司"


def test_normalize_stock_missing_transfer_agent_becomes_none():
    row = {
        **MORNSUN_ROW,
        "StockTransferAgent": "",
        "StockTransferAgentTelephone": "",
    }

    security = tpex_adapter.normalize_stock(row)

    assert security["transfer_agent_name"] is None
    assert security["transfer_agent_phone"] is None


def test_normalize_stock_decodes_stray_html_entities():
    # Same government open-data feed family as the TWSE adapter, where
    # this was confirmed live — defensive here even without a live
    # TPEx sighting (design 002 §5.2).
    row = {**MORNSUN_ROW, "StockTransferAgent": "&nbsp;元大證券股份有限公司股務代理部"}

    security = tpex_adapter.normalize_stock(row)

    assert security["transfer_agent_name"] == "元大證券股份有限公司股務代理部"


def test_normalize_stock_decodes_double_escaped_html_entities():
    # Matches the TWSE adapter's confirmed-live "&amp;nbsp;" case —
    # one unescape pass alone would leave a literal "&nbsp;" behind.
    row = {
        **MORNSUN_ROW,
        "StockTransferAgent": "&amp;nbsp;元大證券股份有限公司股務代理部",
    }

    security = tpex_adapter.normalize_stock(row)

    assert security["transfer_agent_name"] == "元大證券股份有限公司股務代理部"


# Real entries from a live call to `info.tpex.org.tw/api/etf?lang=zh-tw`
# (design 004 §4.2) — confirmed to return all 119 currently-listed
# OTC ETFs as "CODE NAME" strings in one request.
def test_normalize_etf_list_entry_splits_code_and_name():
    entry = tpex_adapter.normalize_etf_list_entry("00719B 元大美債1-3")

    assert entry == {
        "market": "TPEx",
        "code": "00719B",
        "name": "元大美債1-3",
        "security_type": "etf",
        "industry": None,
        "transfer_agent_name": None,
        "transfer_agent_phone": None,
        "status": "active",
    }


def test_normalize_etf_list_entry_handles_active_fund_name_with_space():
    # Live-observed: codes for actively-managed funds (like "00980D
    # 主動聯博投等入息") still split cleanly on the first space —
    # confirming `split(" ", 1)` rather than a naive full split is the
    # right call in general, even though this particular example has
    # no further spaces.
    entry = tpex_adapter.normalize_etf_list_entry("00980D 主動聯博投等入息")

    assert entry["code"] == "00980D"
    assert entry["name"] == "主動聯博投等入息"


def test_normalize_etf_list_entry_rejects_malformed_entry():
    assert tpex_adapter.normalize_etf_list_entry("no-space-here") is None
    assert tpex_adapter.normalize_etf_list_entry("") is None


# Trimmed but real field values from a live call to
# https://info.tpex.org.tw/api/etfExDiv (design 004 §4.2), POST with
# stkNo=00719B&startDate=20260101&endDate=20261231&lang=zh-tw — the
# long legal disclaimer in "description" is replaced with a short
# placeholder since that field isn't mapped to anything.
def test_normalize_etf_distributions_maps_announced_amount():
    rows = [
        {
            "amount": "0.27",
            "stockName": "元大美債1-3",
            "divDate": "115年07月21日",
            "year": "115",
            "inDate": "115年08月10日",
            "description": "…",
            "inBaseDate": "115年07月27日",
            "stockNo": "00719B",
        }
    ]

    events = tpex_adapter.normalize_etf_distributions(rows)

    assert events == [
        {
            "code": "00719B",
            "ex_dividend_date": "2026-07-21",
            "record_date": "2026-07-27",
            "pay_date": "2026-08-10",
            "amount_per_share": "0.2700",
            "amount_status": "final",
        }
    ]


def test_normalize_etf_distributions_blank_amount_becomes_unknown_status():
    # Live-observed (2026-09) on 00980D: an undecided amount is an
    # empty string here, not TWSE's JSON `null` — `clean_amount`
    # already treats both as "no amount yet" so no special-casing is
    # needed for the difference (design 004 §4.2).
    rows = [
        {
            "amount": "",
            "stockName": "主動聯博投等入息",
            "divDate": "115年09月16日",
            "year": "115",
            "inDate": "115年10月13日",
            "description": "…",
            "inBaseDate": "115年09月22日",
            "stockNo": "00980D",
        }
    ]

    events = tpex_adapter.normalize_etf_distributions(rows)

    assert events[0]["pay_date"] == "2026-10-13"
    assert events[0]["amount_per_share"] is None
    assert events[0]["amount_status"] == "unknown"


def test_normalize_etf_distributions_empty_list_returns_empty_list():
    # Live-observed (2026-09): an unknown/invalid stkNo returns a bare
    # empty JSON array, not an error — "nothing announced".
    assert tpex_adapter.normalize_etf_distributions([]) == []
