"""Unit tests for pure TWSE row-normalization logic (no network)."""

import pytest

from taiwan_stock.catalog import twse_adapter

# Trimmed but real field values from a live call to
# /opendata/t187ap03_L for company 2330 (TSMC), made while writing the
# adapter — see the module docstring.
TSMC_ROW = {
    "公司代號": "2330",
    "公司名稱": "台灣積體電路製造股份有限公司",
    "公司簡稱": "台積電",
    "產業別": "24",
    "股票過戶機構": "中國信託商業銀行 代理部",
    "過戶電話": "02-6636-5566",
    # Plain Gregorian, unlike every other date field these two adapters
    # otherwise handle — confirmed live 2026-09-13 against TSMC's real
    # 1994-09-05 listing date (t187ap03_L, unlike t187ap47_L below).
    "上市日期": "19940905",
}

# Trimmed but real field values from a live call to
# /opendata/t187ap47_L for fund 0050 (元大台灣50).
ETF_0050_ROW = {
    "基金代號": "0050",
    "基金簡稱": "元大台灣50",
    "基金中文名稱": "元大台灣卓越50證券投資信託基金",
    "標的指數/追蹤指數名稱": "臺灣50指數",
    "保管機構": "臺灣集中保管結算所股份有限公司",
    "上市日期": "0920630",  # ROC date — 0050 actually listed 2003-06-30.
}


def test_normalize_stock_maps_core_fields():
    security = twse_adapter.normalize_stock(TSMC_ROW)

    assert security == {
        "market": "TWSE",
        "code": "2330",
        "name": "台積電",
        "security_type": "stock",
        "industry": "24",
        "transfer_agent_name": "中國信託商業銀行 代理部",
        "transfer_agent_phone": "02-6636-5566",
        "status": "active",
        "listed_date": "1994-09-05",
    }


def test_normalize_stock_missing_listed_date_becomes_none():
    row = {**TSMC_ROW, "上市日期": ""}

    assert twse_adapter.normalize_stock(row)["listed_date"] is None


def test_normalize_stock_falls_back_to_full_name_when_short_name_blank():
    row = {**TSMC_ROW, "公司簡稱": ""}

    security = twse_adapter.normalize_stock(row)

    assert security["name"] == "台灣積體電路製造股份有限公司"


def test_normalize_stock_missing_transfer_agent_becomes_none():
    row = {**TSMC_ROW, "股票過戶機構": "", "過戶電話": ""}

    security = twse_adapter.normalize_stock(row)

    assert security["transfer_agent_name"] is None
    assert security["transfer_agent_phone"] is None


def test_normalize_etf_maps_raw_custodian_institution_and_benchmark():
    security, profile = twse_adapter.normalize_etf(ETF_0050_ROW)

    assert security == {
        "market": "TWSE",
        "code": "0050",
        "name": "元大台灣50",
        "security_type": "etf",
        "industry": None,
        "transfer_agent_name": None,
        "transfer_agent_phone": None,
        "status": "active",
        "listed_date": "2003-06-30",
    }
    # Design 004 §4.3.1: this raw value is frequently 集保 or the 投信
    # itself, not the real custodian bank, so it only ever lands in the
    # reference-only raw field — `custodian_bank` stays unset here.
    assert profile["custodian_institution_raw"] == "臺灣集中保管結算所股份有限公司"
    assert profile["benchmark"] == "臺灣50指數"


def test_normalize_etf_leaves_unverified_fields_as_none():
    _, profile = twse_adapter.normalize_etf(ETF_0050_ROW)

    assert profile["custodian_bank"] is None
    assert profile["distribution_frequency"] is None
    assert profile["fee_summary"] is None


def test_normalize_etf_decodes_stray_html_entities_in_custodian_institution_raw():
    # Live-observed (2026-09): 00729R/00767's 保管機構 carries a
    # *double*-escaped "&amp;nbsp;" instead of a plain space — a single
    # html.unescape() pass only gets to the still-literal "&nbsp;",
    # which Jinja then re-escapes for display right back into
    # "&amp;nbsp;". The repeated unescape must fully remove it.
    row = {**ETF_0050_ROW, "保管機構": "&amp;nbsp;臺灣集中保管結算所股份有限公司"}

    _, profile = twse_adapter.normalize_etf(row)

    assert profile["custodian_institution_raw"] == "臺灣集中保管結算所股份有限公司"


def test_normalize_etf_single_escaped_nbsp_is_also_fully_cleaned():
    row = {**ETF_0050_ROW, "保管機構": "&nbsp;臺灣集中保管結算所股份有限公司"}

    _, profile = twse_adapter.normalize_etf(row)

    assert profile["custodian_institution_raw"] == "臺灣集中保管結算所股份有限公司"


def test_normalize_stock_decodes_stray_html_entities_in_transfer_agent():
    row = {**TSMC_ROW, "股票過戶機構": "中國信託商業銀行&amp;代理部"}

    security = twse_adapter.normalize_stock(row)

    assert security["transfer_agent_name"] == "中國信託商業銀行&代理部"


# Trimmed but real field values from a live call to
# https://www.twse.com.tw/rwd/zh/ETF/etfDiv?stkNo=00878&startDate=2026&
# endDate=2026&response=json (design 004 §4.2) — the long legal
# disclaimer in "收益分配標準" is replaced with a short placeholder since
# that field isn't mapped to anything and its exact text doesn't matter.
ETF_DISTRIBUTION_FIELDS = [
    "證券代號",
    "證券簡稱",
    "除息交易日",
    "收益分配基準日",
    "收益分配發放日",
    "收益分配金額 (每1受益權益單位)",
    "收益分配標準 (102年度起啟用)",
    "公告年度",
]


def test_normalize_etf_distributions_maps_announced_amount():
    payload = {
        "status": "ok",
        "fields": ETF_DISTRIBUTION_FIELDS,
        "data": [
            [
                "00878",
                "國泰永續高股息",
                "115年08月18日",
                "115年08月24日",
                "115年09月11日",
                "1.01",
                "…",
                115,
            ]
        ],
    }

    events = twse_adapter.normalize_etf_distributions(payload)

    assert events == [
        {
            "code": "00878",
            "ex_dividend_date": "2026-08-18",
            "record_date": "2026-08-24",
            "pay_date": "2026-09-11",
            "amount_per_share": "1.0100",
            "amount_status": "final",
        }
    ]


def test_normalize_etf_distributions_null_amount_becomes_unknown_status():
    # Live-observed (2026-09) on 00918: the pay date is already known
    # (2026-10-15) but the amount is JSON `null` — an announced date
    # with an undecided amount, not "no event" (design 004 §2.2).
    payload = {
        "status": "ok",
        "fields": ETF_DISTRIBUTION_FIELDS,
        "data": [
            [
                "00918",
                "大華優利高填息30",
                "115年09月18日",
                "115年09月26日",
                "115年10月15日",
                None,
                "…",
                115,
            ]
        ],
    }

    events = twse_adapter.normalize_etf_distributions(payload)

    assert events[0]["pay_date"] == "2026-10-15"
    assert events[0]["amount_per_share"] is None
    assert events[0]["amount_status"] == "unknown"


def test_normalize_etf_distributions_empty_data_returns_empty_list():
    # Live-observed (2026-09): an unknown/invalid stkNo still returns
    # `"status":"ok"` with an empty `data` list, not an error — this is
    # "nothing announced", not a failure.
    payload = {"status": "ok", "fields": ETF_DISTRIBUTION_FIELDS, "data": []}

    assert twse_adapter.normalize_etf_distributions(payload) == []


def test_normalize_etf_distributions_strips_fixed_width_code_padding():
    # Live-observed 2026-09-13 (00400A and ~100 other codes): TWSE's
    # `證券代號`/`證券簡稱` columns are right-padded with spaces to a
    # fixed width. Comparing the unstripped code against the requested
    # one (design 005 §4.4's mismatch check) always "failed" even for a
    # genuine same-code event — this asserts it comes back clean.
    payload = {
        "status": "ok",
        "fields": ETF_DISTRIBUTION_FIELDS,
        "data": [
            [
                "00878    ",
                "國泰永續高股息    ",
                "115年08月18日",
                "115年08月24日",
                "115年09月11日",
                "1.01",
                "…",
                115,
            ]
        ],
    }

    events = twse_adapter.normalize_etf_distributions(payload)

    assert events[0]["code"] == "00878"


def test_normalize_etf_distributions_skips_no_distribution_sentinel_row():
    # Live-observed 2026-09-13 (00400A/2024 and ~100 other year/ETF
    # combinations): for a year with no distribution, TWSE sometimes
    # returns one sentinel row (blank date fields, amount literally
    # "無收益分配", status "(尚無資料)") instead of an empty `data` list.
    # Treated as a malformed event (no `ex_dividend_date`) it used to
    # fail the whole unit as "配息事件缺少有效除息日" — it must instead
    # resolve the same as an empty `data` list: no events, not an error.
    payload = {
        "status": "ok",
        "fields": ETF_DISTRIBUTION_FIELDS,
        "data": [
            [
                "00400A    ",
                "主動國泰動能高息    ",
                "",
                "",
                "",
                "無收益分配",
                "(尚無資料)",
                "",
            ]
        ],
    }

    assert twse_adapter.normalize_etf_distributions(payload) == []


def test_normalize_etf_distributions_fails_safe_on_unexpected_fields():
    # If TWSE ever renames/reorders these fields, indexing by name
    # means we report a parse failure rather than silently reading the
    # wrong column (design 004 §4.2's "依 fields 建立欄位索引").
    payload = {
        "status": "ok",
        "fields": ["some", "unexpected", "shape"],
        "data": [["00878", "國泰永續高股息", "x"]],
    }

    with pytest.raises(ValueError, match="必要欄位"):
        twse_adapter.normalize_etf_distributions(payload)
