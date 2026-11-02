"""Page actions retain native form state and server-rendered sync feedback."""

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.web.routes_sync_jobs import get_job_launcher
from taiwan_stock.web.templates import templates


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.button = None
        self.depth = 0
        self.actions_depth = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.depth += 1
            if "page-actions" in attrs.get("class", "").split():
                self.actions_depth = self.depth
        if tag == "form":
            self.current = {
                "attrs": attrs,
                "fields": {},
                "buttons": [],
                "in_actions": self.actions_depth is not None,
            }
            self.forms.append(self.current)
        elif self.current is not None:
            if tag == "input" and "name" in attrs:
                self.current["fields"][attrs["name"]] = attrs.get("value", "")
            elif tag == "button":
                self.button = {"attrs": attrs, "text": ""}
                self.current["buttons"].append(self.button)

    def handle_endtag(self, tag):
        if tag == "button":
            self.button = None
        elif tag == "form":
            self.current = None
        elif tag == "div":
            if self.depth == self.actions_depth:
                self.actions_depth = None
            self.depth -= 1

    def handle_data(self, text):
        if self.button is not None:
            self.button["text"] += text


@pytest.mark.parametrize(
    "path,operation,action,label",
    [
        ("/", "prices", "/prices/sync", "同步最新股價"),
        (
            "/securities?security_type=stock&view=watchlist&q=2330&sort=code"
            "&direction=desc",
            "catalog",
            "/securities/sync",
            "同步商品名單",
        ),
        (
            "/securities?security_type=etf&view=all",
            "etf-data",
            "/securities/profiles/sync",
            "同步 ETF 資料",
        ),
        (
            "/corporate-actions",
            "stock-announcements",
            "/corporate-actions/sync",
            "同步除權息公告",
        ),
        (
            "/income-calendar?month=2026-08",
            "held-etf-distributions",
            "/income-calendar/refresh-etf",
            "同步 ETF 配息",
        ),
    ],
)
def test_page_sync_keeps_native_post_and_return_state(
    tmp_path, path, operation, action, label
):
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(path)
    assert response.status_code == 200
    sync_forms = [
        form
        for form in Forms(response.text).forms
        if form["attrs"].get("data-sync-operation") == operation
    ]
    assert len(sync_forms) == 1
    form = sync_forms[0]
    assert form["in_actions"]
    assert form["attrs"]["method"] == "post"
    assert form["attrs"]["action"] == action
    assert form["fields"]["request_key"]
    button = form["buttons"][0]
    assert button["text"].strip() == label
    assert {"secondary", "action-sync"} <= set(button["attrs"]["class"].split())
    assert "disabled" not in button["attrs"]
    assert f'id="sync-hint-{operation}"' in response.text
    if operation == "catalog":
        original = parse_qs(urlsplit(path).query)
        returned = parse_qs(urlsplit(form["fields"]["return_to"]).query)
        assert {key: returned[key] for key in original} == original
        assert returned["page"] == ["1"]
    elif operation == "held-etf-distributions":
        assert form["fields"]["month"] == "2026-08"
        assert form["fields"]["filter"] == "all"
    elif operation in ("prices", "stock-announcements"):
        assert response.text.index('class="button action-button action-create"') > (
            response.text.index(f'data-sync-operation="{operation}"')
        )


@pytest.mark.parametrize(
    "operation,status,expected",
    [
        ("stock-announcements", "queued", "準備同步…"),
        ("stock-announcements", "running", "同步中…"),
        ("stock-dividends", "running", "其他同步中…"),
    ],
)
def test_server_rendered_sync_distinguishes_own_job_from_conflict(
    operation, status, expected
):
    module = templates.env.get_template("_actions.html").make_module(
        {
            "sync_snapshot": {
                "jobs": [{"operation": operation, "status": status}],
                "blocked_operations": ["stock-announcements"],
            }
        }
    )
    html = str(module.sync_button("stock-announcements", "同步除權息公告", "同步說明"))
    button = Forms(f"<form>{html}</form>").forms[0]["buttons"][0]
    assert "disabled" in button["attrs"]
    assert "title" not in button["attrs"]
    assert button["text"].strip() == expected
    conflict = operation == "stock-dividends"
    assert (' title="其他同步正在執行，完成後即可同步"' in html) == conflict
    assert (' title="同步說明"' in html) != conflict
    assert 'class="sr-only" id="sync-hint-stock-announcements"' in html


def _no_worker(data_dir, job_id):
    raise OSError("no worker in this test")


def _add_security(data_dir):
    conn = get_connection(data_dir)
    try:
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
        )
    finally:
        conn.close()


@pytest.mark.parametrize("return_to", ["/\\evil.example", "https://evil.example/"])
@pytest.mark.parametrize(
    "action,default,field",
    [
        ("/securities/sync", "/securities", "job_id"),
        ("/watchlist/add", "/securities", "watchlist_added"),
        ("/watchlist/remove", "/securities", "watchlist_removed"),
    ],
)
def test_form_returns_reject_non_local_targets(
    tmp_path, return_to, action, default, field
):
    # Design 030: every user-supplied return path goes through
    # `local_return`, so a browser can't read "/\evil" as "//evil".
    app = create_app(tmp_path)
    app.dependency_overrides[get_job_launcher] = lambda: _no_worker
    with TestClient(app) as client:
        _add_security(tmp_path)
        response = client.post(
            action,
            data={"security_id": "sec-2330", "return_to": return_to},
            follow_redirects=False,
        )
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert (location.scheme, location.netloc, location.path) == ("", "", default)
    assert field in parse_qs(location.query)


def test_watchlist_return_keeps_local_query(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        _add_security(tmp_path)
        response = client.post(
            "/watchlist/add",
            data={"security_id": "sec-2330", "return_to": "/securities?view=all#top"},
            follow_redirects=False,
        )
    assert response.headers["location"] == (
        "/securities?view=all&watchlist_added=1#top"
    )
