import configparser
import shlex
from pathlib import Path


def test_five_workers_have_independent_restart_contracts() -> None:
    root = Path(__file__).parents[1]
    unit_paths = sorted((root / "deployment").glob("*.service"))
    assert len(unit_paths) == 5

    commands: set[tuple[str, str]] = set()
    environment_files: set[str] = set()
    for path in unit_paths:
        unit = configparser.ConfigParser(interpolation=None)
        unit.optionxform = str
        unit.read(path, encoding="utf-8")
        assert "Requires" not in unit["Unit"]
        assert "PartOf" not in unit["Unit"]
        assert unit["Service"]["Restart"] == "on-failure"
        environment_files.add(unit["Service"]["EnvironmentFile"])

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
