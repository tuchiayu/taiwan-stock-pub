"""Securities query, catalog sync forms and sync reports (design 070)."""

import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from taiwan_stock.catalog import (
    bank_dictionary,
    profile_values,
    search,
    sync_history,
)
from taiwan_stock.catalog import (
    query as catalog_query,
)
from taiwan_stock.portfolio import (
    watchlist,
)
from taiwan_stock.shared.dates import today_taipei
from taiwan_stock.web import query_state
from taiwan_stock.web.deps import get_data_dir, get_db
from taiwan_stock.web.routes_sync_jobs import get_job_launcher, submit_form
from taiwan_stock.web.templates import templates

router = APIRouter()

DISTRIBUTION_FREQUENCY_LABELS = profile_values.FREQUENCY_LABELS


# The securities filter also separates stopped-trading rows from 資料不足.
def _securities_frequency_filters():
    filters = {}
    for key, label in DISTRIBUTION_FREQUENCY_LABELS.items():
        filters[key] = label
        if key == "quarterly":
            # Design 046 §4.2.3: each ex-dividend month group of 季配.
            for value, group in catalog_query.QUARTER_GROUP_FILTERS.items():
                months = profile_values.QUARTER_GROUP_MONTHS[group]
                filters[value] = f"季配・{'、'.join(map(str, months))} 月"
    filters[catalog_query.DELISTED] = "已下市"
    return filters


SECURITIES_FREQUENCY_FILTERS = _securities_frequency_filters()


@router.get("/securities", response_class=HTMLResponse)
def securities_page(
    request: Request,
    q: str | None = None,
    security_type: str | None = None,
    market: str | None = None,
    industry: str | None = None,
    pay_month: str | None = None,
    custodian_bank: str | None = None,
    distribution_frequency: str | None = None,
    sort: str | None = None,
    direction: str | None = None,
    view: str | None = None,
    page: int = 1,
    include_delisted: str | None = None,
    synced: int | None = None,
    sync_error: str | None = None,
    status_error: str | None = None,
    watchlist_added: int | None = None,
    watchlist_removed: int | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    # Design 047: a bare entry opens ETF / 全部商品 (reverses 004's stock default).
    mode = security_type if security_type in ("stock", "etf") else "etf"
    market = market if market in ("TWSE", "TPEx") else None
    industry = industry if mode == "stock" else None
    if mode != "etf":
        pay_month = custodian_bank = distribution_frequency = None
    if pay_month:
        try:
            date.fromisoformat(pay_month + "-01")
        except ValueError:
            pay_month = None
    bank = (
        custodian_bank
        if custodian_bank in ("unknown", catalog_query.DELISTED)
        else bank_dictionary.canonical_name(custodian_bank)
    )
    frequency = (
        distribution_frequency
        if distribution_frequency in SECURITIES_FREQUENCY_FILTERS
        else None
    )
    # Design 048 §4.2.1: an older 已下市 filter link still lists those rows.
    include_delisted = include_delisted == "1" or catalog_query.DELISTED in (
        bank,
        frequency,
    )
    watched = watchlist.list_watchlist(conn, security_type=mode)
    view = view if view in ("all", "watchlist") else "all"
    sort, direction = catalog_query.normalize_sort(sort, direction, mode)
    data = catalog_query.browse(
        conn,
        mode=mode,
        view=view,
        q=q,
        market=market,
        industry=industry,
        pay_month=pay_month,
        bank=bank,
        frequency=frequency,
        sort=sort,
        direction=direction,
        page=page,
        include_delisted=include_delisted,
    )
    state = {
        "security_type": mode,
        "view": view,
        "q": q or "",
        "market": market or "",
        "industry": industry or "",
        "pay_month": pay_month or "",
        "custodian_bank": bank or "",
        "distribution_frequency": frequency or "",
        "sort": sort,
        "direction": direction,
        "page": data["page"],
        "include_delisted": "1" if include_delisted else "",
    }
    invalid_bank = (
        bank
        and bank not in ("unknown", catalog_query.DELISTED)
        and bank not in search.list_custodian_banks(conn)
    )
    context = {
        **state,
        **data,
        "query": q or "",
        "watchlist_count": len(watched),
        "today": today_taipei(),
        "watchlist_ids": watchlist.get_watchlist_security_ids(conn),
        "catalog_size": conn.execute("SELECT count(*) FROM securities").fetchone()[0],
        "industries": search.list_industries(conn),
        "distribution_frequency_labels": SECURITIES_FREQUENCY_FILTERS,
        "tier_labels": profile_values.TIER_LABELS,
        "sort_links": query_state.sort_links(state, catalog_query.ALL_SORTS),
        "return_to": query_state.url(state),
        "page_link": lambda target: query_state.url(state, page=target),
        "clear_url": query_state.url({"security_type": mode, "view": view}),
        "all_url": query_state.url(state, view="all", page=1),
        "watchlist_url": query_state.url(state, view="watchlist", page=1),
        "stock_url": query_state.url(
            state,
            security_type="stock",
            custodian_bank=None,
            distribution_frequency=None,
            pay_month=None,
            include_delisted=None,
            sort="code",
            direction="asc",
            page=1,
        ),
        # Design 048 §4.2.3: no sort, so the ETF default (size) applies.
        "etf_url": query_state.url(
            state,
            security_type="etf",
            industry=None,
            include_delisted=None,
            sort=None,
            direction=None,
            page=1,
        ),
        "include_delisted_url": query_state.url(
            state, include_delisted=None if include_delisted else "1", page=1
        ),
        "invalid_bank": invalid_bank,
        "synced": synced,
        "sync_error": sync_error,
        "watchlist_added": watchlist_added,
        "status_error": status_error,
        "watchlist_removed": watchlist_removed,
        "sync_runs": [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM catalog_sync_runs ORDER BY rowid DESC LIMIT 5"
            )
        ],
    }
    return templates.TemplateResponse(
        request=request, name="securities.html", context=context
    )


@router.get("/catalog-sync-runs/{run_id}", response_class=HTMLResponse)
def catalog_sync_report(request: Request, run_id: str, conn=Depends(get_db)):
    """Read-only source report shared by Web background jobs and CLI updates."""
    report = sync_history.get_report(conn, run_id)
    if report is None:
        raise HTTPException(404, "找不到同步紀錄")
    return templates.TemplateResponse(
        request=request,
        name="catalog_sync_report.html",
        context={
            "report": report,
            "suspected_changes": sync_history.suspected_changes(
                conn, [report["source"]]
            ),
        },
    )


@router.post("/securities/sync")
def sync_securities_form(
    return_to: str = Form("/securities"),
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
    request_key: str = Form(""),
):
    return submit_form(
        conn, data_dir, "catalog", launcher, return_to, request_key or None
    )


@router.post("/securities/profiles/sync")
def sync_etf_profiles_form(
    return_to: str = Form("/securities?security_type=etf"),
    conn=Depends(get_db),
    data_dir=Depends(get_data_dir),
    launcher=Depends(get_job_launcher),
    request_key: str = Form(""),
):
    return submit_form(
        conn, data_dir, "etf-data", launcher, return_to, request_key or None
    )
