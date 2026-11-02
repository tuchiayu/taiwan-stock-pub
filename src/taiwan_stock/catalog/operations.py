"""One allowlisted operation registry shared by Web, CLI, and the worker."""

import json
from datetime import date

from taiwan_stock.catalog import (
    delistings,
    frequency_inference,
    host_pool,
    profile_enrichment,
    sync,
    sync_daily_prices,
    sync_etf_distributions,
    sync_history,
    sync_investment_trust,
    sync_stock_dividends,
    tpex_adapter,
)
from taiwan_stock.shared import pacing, sync_progress
from taiwan_stock.shared.dates import today_taipei

_ANNUAL_HISTORY_BACKFILL_CODES = frozenset({"0051", "0053", "0055", "006201"})


def _annual_history_backfill_scope(conn):
    """Four verified annual ETFs need one older exchange year for a third event.

    Only schedule a missing year if the current two events are a plausible
    annual pair. This remains a one-time targeted fetch, not a broader default
    history window or a relaxation of the inference threshold.
    """
    units = sync_history.latest_units(conn)
    result = {"TWSE": {}, "TPEx": {}}
    for row in conn.execute(
        "SELECT s.id,s.code,s.market FROM securities s "
        "WHERE s.market IN ('TWSE','TPEx') "
        "AND s.security_type='etf' AND s.status='active'"
    ):
        if row["code"] not in _ANNUAL_HISTORY_BACKFILL_CODES:
            continue
        events = conn.execute(
            "SELECT ex_dividend_date FROM distributions WHERE security_id=? "
            "AND ex_dividend_date IS NOT NULL ORDER BY ex_dividend_date DESC LIMIT 3",
            (row["id"],),
        ).fetchall()
        if len(events) != 2:
            continue
        newer, older = (date.fromisoformat(event[0]) for event in events)
        if not 330 <= (newer - older).days <= 400:
            continue
        missing_year = older.year - 1
        if not sync_history.years_covered(
            units, row["id"], row["market"], [missing_year]
        ):
            result[row["market"]][row["id"]] = {missing_year}
    return result


SOURCE_WAIT_SECONDS = 5
SOURCE_WAIT_ATTEMPTS = 24  # two minutes: a held-ETF refresh takes ~30 seconds


def _when_source_free(work):
    """Design 052 §4.2.1: the job doesn't reserve the distribution sources,
    so a running 領息月曆 refresh is waited out instead of failing the step."""
    for attempt in range(SOURCE_WAIT_ATTEMPTS + 1):
        try:
            return work()
        except sync_history.SyncInProgress:
            if attempt == SOURCE_WAIT_ATTEMPTS:
                raise
            pacing.pause(SOURCE_WAIT_SECONDS)


_PROFILE_COLUMNS = (
    "custodian_bank",
    "distribution_frequency",
    "distribution_policy",
    "issuer_name",
    "benchmark",
)


def _profile_snapshot(conn):
    return {
        r["security_id"]: dict(r) for r in conn.execute("SELECT * FROM etf_profiles")
    }


def _profile_result(conn, previous, result, basic):
    """The ETF data step's result, with how many of each field changed."""
    changed = {key: 0 for key in _PROFILE_COLUMNS}
    for row in conn.execute("SELECT * FROM etf_profiles"):
        for key in _PROFILE_COLUMNS:
            changed[key] += row[key] != previous.get(row["security_id"], {}).get(key)
    return {
        **result,
        "basic": basic,
        "coverage": profile_enrichment.coverage(conn),
        "changed_fields": changed,
    }


def _distribution_years():
    """Every listed ETF's current year (and next, in December): enough for
    下次配息; older years stay with the CLI and design 050's backfill."""
    today = date.fromisoformat(today_taipei())
    return [today.year] + ([today.year + 1] if today.month == 12 else [])


def _has_errors(value):
    if not isinstance(value, dict):
        return False
    return bool(
        value.get("errors")
        or value.get("failed_units")
        or value.get("incomplete_dates")
        or value.get("skipped_invalid_ratio")
        or value.get("validation_errors")
    ) or any(_has_errors(v) for v in value.values() if isinstance(v, dict))


_PHASES = {
    "stock_count": "TWSE 股票名單",
    "etf_count": "TWSE ETF 名單",
    "tpex_stock_count": "TPEx 股票名單",
    "tpex_etf_count": "TPEx ETF 名單",
}


def _begin_step(conn, progress, key):
    """`(True, result)` when a retried job reuses this step's earlier
    success; otherwise records it pending and returns `(False, first_row)`
    for `_end_step`."""
    previous_id = conn.execute(
        "SELECT retry_of FROM sync_jobs WHERE id=?", (progress.job_id,)
    ).fetchone()[0]
    if previous_id:
        previous = conn.execute(
            "SELECT result FROM sync_job_steps WHERE job_id=? AND step_key=? AND "
            "status='success'",
            (previous_id, key),
        ).fetchone()
        if previous:
            conn.execute(
                "INSERT INTO sync_job_steps VALUES(?,?,'success',?)",
                (progress.job_id, key, previous[0]),
            )
            return True, json.loads(previous[0])
    conn.execute(
        "INSERT INTO sync_job_steps VALUES(?,?,'pending',NULL)", (progress.job_id, key)
    )
    first_row = conn.execute(
        "SELECT COALESCE(max(rowid),0) FROM catalog_sync_runs"
    ).fetchone()[0]
    return False, first_row


def _end_step(conn, progress, key, result, first_row):
    # A circuit breaker can leave planned units pending even with no thrown error.
    source_failed = conn.execute(
        "SELECT 1 FROM catalog_sync_runs WHERE job_id=? AND rowid>? AND status "
        "IN ('partial','failed','interrupted')",
        (progress.job_id, first_row),
    ).fetchone()
    status = "partial" if _has_errors(result) or source_failed else "success"
    conn.execute(
        "UPDATE sync_job_steps SET status=?,result=? WHERE job_id=? AND step_key=?",
        (status, json.dumps(result, ensure_ascii=False), progress.job_id, key),
    )
    progress.update()


def step(conn, key, work):
    progress = sync_progress.current.get()
    if progress is None:
        return work()
    progress.phase = _PHASES.get(key, key)
    progress.update()
    reused, value = _begin_step(conn, progress, key)
    if reused:
        return value
    result = work()
    _end_step(conn, progress, key, result, value)
    return result


def parallel_steps(conn, parts, label=None):
    """Design 053: several syncs' requests in flight at once.

    `parts` are `(name, step_key, make_steps)`. Each part not reused from a
    retried job is started (its requests queued per host) before any is
    finished; then each is written in turn on this thread. A part's error
    lands in its own `{"error": …}` and never stops the others.
    """
    progress = sync_progress.current.get()
    results, begun = {}, []
    with host_pool.shared():
        for name, key, make_steps in parts:
            first_row = None
            if progress is not None:
                reused, first_row = _begin_step(conn, progress, key)
                if reused:
                    results[name] = first_row
                    continue
            try:
                finish = _when_source_free(lambda m=make_steps: host_pool.start(m()))
            except Exception as exc:  # noqa: BLE001 - reported per part
                results[name] = {"error": sync._short_reason(exc)}
                continue
            begun.append((name, key, first_row, finish))
        if progress is not None and begun:
            # Design 054 §4.2.4: one short label beats four step names.
            progress.phase = label or " · ".join(key for _, key, _, _ in begun)
            progress.update()
        for name, key, first_row, finish in begun:
            try:
                result = finish()
            except Exception as exc:  # noqa: BLE001 - reported per part
                results[name] = {"error": sync._short_reason(exc)}
                continue
            if progress is not None:
                _end_step(conn, progress, key, result, first_row)
            results[name] = result
    return results


def retry_source(conn, client, run_id):
    report = sync_history.get_report(conn, run_id)
    loaders = {
        "investment_trust": sync_investment_trust.sync_investment_trust_data,
        "etf_profile_basic": profile_enrichment.sync_basic_info,
        delistings.SOURCE: delistings.sync_delistings,
        "twse_etf_distribution_list": sync_etf_distributions.sync_etf_distributions,
        "tpex_etf_distribution_list": (
            sync_etf_distributions.sync_tpex_etf_distributions
        ),
    }
    kwargs = {"retry_run_id": run_id}
    if report["source"].endswith("etf_distribution_list"):
        years = [int(u["period"]) for u in report["units"] if u["period"]]
        if years:
            kwargs.update(reference_year=max(years), years=max(years) - min(years) + 1)
    result = loaders[report["source"]](conn, client, **kwargs)
    if report["source"].endswith("etf_distribution_list"):
        frequency_inference.recompute_all(conn)
    return result


def execute(conn, client, operation, options):
    def do(key, work):
        return step(conn, key, work)

    if operation == "market-calendar":
        from taiwan_stock.catalog import market_calendar

        return do(
            "更新 TWSE 休市行事曆",
            lambda: market_calendar.synchronize(conn, client, **options),
        )
    if operation == "etf-scale":
        from taiwan_stock.catalog import etf_scale

        return do(
            "更新 ETF 規模／受益人數",
            lambda: etf_scale.synchronize(conn, client, **options),
        )
    if operation == "prices":
        outcome = do(
            "更新 TWSE／TPEx 行情",
            lambda: sync_daily_prices.sync_daily_prices_both(conn, client),
        )
        return {
            **outcome["results"],
            "errors": outcome["errors"] or None,
            "total_synced": sum(r["synced"] for r in outcome["results"].values()),
        }
    if operation == "catalog":
        result = {}
        for key, work in (
            ("stock_count", lambda: sync.sync_stock_catalog(conn, client)),
            ("etf_count", lambda: sync.sync_etf_catalog(conn, client)),
            (
                "tpex_stock_count",
                lambda: sync.sync_stock_catalog(conn, client, adapter=tpex_adapter),
            ),
            ("tpex_etf_count", lambda: sync.sync_tpex_etf_list(conn, client)),
        ):
            result[key] = do(key, work)
        result["listingStatus"] = do(
            "核對上市狀態", lambda: delistings.sync_delistings(conn, client)
        )
        return result
    if operation == "stock-announcements":
        twse = do(
            "TWSE 除權息公告",
            lambda: sync_stock_dividends.sync_ex_dividend_notices(conn, client),
        )
        tpex = do(
            "TPEx 除權息公告",
            lambda: sync_stock_dividends.sync_ex_dividend_notices(
                conn, client, adapter=tpex_adapter
            ),
        )
        return {
            **{
                key: twse.get(key, 0) + tpex.get(key, 0)
                for key in (
                    "distributions",
                    "corporate_actions",
                    "skipped_unknown_security",
                    "skipped_invalid_ratio",
                )
            },
            **{
                key: twse.get(key, []) + tpex.get(key, [])
                for key in ("validation_errors", "reconciliation_warnings")
            },
            "history": do(
                "個股歷史現金股利",
                lambda: sync_stock_dividends.sync_stock_dividend_history(conn, client),
            ),
            "pay_dates": do(
                "補齊發放日",
                lambda: sync_stock_dividends.sync_stock_dividend_pay_dates(
                    conn, client
                ),
            ),
        }
    if operation == "held-etf-distributions":
        kwargs = {}
        if progress := sync_progress.current.get():
            stored = conn.execute(
                "SELECT plan FROM sync_jobs WHERE id=?", (progress.job_id,)
            ).fetchone()[0]
            plan = (
                json.loads(stored)
                if stored
                else {
                    "scope": {
                        market: sorted(ids)
                        for market, ids in sync_etf_distributions._held_etf_scope(
                            conn
                        ).items()
                    },
                    "today": today_taipei(),
                }
            )
            if stored is None:
                conn.execute(
                    "UPDATE sync_jobs SET plan=? WHERE id=?",
                    (json.dumps(plan), progress.job_id),
                )
            kwargs = {
                "today": plan["today"],
                "scope": {market: set(ids) for market, ids in plan["scope"].items()},
            }
        return do(
            "持有／追蹤 ETF 配息",
            lambda: sync_etf_distributions.refresh_held_etf_distributions(
                conn, client, **kwargs
            ),
        )
    if operation == "full-etf-distributions":
        result = {}
        for name, loader in (
            ("twse", sync_etf_distributions.sync_etf_distributions),
            ("tpex", sync_etf_distributions.sync_tpex_etf_distributions),
        ):
            result[name] = do(
                f"{name.upper()} ETF 配息",
                lambda loader=loader: loader(conn, client, **options),
            )
        backfill = _annual_history_backfill_scope(conn)
        for market, loader in (
            ("TWSE", sync_etf_distributions.sync_etf_distributions),
            ("TPEx", sync_etf_distributions.sync_tpex_etf_distributions),
        ):
            year_map = backfill[market]
            if year_map:
                result.setdefault("annual_backfill", {})[market.lower()] = do(
                    f"{market} 舊 ETF 年配歷史補缺",
                    lambda loader=loader, year_map=year_map: loader(
                        conn,
                        client,
                        security_ids=set(year_map),
                        year_list=sorted(set().union(*year_map.values())),
                        year_map=year_map,
                    ),
                )
        frequency_inference.recompute_all(conn)
        return result
    if operation == "investment-trust":
        previous = _profile_snapshot(conn)
        basic = do(
            "ETF 官方基本資料",
            lambda: profile_enrichment.sync_basic_info(
                conn, client, issuer=options.get("issuer")
            ),
        )
        result = do(
            "投信保管銀行／配息頻率",
            lambda: sync_investment_trust.sync_investment_trust_data(
                conn, client, **options
            ),
        )
        return _profile_result(conn, previous, result, basic)
    if operation == "etf-data":
        # Design 052 §4.2.1: the ETF tab's three syncs in their only sensible
        # order; one phase failing never stops the next.
        result = {}
        try:
            result["catalog"] = execute(conn, client, "catalog", {})
        except Exception as exc:  # noqa: BLE001 - reported per phase
            result["catalog"] = {"error": sync._short_reason(exc)}
        # Design 053 §4.2.3: once the list is fresh, ETF data and every
        # market's distributions fetch at once; they are written in the old
        # order (basic data, issuers, TWSE, TPEx).
        previous = _profile_snapshot(conn)
        years = _distribution_years()
        done = parallel_steps(
            conn,
            [
                (
                    "basic",
                    "ETF 官方基本資料",
                    lambda: profile_enrichment.basic_info_steps(conn, client),
                ),
                (
                    "trust",
                    "投信保管銀行／配息頻率",
                    lambda: sync_investment_trust.investment_trust_steps(conn, client),
                ),
                *(
                    (
                        market.lower(),
                        f"{market} ETF 配息",
                        lambda market=market: (
                            sync_etf_distributions.etf_distribution_steps(
                                conn, client, market, year_list=years
                            )
                        ),
                    )
                    for market in ("TWSE", "TPEx")
                ),
            ],
            label="ETF 資料與配息",
        )
        frequency_inference.recompute_all(conn)
        result["etf_profiles"] = _profile_result(
            conn, previous, done["trust"], done["basic"]
        )
        result["distributions"] = {
            "years": years,
            "twse": done["twse"],
            "tpex": done["tpex"],
        }
        return result
    if operation == "delistings":
        return do("上市／上櫃狀態", lambda: delistings.sync_delistings(conn, client))
    if operation == "stock-dividends":
        return {
            "history": do(
                "個股歷史現金股利",
                lambda: sync_stock_dividends.sync_stock_dividend_history(
                    conn, client, **options
                ),
            ),
            "pay_dates": do(
                "補齊發放日",
                lambda: sync_stock_dividends.sync_stock_dividend_pay_dates(
                    conn, client
                ),
            ),
        }
    if operation == "retry":
        return do("重試來源單位", lambda: retry_source(conn, client, options["run_id"]))
    raise ValueError("不支援的同步操作")
