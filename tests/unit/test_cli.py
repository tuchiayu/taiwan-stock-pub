"""Unit tests for the taiwan-stock CLI."""

from pathlib import Path
from unittest.mock import patch

import pytest

from taiwan_stock import cli


def test_cli_parser_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["web"])
    assert args.command == "web"
    assert args.port == 2330
    assert args.host == "127.0.0.1"
    assert args.no_browser is False
    assert args.data_dir is None


def test_cli_parser_custom_args():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "web",
            "--port",
            "8080",
            "--host",
            "0.0.0.0",
            "--no-browser",
            "--data-dir",
            "C:\\data",
        ]
    )
    assert args.command == "web"
    assert args.port == 8080
    assert args.host == "0.0.0.0"
    assert args.no_browser is True
    assert args.data_dir == Path("C:\\data")


def test_cli_no_args_returns_code_1(capsys):
    ret = cli.main([])
    assert ret == 1
    captured = capsys.readouterr()
    usage = "usage: taiwan-stock"
    assert usage in captured.out or usage in captured.err


def test_cli_web_subcommand_calls_uvicorn(tmp_path):
    cmd_args = [
        "web",
        "--port",
        "9999",
        "--no-browser",
        "--data-dir",
        str(tmp_path),
    ]
    with patch("uvicorn.run") as mock_run:
        ret = cli.main(cmd_args)
        assert ret == 0
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["port"] == 9999
        assert kwargs["host"] == "127.0.0.1"


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("127.0.0.1", "http://127.0.0.1:9999"),
        ("0.0.0.0", "http://127.0.0.1:9999"),
        ("::", "http://127.0.0.1:9999"),
        ("::1", "http://[::1]:9999"),
    ],
)
def test_cli_web_opens_a_url_the_trusted_host_check_accepts(tmp_path, host, url):
    # Issue #87: a wildcard bind must not open http://0.0.0.0, which the
    # server itself would now reject.
    with (
        patch("uvicorn.run") as mock_run,
        patch.object(cli, "create_app") as create_app,
        patch.object(cli.threading, "Timer") as timer,
    ):
        cli.main(["web", "--host", host, "--port", "9999", "--data-dir", str(tmp_path)])

    assert create_app.call_args.kwargs["bind_host"] == host
    assert mock_run.call_args.kwargs["host"] == host
    open_browser = timer.call_args.args[1]
    with patch.object(cli.webbrowser, "open") as browser:
        open_browser()
    browser.assert_called_once_with(url)


@pytest.mark.parametrize(
    ("host", "warned"),
    [
        ("127.0.0.1", False),
        ("localhost", False),
        ("::1", False),
        ("[::1]", False),
        ("0.0.0.0", True),
        ("192.168.1.5", True),
    ],
)
def test_cli_web_warns_when_bound_beyond_loopback(tmp_path, capsys, host, warned):
    with patch("uvicorn.run"), patch.object(cli, "create_app"):
        cli.main(["web", "--host", host, "--no-browser", "--data-dir", str(tmp_path)])

    assert ("沒有登入保護" in capsys.readouterr().err) == warned


@pytest.mark.parametrize("dev", [True, False])
def test_cli_web_labels_development_data(tmp_path, capsys, monkeypatch, dev):
    """Design 077: the startup line says when it is the source tree's data."""
    monkeypatch.setattr(cli, "dev_data_dir", lambda: tmp_path if dev else None)
    with patch("uvicorn.run"), patch.object(cli, "create_app"):
        cli.main(["web", "--no-browser", "--data-dir", str(tmp_path)])

    line = capsys.readouterr().out
    assert f"(data: {tmp_path}" in line
    assert ("開發資料" in line) is dev


def test_cli_sync_etf_distributions_parser_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["sync", "etf-distributions"])
    assert args.command == "sync"
    assert args.sync_target == "etf-distributions"
    assert args.years == 3
    assert args.reference_year is None
    assert args.data_dir is None


def test_cli_sync_investment_trust_accepts_known_issuer_only():
    parser = cli.build_parser()
    args = parser.parse_args(["sync", "investment-trust", "--issuer", "國泰"])
    assert args.issuer == "國泰"

    with pytest.raises(SystemExit):
        parser.parse_args(["sync", "investment-trust", "--issuer", "不存在的投信"])


def test_cli_sync_retry_requires_run_id():
    parser = cli.build_parser()
    args = parser.parse_args(["sync", "retry", "run-123"])
    assert args.sync_target == "retry"
    assert args.run_id == "run-123"

    with pytest.raises(SystemExit):
        parser.parse_args(["sync", "retry"])
