"""Pages load the shared column-resize script after page scripts (design 045)."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from taiwan_stock.main import create_app


def test_every_page_loads_the_resize_script_after_page_scripts(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        pages = {
            path: client.get(path).text
            for path in (
                "/",
                "/transactions",
                "/securities?security_type=etf",
                "/settings",
            )
        }
    for path, html in pages.items():
        scripts = re.findall(r'<script src="/static/([\w-]+)\.js', html)
        assert scripts[-1] == "table-columns", path
    # holdings-table.js rebuilds its headers; the handles must be added after.
    home = re.findall(r'<script src="/static/([\w-]+)\.js', pages["/"])
    assert home.index("holdings-table") < home.index("table-columns")


def test_securities_headers_have_stable_column_keys(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        html = client.get(
            "/securities?security_type=etf&view=all&pay_month=2026-10"
        ).text
    keys = re.findall(r'<th scope="col"[^>]*data-col-key="([\w]+)"', html)
    assert keys[:4] == ["code", "name", "market", "net_assets"]
    # The payment-month heading carries the month; its key does not.
    assert "pay_date" in keys


def test_compact_tables_are_marked(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        settings = client.get("/settings").text
    assert '<table class="backup-table compact-table">' in settings
    assert '<table class="tax-review-table compact-table">' in settings


def test_table_rows_are_top_aligned_everywhere():
    """Issue #143: rows with multi-line cells start on one line in every table."""
    css = (
        Path(__file__).resolve().parents[2] / "src/taiwan_stock/static/app.css"
    ).read_text(encoding="utf-8")
    rules = re.findall(r"([^{}]*)\{([^}]*)\}", css)
    centred = [
        selector.strip()
        for selector, body in rules
        if re.search(r"\b(tbody|td)\b", selector)
        and re.search(r"vertical-align:\s*(middle|bottom)", body)
    ]
    assert centred == []
