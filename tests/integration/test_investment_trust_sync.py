"""Integration tests for `sync_investment_trust.sync_investment_trust_data` (design 004
§4.3.1/§4.5.1/§4.8 Phase E) against a mocked multi-host transport — no
real network access (DEVELOPMENT.md §2.4.2).
"""

import json

import httpx2
import pytest

from taiwan_stock.catalog import sync, sync_history, sync_investment_trust
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


def _seed_etf(conn, *, code, name, custodian_institution_raw=None, **profile_overrides):
    security_id = sync.upsert_security(
        conn,
        {
            "market": "TWSE",
            "code": code,
            "name": name,
            "security_type": "etf",
            "industry": None,
            "transfer_agent_name": None,
            "transfer_agent_phone": None,
            "status": "active",
        },
    )
    sync.upsert_etf_profile(
        conn,
        security_id,
        {
            "custodian_institution_raw": custodian_institution_raw,
            "fee_summary": None,
            "benchmark": None,
        },
    )
    if profile_overrides:
        assignments = ", ".join(f"{key} = :{key}" for key in profile_overrides)
        conn.execute(
            f"UPDATE etf_profiles SET {assignments} WHERE security_id = :security_id",
            {**profile_overrides, "security_id": security_id},
        )
    conn.commit()
    return security_id


CAPITAL_OVERVIEW_HTML = '<a href="/etf/product/detail/195/basic">00919</a>'
CAPITAL_DETAIL_HTML = (
    '<div class="th cell">保管銀行</div>'
    '<div class="td cell small-6 medium-auto">中國信託商業銀行  </div>'
    '<h3 class="main-info-item-value profit-frequency">季配</h3>'
    '<div class="th cell">收益分配評價月份</div>'
    '<div class="td cell small-6 medium-auto">2、5、8、11月  </div>'
)
CAPITAL_SCHEDULE_HTML = (
    '<div class="schedule-main-section-card">'
    '<div class="schedule-main-section-card-header-detail-right">季配</div>'
    '<div class="schedule-main-section-card-header-months-item">3 月</div>'
    '<div class="schedule-main-section-card-header-months-item">6 月</div>'
    '<div class="schedule-main-section-card-header-months-item">9 月</div>'
    '<div class="schedule-main-section-card-header-months-item">12 月</div>'
    '<a href="/etf/product/detail/195/basic">00919 群益台灣精選高息</a>'
    "</div>"
)
CATHAY_LIST_JSON = {
    "totalCount": 3,
    "result": [
        {"fundCode": "AJ", "stockCode": "00725B"},
        {"fundCode": "EA", "stockCode": "00400A"},
        {"fundCode": "CN", "stockCode": "00878"},
    ],
    "returnCode": "2000",
}
CATHAY_INTRODUCE_JSON = {
    "AJ": {"allotType": "季配", "bank": "玉山商業銀行"},
    "CN": {"allotType": "年配", "bank": "國泰世華銀行"},
    "EA": {"allotType": "併入基金資產，不予分配", "bank": "中國信託商業銀行"},
}


def test_ctbc_official_frequency_and_non_distributing_policy(conn):
    quarterly = _seed_etf(conn, code="00995A", name="中信台灣ETF")
    accumulating = _seed_etf(conn, code="009828", name="中信累積ETF")

    def handler(request):
        path = request.url.path
        if path.endswith("/home/AuthToken"):
            data = {"token": "fresh-token"}
        elif path.endswith("/etf/ETFList"):
            data = {
                "Data": [
                    {"ETF_ID": "00995A", "CNO": "product-a"},
                    {"ETF_ID": "009828", "CNO": "product-b"},
                ]
            }
        else:
            assert path.endswith("/etf/ETFDetail")
            cno = request.content.decode("utf-8")
            code, raw = (
                ("00995A", "<p>季配息</p>")
                if "product-a" in cno
                else ("009828", "<p>無</p>")
            )
            data = {
                "FundDetail": [
                    {
                        "ETF_ID": code,
                        "CNO": "product-a" if code == "00995A" else "product-b",
                        "PreserveBank": "元大銀行",
                        "DistriRate": raw,
                    }
                ]
            }
        return httpx2.Response(200, json={"ResultCode": 0, "Data": data})

    result = sync_investment_trust.sync_investment_trust_data(
        conn, httpx2.Client(transport=httpx2.MockTransport(handler)), issuer="中信"
    )
    assert result["failed_units"] == []
    q = conn.execute(
        "SELECT distribution_frequency,distribution_policy,custodian_bank "
        "FROM etf_profiles WHERE security_id=?",
        (quarterly,),
    ).fetchone()
    assert tuple(q) == ("quarterly", "distributing", "元大商業銀行")
    a = conn.execute(
        "SELECT distribution_frequency,distribution_policy "
        "FROM etf_profiles WHERE security_id=?",
        (accumulating,),
    ).fetchone()
    assert tuple(a) == (None, "non_distributing")


def test_sitca_policy_fills_gaps_without_overwriting_direct_issuer(conn, monkeypatch):
    from taiwan_stock.catalog import official_profile_adapters as adapters
    from taiwan_stock.catalog import profile_enrichment, profile_values

    twice = _seed_etf(conn, code="00922", name="國泰台灣領袖50")
    accumulation = _seed_etf(conn, code="00636", name="國泰中國A50")
    direct = _seed_etf(
        conn,
        code="0051",
        name="元大中型100",
        distribution_frequency="quarterly",
        distribution_frequency_source=json.dumps(
            {"origin": "investment_trust", "raw": "季配"}
        ),
    )
    source = {
        "issuer_name": "國泰投信",
        "source_url": adapters.SITCA_LIST,
        "data_period": "202609",
    }
    monkeypatch.setattr(
        adapters,
        "fetch_sitca_profiles",
        lambda client: {
            "00922": {
                **source,
                "distribution_policy": "distributing",
                "frequency": "semiannual",
                "distribution_frequency_raw": "一年兩次配息",
            },
            "00636": {
                **source,
                "distribution_policy": "non_distributing",
                "distribution_frequency_raw": "不分配",
            },
            "0051": {
                **source,
                "distribution_policy": "distributing",
                "frequency": "annual",
                "distribution_frequency_raw": "年配",
            },
        },
    )
    result = profile_enrichment.sync_basic_info(conn, None, sleep=lambda _: None)
    assert result["validation_errors"] == ["0051:frequency"]
    display = profile_values.distribution_frequency(
        dict(
            conn.execute(
                "SELECT * FROM etf_profiles WHERE security_id=?", (twice,)
            ).fetchone()
        )
    )
    assert (display["value"], display["tier"], display["label"]) == (
        "semiannual",
        "official",
        "半年配",
    )
    assert (
        profile_values.distribution_frequency(
            dict(
                conn.execute(
                    "SELECT * FROM etf_profiles WHERE security_id=?", (accumulation,)
                ).fetchone()
            )
        )["value"]
        == "non_distributing"
    )
    assert (
        conn.execute(
            "SELECT distribution_frequency FROM etf_profiles WHERE security_id=?",
            (direct,),
        ).fetchone()[0]
        == "quarterly"
    )


YUANTA_DETAIL_HTML = '<div>保管銀行</div><div class="fundData">中國信託商業銀行</div>'
EZMONEY_BANK_INDEX_HTML = '<a href="/ETF/Fund/Info?fundCode=49YTW">00981A</a>'
EZMONEY_DETAIL_HTML = (
    "<div>保管銀行\\n\\t\\t\\t</th>\\n\\t\\t\\t<td>"
    "\\n\\t\\t\\t\\t華南銀行\\n\\t\\t\\t</td></div>"
)


def _mock_client() -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        host, path = request.url.host, request.url.path
        query = str(request.url.params)

        if host == "www.capitalfund.com.tw" and path.endswith("/overview"):
            return httpx2.Response(200, text=CAPITAL_OVERVIEW_HTML)
        if host == "www.capitalfund.com.tw" and path.endswith("/cross/schedule"):
            return httpx2.Response(200, text=CAPITAL_SCHEDULE_HTML)
        if host == "www.capitalfund.com.tw" and "/detail/195/" in path:
            return httpx2.Response(200, text=CAPITAL_DETAIL_HTML)
        if host == "cwapi.cathaysite.com.tw" and path.endswith("/GetETFList"):
            return httpx2.Response(200, json=CATHAY_LIST_JSON)
        if host == "cwapi.cathaysite.com.tw" and path.endswith("/GetETFIntroduce"):
            result = CATHAY_INTRODUCE_JSON.get(request.url.params["fundCode"])
            return httpx2.Response(
                200,
                json={"result": result, "returnCode": "2000" if result else "4005"},
            )
        if host == "www.yuantaetfs.com":
            return httpx2.Response(200, text=YUANTA_DETAIL_HTML)
        if host == "www.ezmoney.com.tw" and path.endswith(
            "ProfitDistributionBankIndex"
        ):
            return httpx2.Response(200, text=EZMONEY_BANK_INDEX_HTML)
        if host == "www.ezmoney.com.tw" and path.endswith("/ETF/Fund/Info"):
            if "fundCode=49YTW" in query:
                return httpx2.Response(200, text=EZMONEY_DETAIL_HTML)
            return httpx2.Response(200, text="<div>no data</div>")
        if host == "www.megafunds.com.tw":
            return httpx2.Response(503, text="service unavailable")
        if host == "sitc.sinopac.com":
            return httpx2.Response(200, text="")
        return httpx2.Response(404, text="unexpected request")

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_sync_fills_custodian_bank_frequency_and_month_pattern(conn):
    _seed_etf(conn, code="00919", name="群益台灣精選高息")

    result = sync_investment_trust.sync_investment_trust_data(conn, _mock_client())

    assert result["synced"] == 1
    assert result["failed_units"] == []
    row = conn.execute(
        "SELECT custodian_bank, distribution_frequency, distribution_month_pattern, "
        "distribution_ex_dividend_schedule, "
        "custodian_bank_source, distribution_frequency_source "
        "FROM etf_profiles p JOIN securities s ON s.id = p.security_id "
        "WHERE s.code = '00919'"
    ).fetchone()
    assert row["custodian_bank"] == "中國信託商業銀行"
    assert row["distribution_frequency"] == "quarterly"
    assert json.loads(row["distribution_month_pattern"]) == [2, 5, 8, 11]
    assert json.loads(row["distribution_ex_dividend_schedule"])["months"] == [
        3,
        6,
        9,
        12,
    ]
    assert json.loads(row["custodian_bank_source"])["issuer"] == "群益"
    assert json.loads(row["distribution_frequency_source"])["raw"] == "季配"


def test_sync_cathay_bank_and_policy_from_fund_api(conn):
    _seed_etf(conn, code="00725B", name="國泰投資級公司債")
    _seed_etf(conn, code="00400A", name="主動國泰動能高息")
    _seed_etf(conn, code="00699B", name="國泰已不在名單")

    result = sync_investment_trust.sync_investment_trust_data(conn, _mock_client())

    rows = {
        row["code"]: row
        for row in conn.execute(
            "SELECT s.code, custodian_bank, distribution_frequency, "
            "distribution_policy, custodian_bank_source "
            "FROM etf_profiles p JOIN securities s ON s.id = p.security_id"
        )
    }
    assert rows["00725B"]["custodian_bank"] == "玉山銀行"
    assert rows["00725B"]["distribution_frequency"] == "quarterly"
    assert json.loads(rows["00725B"]["custodian_bank_source"])["url"].endswith(
        "GetETFIntroduce?fundCode=AJ"
    )
    assert rows["00400A"]["custodian_bank"] == "中國信託商業銀行"
    assert rows["00400A"]["distribution_frequency"] is None
    assert rows["00400A"]["distribution_policy"] == "non_distributing"
    assert rows["00699B"]["custodian_bank"] is None
    # Absent from the issuer's own list: settled as not listed, not a failure.
    assert result["failed_units"] == []
    report = sync_history.get_report(conn, result["run_id"])
    assert report["status"] == "success"
    missing = next(u for u in report["units"] if u["unit_key"] == "國泰:00699B")
    assert missing["status"] == "not_listed"


def test_official_schedule_uses_ex_dividend_months_for_00927(conn):
    from taiwan_stock.catalog import profile_values

    sid = _seed_etf(conn, code="00927", name="群益半導體收益")
    schedule_html = (
        CAPITAL_SCHEDULE_HTML.replace("3 月</div>", "1 月</div>")
        .replace("6 月</div>", "4 月</div>")
        .replace("9 月</div>", "7 月</div>")
        .replace("12 月</div>", "10 月</div>")
        .replace("/195/basic", "/366/basic")
        .replace("00919", "00927")
    )
    detail_html = CAPITAL_DETAIL_HTML.replace("2、5、8、11月", "3、6、9、12月")

    def handler(request):
        path = request.url.path
        if path.endswith("/overview"):
            return httpx2.Response(
                200, text='<a href="/etf/product/detail/366/basic">00927</a>'
            )
        return httpx2.Response(
            200, text=schedule_html if path.endswith("/cross/schedule") else detail_html
        )

    result = sync_investment_trust.sync_investment_trust_data(
        conn, httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    assert result["failed_units"] == []
    row = dict(
        conn.execute(
            "SELECT * FROM etf_profiles WHERE security_id=?", (sid,)
        ).fetchone()
    )
    assert json.loads(row["distribution_month_pattern"]) == [3, 6, 9, 12]
    display = profile_values.distribution_frequency(row)
    assert display["month_pattern"] == [1, 4, 7, 10]
    assert display["month_tier"] == "official"


def test_schedule_failure_preserves_prior_data_and_retry_only_fetches_schedule(conn):
    sid = _seed_etf(conn, code="00919", name="群益台灣精選高息")
    sync_investment_trust.sync_investment_trust_data(conn, _mock_client())
    original = conn.execute(
        "SELECT distribution_ex_dividend_schedule FROM etf_profiles "
        "WHERE security_id=?",
        (sid,),
    ).fetchone()[0]
    requested = []

    def failure(request):
        requested.append(request.url.path)
        if request.url.path.endswith("/cross/schedule"):
            return httpx2.Response(503, text="unavailable")
        return httpx2.Response(
            200,
            text=CAPITAL_OVERVIEW_HTML
            if request.url.path.endswith("/overview")
            else CAPITAL_DETAIL_HTML,
        )

    failed = sync_investment_trust.sync_investment_trust_data(
        conn, httpx2.Client(transport=httpx2.MockTransport(failure))
    )
    assert "schedule:群益" in failed["failed_units"]
    assert (
        conn.execute(
            "SELECT distribution_ex_dividend_schedule FROM etf_profiles "
            "WHERE security_id=?",
            (sid,),
        ).fetchone()[0]
        == original
    )

    def retry_handler(request):
        requested.append(request.url.path)
        assert request.url.path.endswith("/cross/schedule")
        return httpx2.Response(200, text=CAPITAL_SCHEDULE_HTML)

    requested.clear()
    retried = sync_investment_trust.sync_investment_trust_data(
        conn,
        httpx2.Client(transport=httpx2.MockTransport(retry_handler)),
        retry_run_id=failed["run_id"],
    )
    assert retried["failed_units"] == []
    keys = {
        u["unit_key"] for u in sync_history.get_report(conn, retried["run_id"])["units"]
    }
    assert keys == {"schedule:群益"}
    assert requested == ["/etf/product/cross/schedule"]


def test_sync_cross_checks_against_twse_raw_institution(conn):
    _seed_etf(
        conn,
        code="00919",
        name="群益台灣精選高息",
        custodian_institution_raw="中國信託商業銀行股份有限公司",
    )
    _seed_etf(
        conn, code="00878", name="國泰永續高股息", custodian_institution_raw="彰化銀行"
    )
    _seed_etf(
        conn,
        code="0050",
        name="元大台灣50",
        custodian_institution_raw="臺灣集中保管結算所股份有限公司",
    )

    sync_investment_trust.sync_investment_trust_data(conn, _mock_client())

    def cross_check(code):
        row = conn.execute(
            "SELECT custodian_bank_source FROM etf_profiles p "
            "JOIN securities s ON s.id = p.security_id WHERE s.code = ?",
            (code,),
        ).fetchone()
        return json.loads(row["custodian_bank_source"])["cross_check"]

    # Adapter's 中國信託商業銀行 matches the alias-normalized TWSE raw value.
    assert cross_check("00919") == "confirmed"
    # Adapter says 國泰世華銀行, TWSE raw says 彰化銀行 — a real disagreement.
    assert cross_check("00878") == "conflict"
    # TWSE raw is a CSDC name, not a bank — can't confirm or conflict.
    assert cross_check("0050") == "single_source"


def test_sync_isolates_one_unit_failure_from_others(conn):
    _seed_etf(conn, code="00919", name="群益台灣精選高息")
    _seed_etf(conn, code="00699", name="群益不存在的基金")  # missing from capital_ids

    result = sync_investment_trust.sync_investment_trust_data(conn, _mock_client())

    assert result["synced"] == 1
    # A code absent from the issuer list is settled as not listed (#135).
    assert result["failed_units"] == []
    untouched = conn.execute(
        "SELECT custodian_bank FROM etf_profiles p "
        "JOIN securities s ON s.id = p.security_id WHERE s.code = '00699'"
    ).fetchone()
    assert untouched["custodian_bank"] is None


def test_sync_skips_etf_with_unrecognized_issuer(conn):
    _seed_etf(conn, code="00999", name="未知投信中國高股息")

    result = sync_investment_trust.sync_investment_trust_data(conn, _mock_client())

    assert result["synced"] == 0
    assert result["skipped_no_issuer"] == 1
    assert result["failed_units"] == []


def test_sync_missing_frequency_field_does_not_clobber_prior_value(conn):
    # 元大 has no structured frequency field on its product page — a
    # rerun must not erase a frequency value some other, still-valid
    # source previously wrote (design 004 §4.6 point 2).
    _seed_etf(
        conn,
        code="0050",
        name="元大台灣50",
        distribution_frequency="quarterly",
        distribution_frequency_source=json.dumps({"issuer": "元大", "raw": "季配"}),
    )

    result = sync_investment_trust.sync_investment_trust_data(conn, _mock_client())

    assert result["synced"] == 1
    row = conn.execute(
        "SELECT custodian_bank, distribution_frequency FROM etf_profiles p "
        "JOIN securities s ON s.id = p.security_id WHERE s.code = '0050'"
    ).fetchone()
    assert row["custodian_bank"] == "中國信託商業銀行"
    assert row["distribution_frequency"] == "quarterly"


def test_sync_never_touches_user_override_columns(conn):
    _seed_etf(
        conn,
        code="00919",
        name="群益台灣精選高息",
        custodian_bank_user_override="兆豐國際商業銀行",
        distribution_frequency_user_override="monthly",
    )

    sync_investment_trust.sync_investment_trust_data(conn, _mock_client())

    row = conn.execute(
        "SELECT custodian_bank_user_override, distribution_frequency_user_override "
        "FROM etf_profiles p JOIN securities s ON s.id = p.security_id "
        "WHERE s.code = '00919'"
    ).fetchone()
    assert row["custodian_bank_user_override"] == "兆豐國際商業銀行"
    assert row["distribution_frequency_user_override"] == "monthly"


def test_sync_uses_ezmoney_cookie_warmup_flow(conn):
    _seed_etf(conn, code="00981A", name="統一台灣高息動能")

    result = sync_investment_trust.sync_investment_trust_data(conn, _mock_client())

    assert result["synced"] == 1
    row = conn.execute(
        "SELECT custodian_bank FROM etf_profiles p "
        "JOIN securities s ON s.id = p.security_id WHERE s.code = '00981A'"
    ).fetchone()
    assert row["custodian_bank"] == "華南銀行"


def test_sync_records_list_lookup_failure_without_crashing(conn):
    # Mega's list endpoint returns 503 in `_mock_client` above.
    _seed_etf(conn, code="00921", name="兆豐台灣產業龍頭")

    result = sync_investment_trust.sync_investment_trust_data(conn, _mock_client())

    assert "兆豐" in result["list_lookup_failures"]
    assert result["failed_units"] == ["兆豐:00921"]


def test_sync_issuer_filter_only_touches_that_issuer(conn):
    _seed_etf(conn, code="00919", name="群益台灣精選高息")
    _seed_etf(conn, code="0050", name="元大台灣50")

    result = sync_investment_trust.sync_investment_trust_data(
        conn, _mock_client(), issuer="群益"
    )

    assert result["synced"] == 1
    report = sync_history.get_report(conn, result["run_id"])
    keys = {u["unit_key"] for u in report["units"]}
    # 元大's ETF was never planned at all — a single-issuer run must not
    # even record it as skipped/unsupported, since it wasn't in scope.
    # ("list:群益" is 群益's own product-id list lookup, still needed.)
    assert keys == {"群益:00919", "list:群益", "schedule:群益"}
    assert (
        conn.execute(
            "SELECT custodian_bank FROM etf_profiles p "
            "JOIN securities s ON s.id = p.security_id WHERE s.code = '0050'"
        ).fetchone()["custodian_bank"]
        is None
    )


def test_sync_circuit_breaker_stops_one_blocked_issuer_without_affecting_others(conn):
    # 國泰's site rejects every request here (mirrors the real HTTP 403
    # observed from Akamai in production); once that's happened
    # `sync._CIRCUIT_BREAKER_THRESHOLD` times in a row, the rest of
    # 國泰's units are left `pending` for a later retry instead of being
    # ground through for no benefit — but 元大 is a different host and
    # must sync normally regardless.
    cathay_codes = [f"00{800 + i}" for i in range(sync._CIRCUIT_BREAKER_THRESHOLD + 1)]
    for code in cathay_codes[:-1]:
        _seed_etf(conn, code=code, name=f"國泰{code}")
    _seed_etf(
        conn,
        code=cathay_codes[-1],
        name=f"國泰{cathay_codes[-1]}",
        custodian_institution_raw="彰化商業銀行股份有限公司",
    )
    _seed_etf(conn, code="0050", name="元大台灣50")

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "cwapi.cathaysite.com.tw":
            return httpx2.Response(403, text="blocked")
        if request.url.host == "www.yuantaetfs.com":
            return httpx2.Response(200, text=YUANTA_DETAIL_HTML)
        return httpx2.Response(404, text="unexpected request")

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    result = sync_investment_trust.sync_investment_trust_data(conn, client)

    assert result["synced"] == 1  # 元大 only
    assert len(result["failed_units"]) == sync._CIRCUIT_BREAKER_THRESHOLD
    report = sync_history.get_report(conn, result["run_id"])
    pending = [u for u in report["units"] if u["status"] == "pending"]
    assert len(pending) == 1  # the one 國泰 unit past the breaker threshold
    # The skipped pending unit must not have mutated etf_profiles before skipping.
    assert (
        conn.execute(
            "SELECT custodian_bank FROM etf_profiles p "
            "JOIN securities s ON s.id = p.security_id WHERE s.code = ?",
            (cathay_codes[-1],),
        ).fetchone()["custodian_bank"]
        is None
    )
    assert (
        conn.execute(
            "SELECT custodian_bank FROM etf_profiles p "
            "JOIN securities s ON s.id = p.security_id WHERE s.code = '0050'"
        ).fetchone()["custodian_bank"]
        == "中國信託商業銀行"
    )


def test_a_maintenance_page_marks_the_whole_issuer_after_one_request(conn):
    """Design 055 (replacing 048's five failures + pending): 元大 redirected
    every product page to /maintenance (2026-10-31). One answer is enough to
    know the host is down, so no more requests, and every 元大 unit reads
    來源維護中 — not failed, old values kept, still retried."""
    codes = [f"00{800 + i}" for i in range(sync._CIRCUIT_BREAKER_THRESHOLD + 3)]
    for code in codes:
        _seed_etf(conn, code=code, name=f"元大{code}", custodian_bank="元大銀行")
    _seed_etf(conn, code="0050", name="元大台灣50")
    conn.execute(
        "UPDATE etf_profiles SET custodian_institution_raw='中國信託商業銀行' "
        "WHERE security_id=(SELECT id FROM securities WHERE code='0050')"
    )
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/maintenance":
            return httpx2.Response(200, text="<p>系統維護中</p>")
        if request.url.host == "www.yuantaetfs.com":
            requests.append(request.url.path)
            return httpx2.Response(302, headers={"location": "/maintenance"})
        return httpx2.Response(404, text="unexpected request")

    result = sync_investment_trust.sync_investment_trust_data(
        conn, httpx2.Client(transport=httpx2.MockTransport(handler))
    )

    assert len(requests) == 1
    assert result["failed_units"] == []
    report = sync_history.get_report(conn, result["run_id"])
    statuses = [
        u["status"] for u in report["units"] if u["unit_key"].startswith("元大:")
    ]
    assert statuses == ["source_maintenance"] * (len(codes) + 1)
    assert {
        u["reason"] for u in report["units"] if u["unit_key"].startswith("元大:")
    } == {"來源網站維護中"}
    assert report["status"] == "failed"  # nothing updated: retried later
    # Old values stay; the exchange's own bank is still written.
    banks = dict(
        conn.execute(
            "SELECT s.code, p.custodian_bank FROM etf_profiles p "
            "JOIN securities s ON s.id = p.security_id"
        ).fetchall()
    )
    assert {banks[code] for code in codes} == {"元大銀行"}
    assert banks["0050"] == "中國信託商業銀行"


def test_sync_circuit_breaker_ignores_404_not_found(conn):
    # Live observation: after fixing 國泰's WAF 403 (missing User-Agent),
    # several `guess_issuer`-misattributed codes turned out to be
    # genuine HTTP 404s (no page for that product at 國泰's site) — that
    # must not trip the breaker, or a run would wrongly abandon every
    # other, perfectly reachable 國泰 product (like the real 00878) the
    # moment a handful of bad codes happened to sort first.
    codes = [f"00{800 + i}" for i in range(sync._CIRCUIT_BREAKER_THRESHOLD + 2)]
    for code in codes:
        _seed_etf(conn, code=code, name=f"國泰{code}")

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "cwapi.cathaysite.com.tw":
            return httpx2.Response(404, text="not found")
        return httpx2.Response(404, text="unexpected request")

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    result = sync_investment_trust.sync_investment_trust_data(conn, client)

    assert len(result["failed_units"]) == len(codes)
    report = sync_history.get_report(conn, result["run_id"])
    assert all(u["status"] != "pending" for u in report["units"])


def _never_called_client() -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"unexpected network call to {request.url}")

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_known_bank_still_fetches_official_policy(conn):
    sid = _seed_etf(
        conn,
        code="00631L",
        name="元大台灣50正2",
        custodian_institution_raw="中國信託商業銀行股份有限公司",
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, text="<div>收益分配</div><div>否</div>")

    result = sync_investment_trust.sync_investment_trust_data(
        conn, httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    assert len(requests) == 1
    assert result["synced"] == 1
    row = conn.execute(
        "SELECT * FROM etf_profiles WHERE security_id=?", (sid,)
    ).fetchone()
    assert row["custodian_bank"] == "中國信託商業銀行"
    assert row["distribution_policy"] == "non_distributing"
    assert json.loads(row["custodian_bank_source"])["origin"] == "exchange"


def test_sync_uses_exchange_bank_even_without_a_guessable_issuer(conn):
    # Previously an unguessable issuer meant permanently `unsupported` —
    # if TWSE/TPEx's own field already resolves to a bank, that's a win
    # this ETF wouldn't otherwise ever get.
    _seed_etf(
        conn,
        code="00999",
        name="未知投信中國高股息",  # no ISSUER_NAME_PREFIXES match
        custodian_institution_raw="彰化商業銀行股份有限公司",
    )

    result = sync_investment_trust.sync_investment_trust_data(
        conn, _never_called_client()
    )

    assert result["synced"] == 1
    assert result["skipped_no_issuer"] == 0
    row = conn.execute(
        "SELECT custodian_bank FROM etf_profiles p "
        "JOIN securities s ON s.id = p.security_id WHERE s.code = '00999'"
    ).fetchone()
    assert row["custodian_bank"] == "彰化銀行"


def test_sync_still_fetches_frequency_issuer_but_exchange_bank_wins_on_conflict(conn):
    # 群益 is a FREQUENCY_ISSUERS member, so it's still worth a fetch even
    # when TWSE/TPEx already resolves a bank — but per design 005 §4.4's
    # live finding (an issuer's own low-traffic page can be stale),
    # the exchange value wins over whatever 群益's page says, and the
    # disagreement is recorded rather than silently dropped.
    _seed_etf(
        conn,
        code="00919",
        name="群益台灣精選高息",
        custodian_institution_raw="彰化商業銀行股份有限公司",  # disagrees with 中國信託
    )

    result = sync_investment_trust.sync_investment_trust_data(conn, _mock_client())

    assert result["synced"] == 1
    row = conn.execute(
        "SELECT custodian_bank, custodian_bank_source, distribution_frequency "
        "FROM etf_profiles p JOIN securities s ON s.id = p.security_id "
        "WHERE s.code = '00919'"
    ).fetchone()
    assert row["custodian_bank"] == "彰化銀行"  # exchange wins, not 群益's 中國信託
    source = json.loads(row["custodian_bank_source"])
    assert source["origin"] == "exchange"
    assert source["cross_check"] == "conflict"
    assert row["distribution_frequency"] == "quarterly"  # still comes from 群益


def test_frequency_survives_missing_bank_and_metadata_preserves_user_override(conn):
    from taiwan_stock.catalog import profile_values

    sid = _seed_etf(
        conn,
        code="00900",
        name="富邦特選高股息30",
        distribution_frequency_user_override="annual",
    )
    body = "<h5>00900 / 富邦商品</h5><h6>收益分配</h6><div><p>月配息</p></div>"
    result = sync_investment_trust.sync_investment_trust_data(
        conn,
        httpx2.Client(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(200, text=body)
            )
        ),
    )
    row = dict(
        conn.execute(
            "SELECT * FROM etf_profiles WHERE security_id=?", (sid,)
        ).fetchone()
    )
    assert row["custodian_bank"] is None
    assert row["distribution_frequency"] == "monthly"
    assert profile_values.distribution_frequency(row)["tier"] == "user_override"
    report = sync_history.get_report(conn, result["run_id"])
    outcomes = json.loads(report["units"][0]["field_results"])
    assert outcomes["bank"]["status"] == "not_provided"
    assert outcomes["frequency"]["status"] == "success"


def test_official_non_distribution_supersedes_inference_and_filters_consistently(conn):
    from taiwan_stock.catalog import profile_values, query

    sid = _seed_etf(
        conn,
        code="00631L",
        name="元大台灣50正2",
        distribution_frequency="annual",
        distribution_month_pattern="[12]",
        distribution_ex_dividend_schedule=json.dumps(
            {"frequency": "annual", "months": [12]}
        ),
        distribution_frequency_inferred=json.dumps({"value": "monthly"}),
    )
    sync_investment_trust.upsert_etf_bank_and_frequency(
        conn,
        sid,
        {
            "distribution_policy": "non_distributing",
            "distribution_policy_source": json.dumps(
                {"url": "https://www.yuantaetfs.com/"}
            ),
        },
    )
    row = dict(
        conn.execute(
            "SELECT * FROM etf_profiles WHERE security_id=?", (sid,)
        ).fetchone()
    )
    assert row["distribution_frequency"] is None
    assert row["distribution_month_pattern"] is None
    assert row["distribution_ex_dividend_schedule"] is None
    assert profile_values.distribution_frequency(row)["value"] == "non_distributing"
    results = query.browse(conn, mode="etf", frequency="non_distributing")
    assert [r["id"] for r in results["results"]] == [sid]
    assert query.browse(conn, mode="etf", frequency="unknown")["results"] == []
    assert results["coverage"]["non_distributing"] == 1
    assert results["coverage"]["official_frequency"] == 0
    assert results["result_coverage"] == {
        "total": 1,
        "bank_known": 0,
        "official": 0,
        "non_distributing": 1,
        "inferred": 0,
        "user_override": 0,
        "unknown": 0,
    }


def test_official_frequency_change_clears_old_ex_dividend_schedule(conn):
    sid = _seed_etf(
        conn,
        code="00900",
        name="富邦特選高股息30",
        distribution_frequency="quarterly",
        distribution_ex_dividend_schedule=json.dumps(
            {"frequency": "quarterly", "months": [1, 4, 7, 10]}
        ),
    )
    sync_investment_trust.upsert_etf_bank_and_frequency(
        conn, sid, {"distribution_frequency": "semiannual"}
    )
    row = conn.execute(
        "SELECT distribution_ex_dividend_schedule FROM etf_profiles "
        "WHERE security_id=?",
        (sid,),
    ).fetchone()
    assert row[0] is None


def test_basic_sources_merge_per_field_by_recency_without_erasing_richer_values(conn):
    from taiwan_stock.catalog import profile_enrichment

    sid = _seed_etf(conn, code="00935", name="野村臺灣新科技50")
    profile_enrichment.merge_basic(
        conn,
        sid,
        {
            "issuer_name": "野村投信",
            "benchmark": "目前指數",
            "data_period": "202610",
            "source_url": "https://info.tpex.org.tw/api/etfProduct",
        },
    )
    profile_enrichment.merge_basic(
        conn,
        sid,
        {
            "issuer_name": "舊公司",
            "benchmark": "舊指數",
            "data_period": "202608",
            "source_url": "https://www.sitca.org.tw/report",
        },
    )
    sync.upsert_etf_profile(
        conn,
        sid,
        {"custodian_institution_raw": None, "benchmark": None, "fee_summary": None},
    )
    row = dict(
        conn.execute(
            "SELECT * FROM etf_profiles WHERE security_id=?", (sid,)
        ).fetchone()
    )
    assert row["issuer_name"] == "野村投信"
    assert row["benchmark"] == "目前指數"
    assert json.loads(row["basic_info_source"])["benchmark"]["data_period"] == "202610"


def test_official_issuer_routes_active_name_to_verified_adapter(conn):
    sid = _seed_etf(conn, code="00944", name="主動其他名稱", issuer_name="野村投信")
    result = sync_investment_trust.sync_investment_trust_data(
        conn,
        httpx2.Client(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200,
                    json={
                        "Entries": {
                            "FundDetail": {
                                "CStockNo": "00944",
                                "CCgDividendTypeName": "月配",
                                "CCustodyBankName": "台北富邦商業銀行",
                            }
                        }
                    },
                )
            )
        ),
    )
    assert result["synced"] == 1
    row = conn.execute(
        "SELECT distribution_frequency FROM etf_profiles WHERE security_id=?", (sid,)
    ).fetchone()
    assert row[0] == "monthly"


def test_basic_failure_does_not_stop_exchange_and_retry_skips_completed_units(
    conn, monkeypatch
):
    from taiwan_stock.catalog import official_profile_adapters as adapters
    from taiwan_stock.catalog import operations, profile_enrichment

    sid = _seed_etf(conn, code="00719B", name="元大美債1-3")
    conn.execute("UPDATE securities SET market='TPEx' WHERE id=?", (sid,))
    attempts = []

    def association(client):
        attempts.append("sitca")
        if attempts.count("sitca") == 1:
            raise ValueError("報表暫不可讀")
        return {
            "00719B": {"issuer_name": "元大投信", "source_url": adapters.SITCA_LIST}
        }

    def exchange(client, code):
        attempts.append(code)
        return {
            "issuer_name": "元大投信",
            "benchmark": "官方債券指數",
            "source_url": "https://info.tpex.org.tw/api/etfProduct",
        }

    monkeypatch.setattr(adapters, "fetch_sitca_profiles", association)
    monkeypatch.setattr(adapters, "fetch_tpex_profile", exchange)
    first = profile_enrichment.sync_basic_info(conn, None)
    assert first["failed_units"] == ["sitca"]
    second = operations.retry_source(conn, None, first["run_id"])
    assert second["failed_units"] == []
    assert attempts == ["sitca", "00719B", "sitca"]
    assert (
        conn.execute(
            "SELECT benchmark FROM etf_profiles WHERE security_id=?", (sid,)
        ).fetchone()[0]
        == "官方債券指數"
    )


def test_policy_conflict_retains_previous_frequency_but_saves_new_bank(conn):
    from taiwan_stock.catalog import query

    sid = _seed_etf(
        conn, code="00900", name="富邦特選高股息30", distribution_frequency="annual"
    )
    html = (
        "<h5>00900 / 富邦商品</h5><p>保管銀行</p><p>永豐銀行</p>"
        "<h6>收益分配</h6><div>月配息</div><h6>收益分配</h6><div>季配息</div>"
    )
    result = sync_investment_trust.sync_investment_trust_data(
        conn,
        httpx2.Client(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(200, text=html)
            )
        ),
    )
    row = conn.execute(
        "SELECT * FROM etf_profiles WHERE security_id=?", (sid,)
    ).fetchone()
    assert row["distribution_frequency"] == "annual"
    assert row["custodian_bank"] == "永豐銀行"
    assert result["failed_units"] == ["富邦:00900"]
    fields = json.loads(
        sync_history.get_report(conn, result["run_id"])["units"][0]["field_results"]
    )
    assert fields["bank"]["status"] == "success"
    assert fields["frequency"]["status"] == "parse_failed"
    assert query.browse(conn, mode="etf")["results"][0]["bank_sync_failed"] is False


def test_taishin_current_profile_policy_precedes_historical_frequency_label(conn):
    sid = _seed_etf(conn, code="009806", name="台新標普500")
    history = (
        '<div class="fund_card"><div class="SeriesName" id="009806">商品</div>'
        "<table><tr><td>2025 / 01</td><td>季配</td></tr></table></div>"
    )
    profile = (
        "<table><tr><th>保管銀行</th><td>華南商業銀行</td></tr>"
        "<tr><th>收益分配</th><td>年配</td></tr></table>"
    )

    def handler(request):
        return httpx2.Response(
            200, text=history if request.url.path.endswith("ETFDIVList") else profile
        )

    result = sync_investment_trust.sync_investment_trust_data(
        conn, httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    assert not result["failed_units"]
    assert (
        conn.execute(
            "SELECT distribution_frequency FROM etf_profiles WHERE security_id=?",
            (sid,),
        ).fetchone()[0]
        == "annual"
    )


@pytest.mark.parametrize(
    ("profile_response", "unit_status"),
    [
        (httpx2.Response(503, text="busy"), "http_failed"),
        (httpx2.Response(200, text="<p>改版</p>"), "parse_failed"),
    ],
)
def test_partial_field_failure_keeps_its_own_kind(conn, profile_response, unit_status):
    # Design 048 §4.2.8: a field that only failed to connect leaves the unit a
    # transient failure (retryable), not 來源待更新.
    _seed_etf(conn, code="009806", name="台新標普500")
    history = (
        '<div class="fund_card"><div class="SeriesName" id="009806">商品</div>'
        "<table><tr><td>2025 / 01</td><td>季配</td></tr></table></div>"
    )

    def handler(request):
        if request.url.path.endswith("ETFDIVList"):
            return httpx2.Response(200, text=history)
        return profile_response

    result = sync_investment_trust.sync_investment_trust_data(
        conn, httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    unit = next(
        u
        for u in sync_history.get_report(conn, result["run_id"])["units"]
        if u["unit_key"] == "台新:009806"
    )
    assert unit["status"] == unit_status
    assert unit["reason"] == "部分欄位取得失敗"
    assert "台新:009806" in result["failed_units"]


def test_custom_non_distribution_is_counted_separately_from_official_policy(conn):
    from taiwan_stock.catalog import profile_enrichment, query

    _seed_etf(
        conn,
        code="00900",
        name="富邦特選高股息30",
        distribution_frequency_user_override="non_distributing",
    )
    counts = profile_enrichment.coverage(conn)
    assert counts["frequency_user_override"] == 1
    assert counts["non_distributing"] == 0
    counts = query.browse(conn, mode="etf")["result_coverage"]
    assert counts["total"] == counts["user_override"] == 1
    assert counts["non_distributing"] == 0


def test_current_product_period_uses_taipei_business_month(conn, monkeypatch):
    from taiwan_stock.catalog import profile_enrichment

    sid = _seed_etf(conn, code="00900", name="富邦特選高股息30")
    monkeypatch.setattr(profile_enrichment, "today_taipei", lambda: "2026-10-01")
    monkeypatch.setattr(
        profile_enrichment, "now_utc_iso", lambda: "2026-09-30T16:30:00.000Z"
    )
    profile_enrichment.merge_basic(
        conn,
        sid,
        {"issuer_name": "富邦投信", "source_url": "https://example.org/official"},
    )
    source = conn.execute(
        "SELECT basic_info_source FROM etf_profiles WHERE security_id=?", (sid,)
    ).fetchone()[0]
    assert json.loads(source)["issuer_name"]["data_period"] == "202610"


def test_pending_profile_update_does_not_label_previous_bank_as_failed(conn):
    from taiwan_stock.catalog import query

    sid = _seed_etf(
        conn, code="00900", name="富邦特選高股息30", custodian_bank="永豐銀行"
    )
    with sync_history.run(conn, "investment_trust") as task:
        task.plan("富邦:00900", security_id=sid, issuer="富邦")
        item = query.browse(conn, mode="etf")["results"][0]
        assert item["custodian_bank_display"]["value"] == "永豐銀行"
        assert item["bank_sync_failed"] is False
        assert item["frequency_missing_reason"] == "同步處理中"


def test_product_missing_from_issuer_list_is_not_labelled_a_sync_failure(conn):
    from taiwan_stock.catalog import query

    sid = _seed_etf(conn, code="00407A", name="主動凱基台灣")
    status, _, reason = sync_history.error_info(sync_history.ProductNotListed("00407A"))
    with sync_history.run(conn, "investment_trust") as task:
        task.plan("凱基:00407A", security_id=sid, issuer="凱基")
        task.record(
            "凱基:00407A",
            "parse_failed",
            exc=sync_history.ProductNotListed("00407A"),
            field_results={
                field: {"status": status, "reason": reason}
                for field in ("bank", "frequency", "policy")
            },
        )
    item = query.browse(conn, mode="etf")["results"][0]
    assert item["bank_missing_reason"] == "來源未收錄此商品"
    assert item["frequency_missing_reason"] == "來源未收錄此商品"
    assert item["sync_failed"] is False
    assert item["bank_sync_failed"] is False


def test_monthly_report_cannot_overwrite_undated_existing_exchange_value(conn):
    from taiwan_stock.catalog import profile_enrichment

    sid = _seed_etf(
        conn, code="00900", name="富邦特選高股息30", benchmark="既有交易所指數"
    )
    profile_enrichment.merge_basic(
        conn,
        sid,
        {
            "issuer_name": "富邦投信",
            "benchmark": "較舊月報指數",
            "source_url": "https://www.sitca.org.tw/report",
            "data_period": "202608",
        },
    )
    row = conn.execute(
        "SELECT * FROM etf_profiles WHERE security_id=?", (sid,)
    ).fetchone()
    assert row["benchmark"] == "既有交易所指數"
    assert row["issuer_name"] == "富邦投信"
    assert "benchmark" not in json.loads(row["basic_info_source"])
    profile_enrichment.merge_basic(
        conn,
        sid,
        {
            "benchmark": "當前官方指數",
            "source_url": "https://www.nomurafunds.com.tw/product",
        },
    )
    assert (
        conn.execute(
            "SELECT benchmark FROM etf_profiles WHERE security_id=?", (sid,)
        ).fetchone()[0]
        == "當前官方指數"
    )
