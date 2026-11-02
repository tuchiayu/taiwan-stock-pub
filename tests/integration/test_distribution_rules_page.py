"""Design 046 on the securities page: quarterly groups, 0%, 不配息, mismatch."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import query, sync_history
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.web import pages_securities

ETFS = {
    # id: (code, profile columns, ex-dividend dates)
    "g1": (
        "00901",
        {"distribution_frequency": "quarterly", "custodian_bank": "元大銀行"},
        ["2026-04-15", "2026-07-15"],
    ),
    "g3": ("00903", {"distribution_frequency": "quarterly"}, ["2026-09-16"]),
    "g2": (
        "00902",
        {"distribution_frequency": "quarterly", "custodian_bank": "元大商業銀行"},
        ["2026-02-10", "2026-05-10"],
    ),
    "nd": ("00904", {"distribution_policy": "non_distributing"}, []),
    "an": ("00905", {"distribution_frequency": "annual"}, []),
    "mm": (
        "00906",
        {
            "distribution_frequency": "quarterly",
            "distribution_frequency_inferred": json.dumps(
                {
                    "value": "monthly",
                    "method": "year_count",
                    "basis_year": 2025,
                    "year_count": 12,
                }
            ),
        },
        ["2026-06-20", "2026-09-20"],
    ),
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    for module in (query, pages_securities):
        monkeypatch.setattr(module, "today_taipei", lambda: "2026-10-29")
    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    for sid, (code, profile, days) in ETFS.items():
        conn.execute(
            "INSERT INTO securities(id,market,code,name,security_type,status,"
            "listed_date) VALUES(?,'TWSE',?,?,'etf','active','2024-07-01')",
            (sid, code, f"測試{code}"),
        )
        columns = ["security_id", *profile]
        conn.execute(
            f"INSERT INTO etf_profiles({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})",
            (sid, *profile.values()),
        )
        for i, day in enumerate(days):
            conn.execute(
                "INSERT INTO distributions(id,security_id,ex_dividend_date,"
                "amount_per_share,amount_status) VALUES(?,?,?,'1','final')",
                (f"{sid}{i}", sid, day),
            )
        conn.execute(
            "INSERT INTO daily_prices(security_id,trade_date,close) "
            "VALUES(?,'2026-10-28','100')",
            (sid,),
        )
    with sync_history.run(conn, "twse_etf_distribution_list") as task:
        for sid in ETFS:
            for year in ("2024", "2025", "2026"):
                key = f"{sid}:{year}"
                task.plan(key, security_id=sid, period=year)
                task.record(key, "success")
    conn.commit()
    conn.close()
    with TestClient(create_app(tmp_path)) as test_client:
        yield test_client


def codes(html):
    table = re.search(r'<table class="securities-table.*?</table>', html, re.S)[0]
    return re.findall(r'<th scope="row" class="col-code"><a[^>]*>(\w+)</a>', table)


def row(html, code):
    return re.search(
        rf'<th scope="row" class="col-code"><a[^>]*>{code}</a>.*?</tr>', html, re.S
    )[0]


def get(client, **params):
    return client.get(
        "/securities", params={"security_type": "etf", "view": "all", **params}
    ).text


def test_quarterly_groups_filter_including_provisional(client):
    assert codes(get(client, distribution_frequency="quarterly_1")) == ["00901"]
    assert codes(get(client, distribution_frequency="quarterly_2")) == ["00902"]
    # 00903 paid once in September: provisionally the 3、6、9、12 group,
    # alongside 00906 (June and September).
    assert sorted(codes(get(client, distribution_frequency="quarterly_3"))) == [
        "00903",
        "00906",
    ]
    assert sorted(codes(get(client, distribution_frequency="quarterly"))) == [
        "00901",
        "00902",
        "00903",
        "00906",
    ]
    html = get(client)
    # Design 047: each option carries its count; provisional groups included.
    assert '<option value="quarterly">季配（4）</option>' in html
    assert '<option value="quarterly_1">季配・1、4、7、10 月（1）</option>' in html
    assert '<option value="quarterly_2">季配・2、5、8、11 月（1）</option>' in html
    assert '<option value="quarterly_3">季配・3、6、9、12 月（2）</option>' in html
    assert '<option value="bimonthly">雙月配（0）</option>' in html
    assert "暫定：3、6、9、12 月" in row(html, "00903")
    # Design 048 §4.2.5: the table keeps 暫定 but drops the 歷史 prefix.
    assert ">1、4、7、10 月</span>" in row(html, "00901")
    assert "歷史：" not in row(html, "00901")


def test_frequency_counts_follow_the_bank_filter(client):
    # Design 047: both 元大 spellings are one bank, and the frequency counts
    # apply the bank filter but not the frequency filter itself.
    html = get(client, distribution_frequency="quarterly_3")
    assert '<option value="元大商業銀行">元大商業銀行（0）</option>' not in html
    assert ">元大銀行（" not in get(client)
    assert '<option value="元大商業銀行">元大商業銀行（2）</option>' in get(client)
    yuanta = get(client, custodian_bank="元大商業銀行")
    assert codes(yuanta) == ["00901", "00902"]
    assert '<option value="quarterly">季配（2）</option>' in yuanta
    assert '<option value="quarterly_3">季配・3、6、9、12 月（0）</option>' in yuanta
    assert '<option value="unknown">資料不足（0）</option>' in yuanta


def test_frequency_sort_lines_quarterly_up_by_group(client):
    quarterly = [
        c
        for c in codes(get(client, sort="frequency", direction="asc"))
        if c in ("00901", "00902", "00903", "00906")
    ]
    assert quarterly[0] == "00901"
    assert quarterly[1] == "00902"
    assert sorted(quarterly[2:]) == ["00903", "00906"]


def test_yield_column_labels(client):
    html = get(client)
    assert "不配息" in row(html, "00904")
    assert "尚無已知配息紀錄" not in row(html, "00904")
    annual = row(html, "00905")
    assert "0.00%" in annual and "近一年未配息" in annual
    # One payment so far: 1 × 4 ÷ 100.
    provisional = row(html, "00903")
    assert "4.00%" in provisional and "推估（依 1 次）" in provisional


def test_official_and_record_mismatch_is_shown(client):
    assert "官方與紀錄不符（2025 年配 12 次）" in row(get(client), "00906")
