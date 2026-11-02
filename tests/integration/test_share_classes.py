"""Foreign-currency share classes follow their TWD class (design 043)."""

import json

import httpx2
import pytest

from taiwan_stock.catalog import (
    profile_enrichment,
    profile_values,
    query,
    share_classes,
    sync,
    sync_history,
    sync_investment_trust,
    twse_adapter,
)
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection

TWD = "國外成分證券指數股票型基金"
FOREIGN = "國外成份/加掛外幣證券指數股票型基金"


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


def _etf(conn, code, name, *, tax_id, fund_type, benchmark, status="active", **extra):
    sid = sync.upsert_security(
        conn,
        {
            "market": "TWSE",
            "code": code,
            "name": name,
            "security_type": "etf",
            "industry": None,
            "transfer_agent_name": None,
            "transfer_agent_phone": None,
            "status": status,
        },
    )
    sync.upsert_etf_profile(
        conn,
        sid,
        {
            "custodian_institution_raw": None,
            "fee_summary": None,
            "benchmark": benchmark,
            "fund_tax_id": tax_id,
            "fund_type": fund_type,
        },
    )
    if extra:
        conn.execute(
            "UPDATE etf_profiles SET "
            + ",".join(f"{k}=:{k}" for k in extra)
            + " WHERE security_id=:sid",
            {**extra, "sid": sid},
        )
    conn.commit()
    return sid


def _cathay_client(requested):
    products = {
        "00636": ("66", "併入基金資產，不予分配", "中國信託商業銀行"),
        "00878": ("CN", "季配", "台新國際商業銀行"),
    }

    def handler(request):
        requested.append(str(request.url))
        if request.url.path.endswith("/GetETFList"):
            rows = [
                {"stockCode": code, "fundCode": fund}
                for code, (fund, _, _) in products.items()
            ]
            return httpx2.Response(
                200,
                json={"totalCount": len(rows), "result": rows, "returnCode": "2000"},
            )
        fund = request.url.params["fundCode"]
        allot, bank = next((a, b) for f, a, b in products.values() if f == fund)
        return httpx2.Response(
            200,
            json={"result": {"allotType": allot, "bank": bank}, "returnCode": "2000"},
        )

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_normalize_etf_keeps_fund_identity():
    _, profile = twse_adapter.normalize_etf(
        {
            "基金代號": "00636K",
            "基金簡稱": "國泰中國A50+U",
            "基金統一編號": " 42260528 ",
            "基金類型": FOREIGN,
            "上市日期": "1061024",
        }
    )
    assert profile["fund_tax_id"] == "42260528"
    assert profile["fund_type"] == FOREIGN


def test_pairs_need_tax_id_foreign_mark_same_index_and_one_active_base(conn):
    base = _etf(
        conn,
        "00636",
        "國泰中國A50",
        tax_id="1",
        fund_type=TWD,
        benchmark="富時中國A50指數",
    )
    child = _etf(
        conn,
        "00636K",
        "國泰中國A50+U",
        tax_id="1",
        fund_type=FOREIGN,
        benchmark="富時中國A50 指數",
    )
    # Different funds sharing an offshore agent's tax id (0080/0081).
    _etf(
        conn,
        "0080",
        "恒中國",
        tax_id="2",
        fund_type="境外指數股票型基金",
        benchmark="恒生H股指數",
    )
    _etf(
        conn,
        "0081",
        "恒香港",
        tax_id="2",
        fund_type="境外指數股票型基金",
        benchmark="恒生指數",
    )
    # Same tax id but a different index.
    _etf(conn, "00999", "其他", tax_id="3", fund_type=TWD, benchmark="A指數")
    _etf(conn, "00999K", "其他+U", tax_id="3", fund_type=FOREIGN, benchmark="B指數")
    # TWD class already delisted.
    _etf(
        conn,
        "00888",
        "舊",
        tax_id="4",
        fund_type=TWD,
        benchmark="C指數",
        status="delisted",
    )
    _etf(conn, "00888K", "舊+U", tax_id="4", fund_type=FOREIGN, benchmark="C指數")

    pairs = share_classes.pairs(conn)

    assert list(pairs) == [child]
    assert pairs[child]["id"] == base


def test_two_twd_candidates_are_ambiguous(conn):
    _etf(conn, "00001", "甲", tax_id="1", fund_type=TWD, benchmark="指數")
    _etf(conn, "00002", "乙", tax_id="1", fund_type=TWD, benchmark="指數")
    _etf(conn, "00001K", "甲+U", tax_id="1", fund_type=FOREIGN, benchmark="指數")
    assert share_classes.pairs(conn) == {}


def test_sync_inherits_twd_class_and_replaces_a_wrong_value(conn):
    base = _etf(
        conn,
        "00636",
        "國泰中國A50",
        tax_id="1",
        fund_type=TWD,
        benchmark="富時中國A50指數",
    )
    # Like 00625K: an earlier run stored another product's policy.
    wrong = json.dumps({"origin": "investment_trust", "raw": "季配息"})
    child = _etf(
        conn,
        "00636K",
        "國泰中國A50+U",
        tax_id="1",
        fund_type=FOREIGN,
        benchmark="富時中國A50指數",
        distribution_frequency="quarterly",
        distribution_frequency_source=wrong,
        distribution_policy="distributing",
        distribution_policy_source=wrong,
    )
    requested = []

    result = sync_investment_trust.sync_investment_trust_data(
        conn, _cathay_client(requested)
    )

    assert not any("00636K" in url for url in requested)
    assert sum("GetETFIntroduce" in url for url in requested) == 1
    rows = {
        r["security_id"]: dict(r) for r in conn.execute("SELECT * FROM etf_profiles")
    }
    assert (
        rows[child]["custodian_bank"]
        == rows[base]["custodian_bank"]
        == "中國信託商業銀行"
    )
    assert rows[child]["distribution_policy"] == "non_distributing"
    assert rows[child]["distribution_frequency"] is None
    source = json.loads(rows[child]["custodian_bank_source"])
    assert (source["origin"], source["from_code"], source["fund_tax_id"]) == (
        "share_class",
        "00636",
        "1",
    )
    report = sync_history.get_report(conn, result["run_id"])
    unit = next(u for u in report["units"] if u["unit_key"] == "國泰:00636K")
    assert unit["status"] == "success"
    assert unit["reason"] == "沿用主級別 00636"
    assert result["failed_units"] == []


def test_child_stays_empty_when_twd_class_has_no_values(conn):
    _etf(conn, "00777", "未知投信甲", tax_id="1", fund_type=TWD, benchmark="指數")
    child = _etf(
        conn, "00777K", "未知投信甲+U", tax_id="1", fund_type=FOREIGN, benchmark="指數"
    )

    result = sync_investment_trust.sync_investment_trust_data(conn, _cathay_client([]))

    row = conn.execute(
        "SELECT custodian_bank,distribution_policy FROM etf_profiles "
        "WHERE security_id=?",
        (child,),
    ).fetchone()
    assert tuple(row) == (None, None)
    report = sync_history.get_report(conn, result["run_id"])
    unit = next(u for u in report["units"] if u["unit_key"].endswith("00777K"))
    assert unit["status"] == "not_listed"
    assert unit["reason"] == "主級別 00777 尚無資料"


def test_display_marks_inherited_values_and_override_still_wins(conn):
    _etf(conn, "00878", "國泰永續高股息", tax_id="1", fund_type=TWD, benchmark="指數")
    child = _etf(
        conn,
        "00878K",
        "國泰永續高股息+U",
        tax_id="1",
        fund_type=FOREIGN,
        benchmark="指數",
    )
    sync_investment_trust.sync_investment_trust_data(conn, _cathay_client([]))

    item = next(
        i for i in query.browse(conn, mode="etf")["results"] if i["code"] == "00878K"
    )
    assert item["custodian_bank_display"] == {
        "value": "台新國際商業銀行",
        "tier": "share_class",
        "from_code": "00878",
    }
    frequency = item["distribution_frequency_display"]
    assert (frequency["value"], frequency["tier"], frequency["from_code"]) == (
        "quarterly",
        "share_class",
        "00878",
    )
    assert profile_values.TIER_LABELS["share_class"] == "同基金"
    # Coverage counts an inherited value as the fund's official value.
    assert profile_enrichment.coverage(conn)["frequency_official"] == 2

    conn.execute(
        "UPDATE etf_profiles SET custodian_bank_user_override='彰化商業銀行' "
        "WHERE security_id=?",
        (child,),
    )
    profile = dict(
        conn.execute(
            "SELECT * FROM etf_profiles WHERE security_id=?", (child,)
        ).fetchone()
    )
    assert profile_values.custodian_bank(profile)["tier"] == "user_override"


def test_bank_filter_finds_inherited_share_class(conn):
    _etf(conn, "00878", "國泰永續高股息", tax_id="1", fund_type=TWD, benchmark="指數")
    _etf(
        conn,
        "00878K",
        "國泰永續高股息+U",
        tax_id="1",
        fund_type=FOREIGN,
        benchmark="指數",
    )
    sync_investment_trust.sync_investment_trust_data(conn, _cathay_client([]))

    codes = {
        i["code"]
        for i in query.browse(conn, mode="etf", bank="台新國際商業銀行")["results"]
    }
    assert codes == {"00878", "00878K"}
