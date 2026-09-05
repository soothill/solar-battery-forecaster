import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
GUIDE = ROOT / "docs" / "setup-and-credentials.md"


def test_operator_guide_preserves_runtime_and_secret_boundaries() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    prose = " ".join(guide.split())

    assert "Last verified: 2026-09-06" in guide
    assert "recommendation-only" in guide
    assert "sends its exact batch directly to InfluxDB first" in prose
    assert "SQLite is used only after a failed or ambiguous InfluxDB write" in prose
    assert "does **not** prove" in prose
    assert "127.0.0.1:8088" in guide
    assert "authenticated HTTPS Nginx proxy in the same LXC" in guide
    assert "unauthenticated 401" in guide
    assert "outbox.sqlite3-wal" in guide
    assert "outbox.sqlite3-shm" in guide
    for service in [
        "solar-battery-telemetry",
        "solar-battery-tariff",
        "solar-battery-forecast-plan",
        "solar-battery-reconciliation",
        "solar-battery-dashboard",
    ]:
        assert service in guide


def test_operator_guide_documents_exact_influx_grants_and_provider_limits() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    prose = " ".join(guide.split())

    for description in [
        "solar-telemetry",
        "solar-tariff",
        "solar-forecast-plan",
        "solar-reconciliation",
        "solar-dashboard",
    ]:
        assert f"--description {description}" in guide
    assert (
        "--read-bucket TELEMETRY_BUCKET_ID --read-bucket TARIFF_BUCKET_ID"
        in guide
    )
    assert (
        "--read-bucket PLANNING_BUCKET_ID --write-bucket PLANNING_BUCKET_ID"
        in guide
    )
    assert "Monitoring" in guide
    assert "Do not request **Control**" in prose
    assert "AppSecret" in guide and "only once" in guide
    assert "does not use an Octopus API key" in prose
    assert "interval longer than two hours" in guide
    assert "public non-commercial API does not require an API key" in prose
    for hostname in [
        "openapi-eu.sigencloud.com",
        "openapi-apac.sigencloud.com",
        "openapi-cn.sigencloud.com",
        "openapi-aus.sigencloud.com",
        "openapi-us.sigencloud.com",
        "openapi-jp.sigencloud.com",
    ]:
        assert hostname in guide


def test_documentation_relative_links_resolve() -> None:
    markdown_files = [ROOT / "README.md", ROOT / "deployment" / "LXC.md", GUIDE]
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

    for markdown in markdown_files:
        text = markdown.read_text(encoding="utf-8")
        for target in link_pattern.findall(text):
            if target.startswith(("https://", "http://", "#")):
                continue
            path_text = target.split("#", 1)[0]
            assert (markdown.parent / path_text).resolve().exists(), (
                f"broken relative link in {markdown.relative_to(ROOT)}: {target}"
            )


def test_examples_use_placeholders_and_every_bash_block_has_context() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    telemetry_env = (
        ROOT / "deployment" / "environment" / "telemetry.env.example"
    ).read_text(encoding="utf-8")

    for placeholder in [
        "ORG_ID",
        "TELEMETRY_BUCKET_ID",
        "TARIFF_BUCKET_ID",
        "PLANNING_BUCKET_ID",
        "DASHBOARD_DNS_NAME",
        "DASHBOARD_USERNAME",
    ]:
        assert placeholder in guide
    for variable in [
        "SIGENERGY_HOME_APP_KEY",
        "SIGENERGY_HOME_APP_SECRET",
        "SIGENERGY_HOME_SYSTEM_ID",
    ]:
        assignment = next(
            line for line in telemetry_env.splitlines() if line.startswith(f"{variable}=")
        )
        assert "replace-with-" in assignment

    lines = guide.splitlines()
    for index, line in enumerate(lines):
        if line != "```bash":
            continue
        previous = next(item for item in reversed(lines[:index]) if item.strip())
        assert previous.startswith("**Run "), f"bash block lacks execution context: {index + 1}"
