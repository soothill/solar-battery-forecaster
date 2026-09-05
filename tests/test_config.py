from pathlib import Path

import pytest
from pydantic import ValidationError

from solar_battery_forecaster.config import HttpConfig, OutboxConfig, load_config


def test_missing_environment_variable_fails(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
influxdb:
  url: http://localhost:8086
  org: test
  token: ${NOT_SET_FOR_TEST}
properties: []
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="NOT_SET_FOR_TEST"):
        load_config(path)


SCOPED_CONFIG = """
influxdb:
  url: https://influx.invalid
  org: test
  telemetry_bucket: solar_telemetry
  tariff_bucket: solar_tariff
  planning_bucket: solar_planning
  tokens:
    telemetry: ${INFLUX_TELEMETRY_TOKEN}
    tariff: ${INFLUX_TARIFF_TOKEN}
    forecast-plan: ${INFLUX_FORECAST_PLAN_TOKEN}
    reconciliation: ${INFLUX_RECONCILIATION_TOKEN}
    dashboard: ${INFLUX_DASHBOARD_TOKEN}
outbox:
  state_directory: ${OUTBOX_STATE_DIRECTORY}
properties:
  - id: test-home
    timezone: Europe/London
    latitude: 51.5
    longitude: -0.1
    arrays:
      - name: roof
        panel_count: 10
        panel_power_w: 400
        tilt_degrees: 35
        azimuth_degrees: 180
    inverter:
      rated_power_kw: 6
      app_key: ${SIGENERGY_APP_KEY}
      app_secret: ${SIGENERGY_APP_SECRET}
      system_id: ${SIGENERGY_SYSTEM_ID}
    battery:
      usable_capacity_kwh: 9
      max_charge_power_kw: 6
    tariff:
      product_code: PRODUCT
      tariff_code: TARIFF
      api_key: ${UNRELATED_TARIFF_SECRET}
    forecast:
      api_key: ${UNRELATED_FORECAST_SECRET}
"""


@pytest.mark.parametrize(
    ("scope", "token_name"),
    [
        ("tariff", "INFLUX_TARIFF_TOKEN"),
        ("forecast-plan", "INFLUX_FORECAST_PLAN_TOKEN"),
        ("reconciliation", "INFLUX_RECONCILIATION_TOKEN"),
        ("dashboard", "INFLUX_DASHBOARD_TOKEN"),
    ],
)
def test_non_telemetry_scopes_do_not_require_inverter_or_other_provider_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, token_name: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(SCOPED_CONFIG, encoding="utf-8")
    monkeypatch.setenv(token_name, "scoped-token")
    monkeypatch.setenv("OUTBOX_STATE_DIRECTORY", f"/var/lib/solar-battery-{scope}")

    config = load_config(path, scope=scope)

    assert config.influxdb.token == "scoped-token"
    assert config.properties[0].inverter.app_key is None
    assert config.properties[0].inverter.system_id is None


def test_telemetry_scope_resolves_only_telemetry_and_inverter_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(SCOPED_CONFIG, encoding="utf-8")
    monkeypatch.setenv("INFLUX_TELEMETRY_TOKEN", "telemetry-token")
    monkeypatch.setenv("SIGENERGY_APP_KEY", "key")
    monkeypatch.setenv("SIGENERGY_APP_SECRET", "secret")
    monkeypatch.setenv("SIGENERGY_SYSTEM_ID", "system")
    monkeypatch.setenv("OUTBOX_STATE_DIRECTORY", "/var/lib/solar-battery-telemetry")

    config = load_config(path, scope="telemetry")

    assert config.influxdb.token == "telemetry-token"
    assert config.properties[0].inverter.app_key == "key"
    assert config.properties[0].inverter.system_id == "system"


def test_scoped_config_rejects_legacy_single_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        SCOPED_CONFIG.replace(
            "  telemetry_bucket: solar_telemetry\n"
            "  tariff_bucket: solar_tariff\n"
            "  planning_bucket: solar_planning\n",
            "  bucket: legacy_combined\n",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("INFLUX_DASHBOARD_TOKEN", "dashboard-token")

    with pytest.raises(ValueError, match="telemetry_bucket"):
        load_config(path, scope="dashboard")


@pytest.mark.parametrize(
    ("field", "duplicate"),
    [
        ("tariff_bucket", "solar_telemetry"),
        ("planning_bucket", "solar_telemetry"),
        ("planning_bucket", "solar_tariff"),
    ],
)
def test_scoped_config_rejects_every_duplicate_bucket_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    duplicate: str,
) -> None:
    path = tmp_path / "config.yaml"
    original = {
        "tariff_bucket": "solar_tariff",
        "planning_bucket": "solar_planning",
    }[field]
    path.write_text(
        SCOPED_CONFIG.replace(f"  {field}: {original}", f"  {field}: {duplicate}"),
        encoding="utf-8",
    )
    monkeypatch.setenv("INFLUX_DASHBOARD_TOKEN", "dashboard-token")

    with pytest.raises(ValueError, match="pairwise distinct"):
        load_config(path, scope="dashboard")


@pytest.mark.parametrize("size", [16_383, 8_388_609])
def test_http_response_limit_rejects_unsafe_bounds(size: int) -> None:
    with pytest.raises(ValidationError, match="max_response_bytes"):
        HttpConfig(max_response_bytes=size)


def test_outbox_state_directory_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="absolute non-root"):
        OutboxConfig(state_directory=Path("relative"))


def test_dashboard_scope_does_not_resolve_writer_outbox_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(SCOPED_CONFIG, encoding="utf-8")
    monkeypatch.setenv("INFLUX_DASHBOARD_TOKEN", "dashboard-token")
    loaded = load_config(path, scope="dashboard")
    assert loaded.outbox is None


def test_writer_scope_resolves_private_outbox_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(SCOPED_CONFIG, encoding="utf-8")
    monkeypatch.setenv("INFLUX_TARIFF_TOKEN", "tariff-token")
    monkeypatch.setenv("OUTBOX_STATE_DIRECTORY", "/var/lib/solar-battery-tariff")
    loaded = load_config(path, scope="tariff")
    assert loaded.outbox is not None
    assert loaded.outbox.state_directory == Path("/var/lib/solar-battery-tariff")


def test_writer_scope_requires_outbox_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        SCOPED_CONFIG.replace("outbox:\n  state_directory: ${OUTBOX_STATE_DIRECTORY}\n", ""),
        encoding="utf-8",
    )
    monkeypatch.setenv("INFLUX_RECONCILIATION_TOKEN", "reconciliation-token")

    with pytest.raises(ValueError, match="requires an outbox state_directory"):
        load_config(path, scope="reconciliation")
