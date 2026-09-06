"""Exercise browser request ordering and user interactions with controlled DOM seams."""

import os
import shutil
import subprocess

import pytest


def browser_node():
    node = shutil.which("node")
    if node is None:
        if any(os.environ.get(name, "").lower() not in {"", "0", "false"}
               for name in ("CI", "GITHUB_ACTIONS", "SOLAR_REQUIRE_BROWSER_TESTS")):
            pytest.fail("Node.js is required in CI for the offline browser behavior harness")
        pytest.skip("Node.js is needed for the offline browser behavior harness")
    return node


def test_dashboard_browser_behavior():
    node = browser_node()
    subprocess.run([node, "tests/dashboard_browser.cjs"], check=True, timeout=30)


@pytest.mark.parametrize("flag", ["CI", "GITHUB_ACTIONS", "SOLAR_REQUIRE_BROWSER_TESTS"])
def test_missing_node_fails_when_browser_gate_is_required(monkeypatch, flag):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    for name in ("CI", "GITHUB_ACTIONS", "SOLAR_REQUIRE_BROWSER_TESTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(flag, "true")
    with pytest.raises(pytest.fail.Exception, match="Node.js is required in CI"):
        browser_node()


def test_missing_node_is_an_explicit_optional_local_skip(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    for flag in ("CI", "GITHUB_ACTIONS", "SOLAR_REQUIRE_BROWSER_TESTS"):
        monkeypatch.delenv(flag, raising=False)
    with pytest.raises(pytest.skip.Exception, match="Node.js is needed"):
        browser_node()
