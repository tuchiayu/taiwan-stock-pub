"""Run the actual JavaScript controller through its DOM/network boundary."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_sync_controller_restores_time_and_never_resubmits_uncertain_post():
    node = shutil.which("node")
    if not node:
        windows_node = Path("C:/Program Files/nodejs/node.exe")
        node = str(windows_node) if windows_node.exists() else None
    if not node:
        if os.environ.get("CI"):
            pytest.fail("CI requires Node for all JavaScript controller tests")
        pytest.skip("Node is required for JavaScript controller tests")
    result = subprocess.run(
        [node, "--test", *map(str, sorted(Path("tests/js").glob("test_*.cjs")))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "terminal refresh passed" in result.stdout
