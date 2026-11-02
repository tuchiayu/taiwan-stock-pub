"""Integration tests for catalog sync against a mocked transport.

No real network access — DEVELOPMENT.md §2.4.2 test isolation and
design 002 §5.1 both expect CI to run on canned/replayable data, not
live calls.
"""

import httpx2
import pytest

from taiwan_stock.catalog import sync, sync_etf_distributions, twse_adapter
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection

STOCK_ROWS = [
    {
        "公司代號": "2330",
        "公司名稱": "台灣積體電路製造股份有限公司",
        "公司簡稱": "台積電",
        "產業別": "24",
        "股票過戶機構": "中國信託商業銀行 代理部",
        "過戶電話": "02-6636-5566",
    },
    {
        "公司代號": "2454",
        "公司名稱": "聯發科技股份有限公司",
        "公司簡稱": "聯發科",
        "產業別": "24",
        "股票過戶機構": "",
        "過戶電話": "",
    },
]

ETF_ROWS = [
    {
        "基金代號": "0050",
        "基金簡稱": "元大台灣50",
        "基金中文名稱": "元大台灣卓越50證券投資信託基金",
        "標的指數/追蹤指數名稱": "臺灣50指數",
        "保管機構": "臺灣集中保管結算所股份有限公司",
    }
]


def _mock_client() -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith(twse_adapter.STOCK_BASIC_INFO_PATH):
            return httpx2.Response(200, json=STOCK_ROWS)
        if request.url.path.endswith(twse_adapter.ETF_BASIC_INFO_PATH):
            return httpx2.Response(200, json=ETF_ROWS)
        return httpx2.Response(404, json={"error": "unexpected path"})

    return httpx2.Client(transport=httpx2.MockTransport(handler))


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


def test_sync_stock_catalog_inserts_rows(conn):
    count = sync.sync_stock_catalog(conn, _mock_client())

    assert count == 2
    stored = conn.execute(
        "SELECT code, name, transfer_agent_name FROM securities ORDER BY code"
    ).fetchall()
    assert [dict(row) for row in stored] == [
        {
            "code": "2330",
            "name": "台積電",
            "transfer_agent_name": "中國信託商業銀行 代理部",
        },
        {"code": "2454", "name": "聯發科", "transfer_agent_name": None},
    ]


def test_sync_etf_catalog_inserts_security_and_profile(conn):
    sync.sync_etf_catalog(conn, _mock_client())

    security = conn.execute(
        "SELECT security_type FROM securities WHERE code = '0050'"
    ).fetchone()
    profile = conn.execute(
        "SELECT s.code, p.custodian_bank, p.custodian_institution_raw, "
        "p.benchmark, p.distribution_frequency "
        "FROM etf_profiles p JOIN securities s ON s.id = p.security_id"
    ).fetchone()

    assert security["security_type"] == "etf"
    assert profile["code"] == "0050"
    # Design 004 §4.3.1: the raw t187ap47_L value is reference-only —
    # `custodian_bank` is left unset until a per-投信 adapter confirms
    # it (Phase E), not filled from this basic-info sync.
    assert profile["custodian_bank"] is None
    assert profile["custodian_institution_raw"] == "臺灣集中保管結算所股份有限公司"
    assert profile["benchmark"] == "臺灣50指數"
    assert profile["distribution_frequency"] is None


def test_resyncing_etf_catalog_does_not_clobber_custodian_bank_set_elsewhere(conn):
    """Regression test for design 004 §2.1: the basic-info sync used to
    unconditionally overwrite `custodian_bank`/`distribution_frequency`
    on every run, which would silently wipe out a value a future
    per-投信 adapter (or user override) had set. `upsert_etf_profile`
    must leave those columns alone."""
    sync.sync_etf_catalog(conn, _mock_client())
    security_id = conn.execute(
        "SELECT id FROM securities WHERE code = '0050'"
    ).fetchone()["id"]
    conn.execute(
        "UPDATE etf_profiles SET custodian_bank = ?, distribution_frequency = ? "
        "WHERE security_id = ?",
        ("中國信託商業銀行", "quarterly", security_id),
    )

    sync.sync_etf_catalog(conn, _mock_client())

    profile = conn.execute(
        "SELECT custodian_bank, distribution_frequency FROM etf_profiles "
        "WHERE security_id = ?",
        (security_id,),
    ).fetchone()
    assert profile["custodian_bank"] == "中國信託商業銀行"
    assert profile["distribution_frequency"] == "quarterly"


def test_upsert_security_preserves_listed_date_when_a_later_sync_omits_it(conn):
    # Regression test for design 005 §4.5 follow-up: TPEx's ETF list
    # source has no listed-date field at all, so re-syncing an ETF whose
    # `listed_date` was already learned from an earlier sync (its stock
    # basic info, or TWSE's ETF basic info) must not wipe it back to
    # unknown just because this particular source doesn't report it.
    fields = {
        "market": "TWSE",
        "code": "0050",
        "name": "元大台灣50",
        "security_type": "etf",
        "industry": None,
        "transfer_agent_name": None,
        "transfer_agent_phone": None,
        "status": "active",
    }
    security_id = sync.upsert_security(conn, {**fields, "listed_date": "2003-06-30"})

    sync.upsert_security(conn, fields)

    row = conn.execute(
        "SELECT listed_date FROM securities WHERE id = ?", (security_id,)
    ).fetchone()
    assert row["listed_date"] == "2003-06-30"


def test_resyncing_updates_existing_rows_without_duplicating(conn):
    sync.sync_stock_catalog(conn, _mock_client())
    first_id = conn.execute("SELECT id FROM securities WHERE code = '2330'").fetchone()[
        "id"
    ]

    sync.sync_stock_catalog(conn, _mock_client())

    rows = conn.execute("SELECT id FROM securities WHERE code = '2330'").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == first_id


def test_partial_failure_rolls_back_the_whole_sync(conn):
    def handler(request: httpx2.Request) -> httpx2.Response:
        # Second row is missing the required 公司代號 key, which
        # normalize_stock will raise a KeyError on partway through.
        return httpx2.Response(200, json=[STOCK_ROWS[0], {"公司名稱": "缺代號公司"}])

    client = httpx2.Client(transport=httpx2.MockTransport(handler))

    with pytest.raises(KeyError):
        sync.sync_stock_catalog(conn, client)

    assert conn.execute("SELECT COUNT(*) AS n FROM securities").fetchone()["n"] == 0


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


def _etf_distribution_payload(rows: list[list]) -> dict:
    return {"status": "ok", "fields": ETF_DISTRIBUTION_FIELDS, "data": rows}


_ETF_0050_DISTRIBUTION_ROW = [
    "0050",
    "元大台灣50",
    "115年08月18日",
    "115年08月24日",
    "115年09月11日",
    "1.01",
    "…",
    115,
]


def test_sync_etf_distributions_inserts_events(conn):
    sync.sync_etf_catalog(conn, _mock_client())
    security_id = conn.execute(
        "SELECT id FROM securities WHERE code = '0050'"
    ).fetchone()["id"]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/rwd/zh/ETF/etfDiv"):
            if request.url.params.get("startDate") == "2026":
                return httpx2.Response(
                    200, json=_etf_distribution_payload([_ETF_0050_DISTRIBUTION_ROW])
                )
            return httpx2.Response(200, json=_etf_distribution_payload([]))
        return httpx2.Response(404, json={"error": "unexpected path"})

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    result = sync_etf_distributions.sync_etf_distributions(
        conn, client, reference_year=2026
    )

    assert result.pop("run_id")
    assert result == {
        "synced": 1,
        "inserted": 1,
        "updated": 0,
        "unchanged": 0,
        "skipped_no_ex_date": 0,
        "failed_units": [],
    }
    row = conn.execute(
        "SELECT ex_dividend_date, record_date, pay_date, amount_per_share, "
        "amount_status, source FROM distributions WHERE security_id = ?",
        (security_id,),
    ).fetchone()
    assert row["ex_dividend_date"] == "2026-08-18"
    assert row["record_date"] == "2026-08-24"
    assert row["pay_date"] == "2026-09-11"
    assert row["amount_per_share"] == "1.0100"
    assert row["amount_status"] == "final"
    assert row["source"] == "twse_etf_distribution_list"


def test_resyncing_etf_distributions_does_not_duplicate(conn):
    sync.sync_etf_catalog(conn, _mock_client())

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/rwd/zh/ETF/etfDiv"):
            return httpx2.Response(
                200, json=_etf_distribution_payload([_ETF_0050_DISTRIBUTION_ROW])
            )
        return httpx2.Response(404, json={"error": "unexpected path"})

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    sync_etf_distributions.sync_etf_distributions(conn, client, reference_year=2026)
    sync_etf_distributions.sync_etf_distributions(conn, client, reference_year=2026)

    count = conn.execute("SELECT COUNT(*) AS n FROM distributions").fetchone()["n"]
    assert count == 1


_ETF_0050_DISTRIBUTION_ROW_CORRECTED = [
    "0050",
    "元大台灣50",
    "115年08月25日",
    "115年08月31日",
    "115年09月18日",
    "1.01",
    "…",
    115,
]


def _single_year_etf_handler(row: list, *, year: str = "2026"):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/rwd/zh/ETF/etfDiv"):
            if request.url.params.get("startDate") == year:
                return httpx2.Response(200, json=_etf_distribution_payload([row]))
            return httpx2.Response(200, json=_etf_distribution_payload([]))
        return httpx2.Response(404, json={"error": "unexpected path"})

    return handler


def test_ex_dividend_date_correction_flags_old_row_for_review(conn):
    # Design 009 §4.4: TWSE's ETF distribution list has no stable event
    # ID, so when a code's ex_dividend_date changes between resyncs of
    # the same year, the old row must be flagged rather than silently
    # replaced or auto-merged with the new one.
    sync.sync_etf_catalog(conn, _mock_client())

    client = httpx2.Client(
        transport=httpx2.MockTransport(
            _single_year_etf_handler(_ETF_0050_DISTRIBUTION_ROW)
        )
    )
    sync_etf_distributions.sync_etf_distributions(conn, client, reference_year=2026)

    client = httpx2.Client(
        transport=httpx2.MockTransport(
            _single_year_etf_handler(_ETF_0050_DISTRIBUTION_ROW_CORRECTED)
        )
    )
    sync_etf_distributions.sync_etf_distributions(conn, client, reference_year=2026)

    rows = {
        r["ex_dividend_date"]: r
        for r in conn.execute(
            "SELECT ex_dividend_date, reconciliation_status, reconciliation_note, "
            "reconciliation_detected_at FROM distributions"
        ).fetchall()
    }
    assert set(rows) == {"2026-08-18", "2026-08-25"}
    assert rows["2026-08-18"]["reconciliation_status"] == "needs_review"
    assert rows["2026-08-18"]["reconciliation_note"]
    assert rows["2026-08-18"]["reconciliation_detected_at"]
    assert rows["2026-08-25"]["reconciliation_status"] is None


def test_ex_dividend_date_reappearing_clears_reconciliation_flag(conn):
    sync.sync_etf_catalog(conn, _mock_client())

    client = httpx2.Client(
        transport=httpx2.MockTransport(
            _single_year_etf_handler(_ETF_0050_DISTRIBUTION_ROW)
        )
    )
    sync_etf_distributions.sync_etf_distributions(conn, client, reference_year=2026)
    client = httpx2.Client(
        transport=httpx2.MockTransport(
            _single_year_etf_handler(_ETF_0050_DISTRIBUTION_ROW_CORRECTED)
        )
    )
    sync_etf_distributions.sync_etf_distributions(conn, client, reference_year=2026)
    flagged = conn.execute(
        "SELECT reconciliation_status FROM distributions "
        "WHERE ex_dividend_date = '2026-08-18'"
    ).fetchone()
    assert flagged["reconciliation_status"] == "needs_review"

    # The source reverts to the original date on a later sync (e.g. the
    # correction itself gets corrected back).
    client = httpx2.Client(
        transport=httpx2.MockTransport(
            _single_year_etf_handler(_ETF_0050_DISTRIBUTION_ROW)
        )
    )
    sync_etf_distributions.sync_etf_distributions(conn, client, reference_year=2026)

    row = conn.execute(
        "SELECT reconciliation_status FROM distributions "
        "WHERE ex_dividend_date = '2026-08-18'"
    ).fetchone()
    assert row["reconciliation_status"] is None


def test_etf_distributions_partial_failure_is_isolated_to_that_unit(conn):
    # Design 004 §4.6 points 4/9: one bad (product, year) request must
    # not roll back or block the others.
    sync.sync_etf_catalog(conn, _mock_client())

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/rwd/zh/ETF/etfDiv"):
            year = request.url.params.get("startDate")
            if year == "2025":
                return httpx2.Response(500, json={"error": "boom"})
            # Year-aware (design 062): the 2026 event only for 2026.
            rows = [_ETF_0050_DISTRIBUTION_ROW] if year == "2026" else []
            return httpx2.Response(200, json=_etf_distribution_payload(rows))
        return httpx2.Response(404, json={"error": "unexpected path"})

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    result = sync_etf_distributions.sync_etf_distributions(
        conn, client, reference_year=2026
    )

    assert result["failed_units"] == ["0050:2025"]
    assert result["synced"] == 1
    count = conn.execute("SELECT COUNT(*) AS n FROM distributions").fetchone()["n"]
    assert count == 1


TPEX_ETF_LIST_SUGGESTIONS = ["00719B 元大美債1-3", "00858 永豐美國500大"]


def _tpex_etf_list_mock_client() -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/api/etf"):
            return httpx2.Response(
                200, json={"query": "", "suggestions": TPEX_ETF_LIST_SUGGESTIONS}
            )
        return httpx2.Response(404, json={"error": "unexpected path"})

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_sync_tpex_etf_list_inserts_securities_and_empty_profiles(conn):
    # Design 004 §4.2: TPEx ETFs (mostly bond ETFs) are a separate
    # source from `sync_etf_catalog`'s TWSE dataset — no combined
    # basic-info dataset covers both markets.
    count = sync.sync_tpex_etf_list(conn, _tpex_etf_list_mock_client())

    assert count == 2
    rows = conn.execute(
        "SELECT s.code, s.market, s.security_type, p.custodian_bank, "
        "p.custodian_institution_raw "
        "FROM securities s JOIN etf_profiles p ON p.security_id = s.id "
        "ORDER BY s.code"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "code": "00719B",
            "market": "TPEx",
            "security_type": "etf",
            "custodian_bank": None,
            "custodian_institution_raw": None,
        },
        {
            "code": "00858",
            "market": "TPEx",
            "security_type": "etf",
            "custodian_bank": None,
            "custodian_institution_raw": None,
        },
    ]


def test_resyncing_tpex_etf_list_does_not_duplicate(conn):
    client = _tpex_etf_list_mock_client()
    sync.sync_tpex_etf_list(conn, client)

    sync.sync_tpex_etf_list(conn, client)

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM securities WHERE market = 'TPEx' "
        "AND security_type = 'etf'"
    ).fetchone()["n"]
    assert count == 2


_TPEX_ETF_DISTRIBUTION_ROW = {
    "amount": "0.27",
    "stockName": "元大美債1-3",
    "divDate": "115年07月21日",
    "year": "115",
    "inDate": "115年08月10日",
    "description": "…",
    "inBaseDate": "115年07月27日",
    "stockNo": "00719B",
}


def test_sync_tpex_etf_distributions_inserts_events(conn):
    sync.sync_tpex_etf_list(conn, _tpex_etf_list_mock_client())
    security_id = conn.execute(
        "SELECT id FROM securities WHERE code = '00719B'"
    ).fetchone()["id"]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/api/etfExDiv"):
            form = dict(httpx2.QueryParams(request.read().decode()))
            if form.get("stkNo") == "00719B" and form.get("startDate") == "20260101":
                return httpx2.Response(200, json=[_TPEX_ETF_DISTRIBUTION_ROW])
            return httpx2.Response(200, json=[])
        return httpx2.Response(404, json={"error": "unexpected path"})

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    result = sync_etf_distributions.sync_tpex_etf_distributions(
        conn, client, reference_year=2026
    )

    assert result.pop("run_id")
    assert result == {
        "synced": 1,
        "inserted": 1,
        "updated": 0,
        "unchanged": 0,
        "skipped_no_ex_date": 0,
        "failed_units": [],
    }
    row = conn.execute(
        "SELECT ex_dividend_date, record_date, pay_date, amount_per_share, "
        "amount_status, source FROM distributions WHERE security_id = ?",
        (security_id,),
    ).fetchone()
    assert row["ex_dividend_date"] == "2026-07-21"
    assert row["record_date"] == "2026-07-27"
    assert row["pay_date"] == "2026-08-10"
    assert row["amount_per_share"] == "0.2700"
    assert row["amount_status"] == "final"
    assert row["source"] == "tpex_etf_distribution_list"


def test_resync_preserves_existing_watchlist_reference(conn):
    sync.sync_stock_catalog(conn, _mock_client())
    security_id = conn.execute(
        "SELECT id FROM securities WHERE code = '2330'"
    ).fetchone()["id"]
    conn.execute("INSERT INTO watchlist (security_id) VALUES (?)", (security_id,))

    sync.sync_stock_catalog(conn, _mock_client())

    watchlist_row = conn.execute(
        "SELECT security_id FROM watchlist WHERE security_id = ?", (security_id,)
    ).fetchone()
    assert watchlist_row is not None
