"""Shared Jinja2Templates instance for main.py and the page routers."""

import hashlib
from contextlib import closing, nullcontext
from pathlib import Path

from fastapi.templating import Jinja2Templates

from taiwan_stock.catalog import sync_history
from taiwan_stock.catalog.industries import industry_filter_label, industry_name
from taiwan_stock.infrastructure import sync_jobs
from taiwan_stock.infrastructure.db import get_runtime_connection
from taiwan_stock.shared.ids import new_id
from taiwan_stock.web.presentation import (
    STATUS_LABELS,
    hundred_million,
    market_label,
    number,
    taipei_time,
)


def sync_context(request):
    from taiwan_stock.web.routes_sync_jobs import result_summary

    # Design 065: the route's own connection when it has one; reads only, so
    # an open transaction of the route's is left as it is.
    shared = getattr(request.state, "db", None)
    with (
        nullcontext(shared)
        if shared is not None
        else closing(get_runtime_connection(request.app.state.data_dir))
    ) as conn:
        saved = request.query_params.get("saved", "")
        row = (
            conn.execute(
                "SELECT s.code,s.name FROM trades t "
                "JOIN securities s ON s.id=t.security_id WHERE t.id=?",
                (saved,),
            ).fetchone()
            if saved and request.url.path != "/transactions"
            else None
        )
        save_notice = f"已儲存交易：{row['code']} {row['name']}。" if row else None
        snapshot = sync_jobs.snapshot(
            conn, request.app.state.data_dir, recover_jobs=False
        )
        selected = sync_jobs.get(conn, request.query_params.get("job_id", ""))
        if selected:
            selected["summary"] = result_summary(selected)
    return {
        "save_notice": save_notice,
        "sync_snapshot": snapshot,
        "selected_job": selected,
        "sync_keys": {operation: new_id() for operation in sync_jobs.RESOURCES},
    }


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_static_versions = {}


def static_url(path):
    """`/static/<path>?v=<content hash>` (Issue #228): a changed file gets a
    new URL, so a browser never pairs a new page with a cached old script or
    stylesheet. Re-hashed when the file's mtime or size changes, so an edit
    shows on the next page load without a restart. A missing file raises."""
    file = STATIC_DIR / path
    stat = file.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _static_versions.get(path)
    if cached is None or cached[0] != key:
        cached = (key, hashlib.sha256(file.read_bytes()).hexdigest()[:12])
        _static_versions[path] = cached
    return f"/static/{path}?v={cached[1]}"


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR), context_processors=[sync_context]
)


templates.env.filters["number"] = number
templates.env.filters["hundred_million"] = hundred_million
templates.env.filters["market_label"] = market_label
templates.env.filters["taipei_time"] = taipei_time
templates.env.filters["industry_name"] = industry_name
templates.env.filters["industry_filter_label"] = industry_filter_label
templates.env.globals["static_url"] = static_url
templates.env.globals["status_labels"] = STATUS_LABELS
templates.env.globals["source_labels"] = sync_history.SOURCE_LABELS
