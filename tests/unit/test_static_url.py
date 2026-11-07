"""Issue #228: static asset URLs carry a content hash instead of a hand-kept ?v=."""

import hashlib
import os
import re

import pytest

from taiwan_stock.web import templates as web_templates
from taiwan_stock.web.templates import TEMPLATES_DIR, static_url

TEMPLATE_TEXTS = {
    path.name: path.read_text(encoding="utf-8")
    for path in sorted(TEMPLATES_DIR.glob("*.html"))
}


def test_templates_never_hand_write_a_static_version():
    hand_written = [
        f"{name}: {match}"
        for name, text in TEMPLATE_TEXTS.items()
        for match in re.findall(r"/static/[^\"' ]+\?v=", text)
    ]
    assert hand_written == []


def test_every_static_url_in_the_templates_names_an_existing_file():
    names = {
        name
        for text in TEMPLATE_TEXTS.values()
        for name in re.findall(r"static_url\('([^']+)'\)", text)
    }
    assert names  # The templates do use it.
    for name in sorted(names):
        assert re.fullmatch(
            rf"/static/{re.escape(name)}\?v=[0-9a-f]{{12}}", static_url(name)
        )


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(web_templates, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(web_templates, "_static_versions", {})
    return tmp_path


def test_the_version_follows_the_file_content(static_dir):
    asset = static_dir / "app.js"
    asset.write_bytes(b"one")
    first = static_url("app.js")
    assert first == "/static/app.js?v=" + hashlib.sha256(b"one").hexdigest()[:12]
    assert static_url("app.js") == first

    # An edit shows without a restart, even within the same mtime tick.
    stat = asset.stat()
    asset.write_bytes(b"three")
    os.utime(asset, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    assert static_url("app.js") == (
        "/static/app.js?v=" + hashlib.sha256(b"three").hexdigest()[:12]
    )


def test_a_missing_static_file_fails_with_its_name(static_dir):
    with pytest.raises(FileNotFoundError, match="nope.js"):
        static_url("nope.js")
