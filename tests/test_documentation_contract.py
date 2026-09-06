import configparser
import os
import re
import shlex
import subprocess
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

    bash_blocks = re.findall(r"```bash\n(.*?)\n```", guide, flags=re.DOTALL)
    assert bash_blocks
    assert all(block.splitlines()[0] == "set -euo pipefail" for block in bash_blocks)


def test_bash_acceptance_prologue_fails_fast_and_propagates_pipeline_failure(
    tmp_path: Path,
) -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    block = re.search(r"```bash\n(.*?)\n```", guide, flags=re.DOTALL)
    assert block is not None
    prologue = block.group(1).splitlines()[0]

    after_command = tmp_path / "after-command"
    command_result = subprocess.run(
        [
            "bash",
            "-c",
            f"{prologue}\nfalse\nprintf reached > {shlex.quote(str(after_command))}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert command_result.returncode != 0
    assert not after_command.exists()

    after_pipeline = tmp_path / "after-pipeline"
    pipeline_result = subprocess.run(
        [
            "bash",
            "-c",
            f"{prologue}\nprintf ok | grep '^missing$' | tail -n 1\n"
            f"printf reached > {shlex.quote(str(after_pipeline))}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert pipeline_result.returncode != 0
    assert not after_pipeline.exists()


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
        "systemctl kill --kill-whom=main --signal=SIGKILL",
        "for attempt in {1..50}",
        'test "$after" -gt "$before"',
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
        "permission checks themselves are silent",
        "one sanitized status JSON object per writer",
    ]:
        assert required in prose


def test_outage_acceptance_selects_exactly_one_property_without_mutating_main_config() -> None:
    guide = GUIDE.read_text(encoding="utf-8")

    for required in [
        "property_alias=home-01",
        'config = yaml.safe_load(source.read_text(encoding="utf-8"))',
        'if len(matches) != 1:',
        'config["properties"] = matches',
        'config["influxdb"]["url"] = "http://127.0.0.1:9"',
        "os.O_EXCL | os.O_NOFOLLOW",
        "os.fchown(descriptor, 0, grp.getgrnam(\"solar-config\").gr_gid)",
        "os.fchmod(descriptor, 0o640)",
        "expected_destination = Path(\"/etc/solar-battery-forecaster/acceptance-outage.yaml\")",
        "pending records increased from zero to one",
        "increased by exactly one",
    ]:
        assert required in guide
    assert "cp --preserve=mode,ownership /etc/solar-battery-forecaster/config.yaml" not in guide
    assert "sed -i" not in guide


def test_outage_cleanup_is_armed_before_generation_and_preserves_existing_residue(
    tmp_path: Path,
) -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    outage_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)\n```", guide, flags=re.DOTALL)
        if "solar-battery-outage-test@telemetry.service" in block
    )
    generator = outage_block.index(
        "/opt/solar-battery-forecaster/.venv/bin/python"
    )
    preflight = outage_block.index('if [[ -e "$acceptance_config"')
    trap = outage_block.index("trap 'finish_acceptance $?' EXIT")
    assert preflight < trap < generator
    for handler in [
        "trap 'finish_acceptance 129' HUP",
        "trap 'finish_acceptance 130' INT",
        "trap 'finish_acceptance 143' TERM",
    ]:
        assert handler in outage_block

    documented_path = "/etc/solar-battery-forecaster/acceptance-outage.yaml"
    prefix = outage_block.split("property_alias=home-01", 1)[0]
    acceptance_path = tmp_path / "acceptance-outage.yaml"
    replacement = f"acceptance_config={shlex.quote(str(acceptance_path))}"
    prefix = prefix.replace(f"acceptance_config={documented_path}", replacement)
    assert documented_path not in prefix

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text(
        """#!/bin/bash
set -euo pipefail
command=$1
shift
case "$command" in
  is-active) cat "$FAKE_STATE_FILE"; test "$(cat "$FAKE_STATE_FILE")" = active ;;
  start) test "$1" != solar-battery-telemetry.service || printf active > "$FAKE_STATE_FILE" ;;
  reset-failed) : ;;
  *) exit 64 ;;
esac
""",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    state_file = tmp_path / "state"
    state_file.write_text("active", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "FAKE_STATE_FILE": str(state_file),
        }
    )

    generator_failure = subprocess.run(
        [
            "bash",
            "-c",
            f"{prefix}\npython3 - \"$acceptance_config\" <<'PY'\n"
            "from pathlib import Path\n"
            "import sys\n"
            'Path(sys.argv[1]).write_text("generated", encoding="utf-8")\n'
            "raise SystemExit(23)\n"
            "PY",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert generator_failure.returncode == 23
    assert not acceptance_path.exists()

    acceptance_path.write_text("unexplained residue", encoding="utf-8")
    after_preflight = tmp_path / "after-preflight"
    residue_result = subprocess.run(
        [
            "bash",
            "-c",
            f"{prefix}\nprintf reached > {shlex.quote(str(after_preflight))}",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert residue_result.returncode != 0
    assert acceptance_path.read_text(encoding="utf-8") == "unexplained residue"
    assert not after_preflight.exists()


def test_outage_scope_restores_only_an_originally_active_telemetry_service(
    tmp_path: Path,
) -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    outage_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)\n```", guide, flags=re.DOTALL)
        if "solar-battery-outage-test@telemetry.service" in block
    )
    documented_path = "/etc/solar-battery-forecaster/acceptance-outage.yaml"
    prefix = outage_block.split("property_alias=home-01", 1)[0]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text(
        """#!/bin/bash
set -euo pipefail
command=$1
shift
printf '%s %s\n' "$command" "$*" >> "$FAKE_LOG_FILE"
case "$command" in
  is-active)
    cat "$FAKE_STATE_FILE"
    test "$(cat "$FAKE_STATE_FILE")" = active
    ;;
  stop)
    test "$1" != solar-battery-telemetry.service || printf inactive > "$FAKE_STATE_FILE"
    ;;
  start)
    if [[ "$1" == solar-battery-outbox-drain@telemetry.service \
          && "${FAKE_FAIL_DRAIN:-0}" == 1 ]]; then
      exit 42
    fi
    if [[ "$1" == solar-battery-telemetry.service ]]; then
      if [[ "${FAKE_FAIL_RESTORE:-0}" == 1 ]]; then
        exit 55
      fi
      printf active > "$FAKE_STATE_FILE"
    fi
    ;;
  reset-failed) : ;;
  *) exit 64 ;;
esac
""",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)

    def run_failure(
        name: str, initial_state: str, body: str, **extra_environment: str
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
        scenario = tmp_path / name
        scenario.mkdir()
        acceptance_path = scenario / "acceptance-outage.yaml"
        state_file = scenario / "state"
        log_file = scenario / "systemctl.log"
        state_file.write_text(initial_state, encoding="utf-8")
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "FAKE_STATE_FILE": str(state_file),
                "FAKE_LOG_FILE": str(log_file),
                **extra_environment,
            }
        )
        scenario_prefix = prefix.replace(
            f"acceptance_config={documented_path}",
            f"acceptance_config={shlex.quote(str(acceptance_path))}",
        )
        result = subprocess.run(
            ["bash", "-c", f'{scenario_prefix}\ntouch "$acceptance_config"\n{body}'],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        return result, acceptance_path, state_file, log_file

    successful_run, config, state, _ = run_failure(
        "success",
        "active",
        'systemctl stop "$telemetry_service"\ntrue',
    )
    assert successful_run.returncode == 0
    assert not config.exists()
    assert state.read_text(encoding="utf-8") == "active"

    after_stop, config, state, log = run_failure(
        "after-stop",
        "active",
        'systemctl stop "$telemetry_service"\nfalse',
    )
    assert after_stop.returncode == 1
    assert not config.exists()
    assert state.read_text(encoding="utf-8") == "active"
    assert "start solar-battery-telemetry.service" in log.read_text(encoding="utf-8")

    during_replay, config, state, log = run_failure(
        "during-replay",
        "active",
        'systemctl stop "$telemetry_service"\n'
        "systemctl start solar-battery-outbox-drain@telemetry.service",
        FAKE_FAIL_DRAIN="1",
    )
    assert during_replay.returncode == 42
    assert not config.exists()
    assert state.read_text(encoding="utf-8") == "active"
    assert "start solar-battery-telemetry.service" in log.read_text(encoding="utf-8")

    initially_inactive, config, state, log = run_failure(
        "initially-inactive",
        "inactive",
        'systemctl stop "$telemetry_service"\nfalse',
    )
    assert initially_inactive.returncode == 1
    assert not config.exists()
    assert state.read_text(encoding="utf-8") == "inactive"
    assert "start solar-battery-telemetry.service" not in log.read_text(encoding="utf-8")

    restoration_failure, config, state, _ = run_failure(
        "restoration-failure",
        "active",
        'systemctl stop "$telemetry_service"\nfalse',
        FAKE_FAIL_RESTORE="1",
    )
    assert restoration_failure.returncode == 125
    assert not config.exists()
    assert state.read_text(encoding="utf-8") == "inactive"
