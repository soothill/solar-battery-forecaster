import configparser
import re
import shlex
from pathlib import Path


def test_five_workers_have_independent_restart_contracts() -> None:
    root = Path(__file__).parents[1]
    unit_paths = sorted((root / "deployment").glob("*.service"))
    assert len(unit_paths) == 5
    identity_by_environment = {
        "telemetry.env": "solar-telemetry",
        "tariff.env": "solar-tariff",
        "forecast-plan.env": "solar-forecast-plan",
        "reconciliation.env": "solar-reconciliation",
        "dashboard.env": "solar-dashboard",
    }

    commands: set[tuple[str, str]] = set()
    environment_files: set[str] = set()
    identities: set[tuple[str, str]] = set()
    memory_limits: dict[str, str] = {}
    for path in unit_paths:
        unit = configparser.ConfigParser(interpolation=None)
        unit.optionxform = str
        unit.read(path, encoding="utf-8")
        assert "Requires" not in unit["Unit"]
        assert "PartOf" not in unit["Unit"]
        assert unit["Service"]["Restart"] == "on-failure"
        environment_file = unit["Service"]["EnvironmentFile"]
        environment_files.add(environment_file)
        user = unit["Service"]["User"]
        group = unit["Service"]["Group"]
        assert user == group
        assert user == identity_by_environment[Path(environment_file).name]
        expected_groups = (
            "solar-config solar-observe" if user == "solar-dashboard" else "solar-config"
        )
        assert unit["Service"]["SupplementaryGroups"] == expected_groups
        identities.add((user, group))
        memory_limits[user] = unit["Service"]["MemoryMax"]
        if user == "solar-dashboard":
            assert "StateDirectory" not in unit["Service"]
            assert unit["Service"]["UMask"] == "0027"
            assert unit["Service"]["ReadOnlyPaths"] == "/run/solar-battery-status"
        else:
            assert unit["Service"]["StateDirectory"] == path.stem
            assert unit["Service"]["StateDirectoryMode"] == "0700"
            assert unit["Service"]["UMask"] == "0077"
        scope = Path(environment_file).stem
        assert unit["Service"]["ReadWritePaths"] == f"/run/solar-battery-status/{scope}"

        argv = shlex.split(unit["Service"]["ExecStart"])
        program = Path(argv[0]).name
        command = argv[1] if program == "solar-battery-forecaster" else "dashboard"
        commands.add((program, command))

    assert commands == {
        ("solar-battery-forecaster", "telemetry"),
        ("solar-battery-forecaster", "tariff"),
        ("solar-battery-forecaster", "forecast-plan"),
        ("solar-battery-forecaster", "reconciliation"),
        ("solar-battery-dashboard", "dashboard"),
    }
    assert environment_files == {
        "/etc/solar-battery-forecaster/telemetry.env",
        "/etc/solar-battery-forecaster/tariff.env",
        "/etc/solar-battery-forecaster/forecast-plan.env",
        "/etc/solar-battery-forecaster/reconciliation.env",
        "/etc/solar-battery-forecaster/dashboard.env",
    }
    assert identities == {
        ("solar-telemetry", "solar-telemetry"),
        ("solar-tariff", "solar-tariff"),
        ("solar-forecast-plan", "solar-forecast-plan"),
        ("solar-reconciliation", "solar-reconciliation"),
        ("solar-dashboard", "solar-dashboard"),
    }
    assert memory_limits == {
        "solar-telemetry": "80M",
        "solar-tariff": "80M",
        "solar-forecast-plan": "80M",
        "solar-reconciliation": "80M",
        "solar-dashboard": "96M",
    }


def test_deployment_assigns_each_secret_file_to_only_its_service_group() -> None:
    deployment = (Path(__file__).parents[1] / "deployment" / "LXC.md").read_text(encoding="utf-8")
    owners = {
        "telemetry.env": "solar-telemetry",
        "tariff.env": "solar-tariff",
        "forecast-plan.env": "solar-forecast-plan",
        "reconciliation.env": "solar-reconciliation",
        "dashboard.env": "solar-dashboard",
    }
    assert "groupadd --system solar-config" in deployment
    assert "chown root:solar-config /etc/solar-battery-forecaster/config.yaml" in deployment
    assert "chmod 0640 /etc/solar-battery-forecaster/config.yaml" in deployment
    for environment_file, group in owners.items():
        scope = environment_file.removesuffix(".env")
        instruction = re.compile(
            rf"install -o root -g {re.escape(group)} -m 0640 \\\n"
            rf"\s+.*/{re.escape(scope)}\.env\.example \\\n"
            rf"\s+/etc/solar-battery-forecaster/{re.escape(environment_file)}"
        )
        assert instruction.search(deployment)


def test_writer_environment_files_bind_private_outbox_directories() -> None:
    root = Path(__file__).parents[1]
    for worker in ["telemetry", "tariff", "forecast-plan", "reconciliation"]:
        environment = (root / "deployment" / "environment" / f"{worker}.env.example").read_text(
            encoding="utf-8"
        )
        assert f"OUTBOX_STATE_DIRECTORY=/var/lib/solar-battery-{worker}" in environment
        assert f"STATUS_DIRECTORY=/run/solar-battery-status/{worker}" in environment
    dashboard = (root / "deployment" / "environment" / "dashboard.env.example").read_text(
        encoding="utf-8"
    )
    assert "OUTBOX_STATE_DIRECTORY" not in dashboard
    assert "STATUS_DIRECTORY=/run/solar-battery-status/dashboard" in dashboard


def test_status_runtime_directories_have_one_way_dashboard_read_access() -> None:
    root = Path(__file__).parents[1]
    deployment = (root / "deployment" / "LXC.md").read_text(encoding="utf-8")
    tmpfiles = (root / "deployment" / "solar-battery-status.tmpfiles").read_text(encoding="utf-8")
    assert "groupadd --system solar-observe" in deployment
    assert "--groups solar-config,solar-observe solar-dashboard" in deployment
    assert (
        "--groups solar-config,solar-observe"
        not in deployment.split("groupadd --system solar-dashboard")[0]
    )
    for service in [
        "telemetry",
        "tariff",
        "forecast-plan",
        "reconciliation",
        "dashboard",
    ]:
        assert f"/run/solar-battery-status/{service} 2750 solar-{service} solar-observe" in tmpfiles


def test_mobile_status_ui_exposes_delivery_and_syslog_health_without_html_injection() -> None:
    app = (
        Path(__file__).parents[1]
        / "src"
        / "solar_battery_forecaster"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")
    for field in [
        "last_cycle_result",
        "last_direct_delivery_at",
        "last_buffered_at",
        "pending_bytes",
        "oldest_pending_age_seconds",
        "blocked_streams",
        "quarantined_records",
        "syslog.dropped",
        "syslog.last_failure_at",
    ]:
        assert field in app
    assert "textContent" in app
    assert "innerHTML" not in app
