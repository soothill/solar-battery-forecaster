"""Exercise browser request ordering and user interactions with controlled DOM seams."""

import shutil
import subprocess

import pytest


def test_dashboard_browser_behavior():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the offline browser behavior harness")
    subprocess.run([node, "tests/dashboard_browser.cjs"], check=True, timeout=30)
