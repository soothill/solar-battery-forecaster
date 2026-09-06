import configparser
import re
import shlex
from pathlib import Path

import yaml

from solar_battery_forecaster import cli

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
    for variable_file in [
        "`telemetry.env`: `SIGENERGY_HOME_APP_KEY`",
        "`telemetry.env`: `SIGENERGY_HOME_APP_SECRET`",
        "`telemetry.env`: `SIGENERGY_HOME_SYSTEM_ID`",
        "`telemetry.env`: `INFLUX_TELEMETRY_TOKEN`",
        "`tariff.env`: `INFLUX_TARIFF_TOKEN`",
        "`forecast-plan.env`: `INFLUX_FORECAST_PLAN_TOKEN`",
        "`reconciliation.env`: `INFLUX_RECONCILIATION_TOKEN`",
        "`dashboard.env`: `INFLUX_DASHBOARD_TOKEN`",
    ]:
        assert variable_file in guide
    assert "reports the local CLI version, not the server version" in prose
    assert "https://influxdb.example.invalid:8086/health" in guide
    assert "organization **name**" in guide
    assert "bucket **IDs**" in guide


def test_operator_guide_links_primary_provider_and_proxy_references() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    for url in [
        "https://developer.sigencloud.com/user/user/manual/68",
        "https://developer.sigencloud.com/user/user/manual/69",
        "https://developer.sigencloud.com/user/user/manual/70",
        "https://developer.sigencloud.com/user/user/manual/77",
        "https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html",
        "https://nginx.org/en/docs/http/ngx_http_proxy_module.html",
    ]:
        assert url in guide


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
        if not re.fullmatch(r"```[a-zA-Z0-9_-]+", line):
            continue
        previous = next(item for item in reversed(lines[:index]) if item.strip())
        assert previous.startswith(("**Run ", "**Save ")), (
            f"code block lacks execution context: {index + 1}"
        )


def test_documentation_never_shell_sources_service_environment_files() -> None:
    markdown = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [ROOT / "README.md", ROOT / "deployment" / "LXC.md", GUIDE]
    )

    assert "set -a" not in markdown
    assert ". /etc/solar-battery-forecaster/" not in markdown
    assert "source /etc/solar-battery-forecaster/" not in markdown
    assert ". ./telemetry.env" not in markdown
    assert "EnvironmentFile=` parser and direct process execution" in markdown


def test_complete_configuration_reference_names_every_example_scalar() -> None:
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    documented_tokens = set(
        re.findall(r"(?<!`)`([^`\n]+)`(?!`)", GUIDE.read_text(encoding="utf-8"))
    )
    scalar_keys: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(item, (dict, list)):
                    scalar_keys.add(str(key))
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(raw)
    missing = {
        key
        for key in scalar_keys
        if not any(
            token == key or token.endswith(f".{key}") or token.endswith(f"[].{key}")
            for token in documented_tokens
        )
    }
    assert not missing
    for requirement in [
        "Complete configuration reference",
        "Units/range",
        "Privacy and authoritative source",
        "Safe example",
        "repeated lists",
        "derived/runtime",
    ]:
        assert requirement in GUIDE.read_text(encoding="utf-8")


def test_maintenance_units_load_environment_without_a_shell() -> None:
    maintenance = ROOT / "deployment" / "maintenance"
    deployment = (ROOT / "deployment" / "LXC.md").read_text(encoding="utf-8")
    expected_commands = {
        "solar-battery-validate@.service": [
            "validate",
            "--scope",
            "%i",
            "--config",
            "/etc/solar-battery-forecaster/config.yaml",
        ],
        "solar-battery-once@.service": [
            "%i",
            "--config",
            "/etc/solar-battery-forecaster/config.yaml",
            "--once",
        ],
        "solar-battery-outage-test@.service": [
            "%i",
            "--config",
            "/etc/solar-battery-forecaster/acceptance-outage.yaml",
            "--once",
        ],
        "solar-battery-outbox-status@.service": [
            "outbox",
            "status",
            "--scope",
            "%i",
            "--config",
            "/etc/solar-battery-forecaster/config.yaml",
        ],
        "solar-battery-outbox-verify@.service": [
            "outbox",
            "verify",
            "--scope",
            "%i",
            "--config",
            "/etc/solar-battery-forecaster/config.yaml",
        ],
        "solar-battery-outbox-drain@.service": [
            "outbox",
            "drain",
            "--scope",
            "%i",
            "--config",
            "/etc/solar-battery-forecaster/config.yaml",
        ],
    }

    assert {path.name for path in maintenance.glob("*.service")} == set(expected_commands)
    assert "deployment/maintenance/*.service /etc/systemd/system/" in deployment
    for name, expected in expected_commands.items():
        unit = configparser.ConfigParser(interpolation=None)
        unit.optionxform = str
        unit.read(maintenance / name, encoding="utf-8")
        service = unit["Service"]
        assert service["Type"] == "oneshot"
        assert service["User"] == "solar-%i"
        assert service["Group"] == "solar-%i"
        assert service["EnvironmentFile"] == "/etc/solar-battery-forecaster/%i.env"
        argv = shlex.split(service["ExecStart"])
        assert argv[0] == (
            "/opt/solar-battery-forecaster/.venv/bin/solar-battery-forecaster"
        )
        assert argv[1:] == expected
        parsed = cli.parser().parse_args(
            ["telemetry" if item == "%i" else item for item in argv[1:]]
        )
        if "outbox" in name:
            action = name.removeprefix("solar-battery-outbox-").removesuffix("@.service")
            assert (parsed.command, parsed.outbox_action, parsed.scope) == (
                "outbox",
                action,
                "telemetry",
            )
        elif "validate" in name:
            assert (parsed.command, parsed.scope) == ("validate", "telemetry")
        else:
            assert parsed.command == "telemetry"
            assert parsed.once is True
        assert all(shell not in service["ExecStart"] for shell in ["sh -c", "bash", "source"])
        assert service["NoNewPrivileges"] == "true"
        assert service["ProtectSystem"] == "strict"
        assert "Install" not in unit


def test_live_acceptance_is_scoped_and_has_exact_expected_results() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    prose = " ".join(guide.split())

    for required in [
        "MemoryCurrent",
        "MemoryMax",
        "NRestarts=0",
        "Stopping/killing each service",
        "http://127.0.0.1:9",
        "does **not** stop, firewall, or reconfigure the shared InfluxDB server",
        "delivered 1 record(s)",
        "increased by exactly one",
        "solar-battery-outbox-status@telemetry.service",
        "all(item[\"stale\"] is False",
        "The command must exit zero and print nothing",
        "does not support mutual TLS (mTLS)",
        "unverified minimum baseline",
        "live-tested/recommended baseline",
    ]:
        assert required in prose
