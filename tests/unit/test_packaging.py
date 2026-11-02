"""Design 066: the wheel declares what requirements.txt installs; tests skip
only the waits that pace external sources."""

import time
import tomllib
from pathlib import Path

from taiwan_stock.shared import pacing

ROOT = Path(__file__).resolve().parents[2]


def requirement_lines(text):
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def test_the_wheel_declares_the_pinned_runtime_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = set(project["project"]["dependencies"])
    pinned = requirement_lines((ROOT / "requirements.txt").read_text(encoding="utf-8"))
    assert declared == pinned


def test_a_missing_line_is_noticed():
    assert requirement_lines("fastapi==1\n# note\n\nuvicorn==2\n") != {"fastapi==1"}


def test_only_source_pacing_is_skipped():
    start = time.monotonic()
    pacing.pause(5)
    assert time.monotonic() - start < 1
    start = time.monotonic()
    time.sleep(0.05)
    assert time.monotonic() - start >= 0.04
