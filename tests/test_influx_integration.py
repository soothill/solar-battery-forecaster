"""Opt-in real InfluxDB acceptance; never point this at an existing installation.

Requires an UNINITIALIZED disposable InfluxDB 2.x on loopback. The fixture creates
only synthetic data and refuses an already configured server before any mutation.
"""

import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

from solar_battery_forecaster.config import InfluxConfig, OutboxConfig, PropertyConfig
from solar_battery_forecaster.dashboard import InfluxDashboardRepository
from solar_battery_forecaster.models import (
    ForecastInterval,
    TariffInterval,
    Telemetry,
    forecast_snapshot_id,
)
from solar_battery_forecaster.planner import make_interval_decision
from solar_battery_forecaster.storage import InfluxStore


@pytest.fixture
def real_store(tmp_path):
    endpoint = os.environ.get("SOLAR_TEST_DISPOSABLE_INFLUX_URL")
    if not endpoint:
        pytest.skip("requires explicit disposable InfluxDB endpoint")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        pytest.fail("disposable InfluxDB must be a bare loopback HTTP endpoint")
    token = secrets.token_urlsafe(48)
    org = "solar-synthetic-acceptance"
    with httpx.Client(base_url=endpoint, timeout=10, trust_env=False) as client:
        response = client.get("/api/v2/setup")
        response.raise_for_status()
        if response.json().get("allowed") is not True:
            pytest.fail("refusing to modify an already initialized InfluxDB")
        setup = client.post(
            "/api/v2/setup",
            json={
                "username": "synthetic-test",
                "password": secrets.token_urlsafe(32),
                "org": org,
                "bucket": "synthetic-telemetry",
                "token": token,
            },
        )
        setup.raise_for_status()
        org_id = setup.json()["org"]["id"]
        for bucket in ("synthetic-tariff", "synthetic-planning"):
            response = client.post(
                "/api/v2/buckets",
                headers={"Authorization": f"Token {token}"},
                json={"orgID": org_id, "name": bucket},
            )
            response.raise_for_status()
    config = InfluxConfig(
        url=endpoint,
        allow_insecure_http=True,
        token=token,
        org=org,
        telemetry_bucket="synthetic-telemetry",
        tariff_bucket="synthetic-tariff",
        planning_bucket="synthetic-planning",
    )
    store = InfluxStore(config, OutboxConfig(state_directory=tmp_path), "telemetry")
    try:
        yield store
    finally:
        store.close()


def test_real_influx_direct_failure_restart_replay_and_idempotency(real_store, tmp_path):
    store = real_store
    observed = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)

    def reading(at):
        return Telemetry(
            observed_at=at,
            pv_power_kw=1.25,
            battery_soc_percent=55,
            battery_power_kw=0,
            load_power_kw=0.75,
            grid_power_kw=-0.5,
            daily_pv_kwh=3,
            lifetime_pv_kwh=100,
        )

    first = reading(observed)
    second = reading(observed + timedelta(minutes=5))
    assert store.write_telemetry("synthetic-home", "fixture", first) == "direct"
    assert store.outbox.status().pending_records == 0
    # Real connection refusal, not a mocked successful write or a shared DB outage.
    refused = InfluxDBClient(url="http://127.0.0.1:9", token="synthetic", timeout=500, retries=0)
    original_writer = store.writer
    try:
        store.writer = refused.write_api(write_options=SYNCHRONOUS)
        assert store.write_telemetry("synthetic-home", "fixture", second) == "buffered"
        assert store.outbox.status().pending_records == 1
    finally:
        store.writer = original_writer
        refused.close()
    # Restart demonstrates durable payload recovery without process memory.
    config = store.config
    store.close()
    store.outbox = None  # Already closed; fixture teardown must not checkpoint it twice.
    recovered = InfluxStore(config, OutboxConfig(state_directory=tmp_path), "telemetry")
    try:
        assert recovered.outbox.status().pending_records == 1
        assert recovered.replay(force=True) == 1
        assert recovered.outbox.status().pending_records == 0
        assert recovered.write_telemetry("synthetic-home", "fixture", second) == "direct"
        query = """from(bucket: "synthetic-telemetry")
            |> range(start: -1h)
            |> filter(fn: (r) => r._measurement == "energy_telemetry"
                and r.property == "synthetic-home" and r._field == "pv_power_kw")"""
        deadline = time.monotonic() + 10
        rows = []
        while time.monotonic() < deadline:
            rows = recovered._records(query)
            if len(rows) == 2:
                break
            time.sleep(0.1)
        assert len(rows) == 2
        assert sorted(row.get_value() for row in rows) == [1.25, 1.25]
        assert sorted(row.get_time() for row in rows) == [first.observed_at, second.observed_at]
        _assert_real_plan_roundtrip(recovered)
    finally:
        recovered.close()


def _assert_real_plan_roundtrip(store):
    """Exercise real mixed-field Flux pivots, exact snapshot selection and plan joins."""
    prop = PropertyConfig.model_validate({
        "id": "synthetic-home", "timezone": "UTC", "latitude": 0, "longitude": 0,
        "arrays": [{"name": "roof", "panel_count": 10, "panel_power_w": 400,
                    "tilt_degrees": 30, "azimuth_degrees": 180}],
        "inverter": {"rated_power_kw": 6},
        "battery": {"usable_capacity_kwh": 9, "max_charge_power_kw": 6},
        "tariff": {"product_code": "synthetic", "tariff_code": "synthetic"},
    })
    day_start = datetime(2026, 9, 7, tzinfo=UTC)
    stop = day_start + timedelta(days=1)
    observation = day_start - timedelta(hours=3)
    forecasts = [
        ForecastInterval(
            start=observation + timedelta(hours=i),
            end=observation + timedelta(hours=i + 1),
            energy_kwh=1.0 if 11 <= i <= 18 else 0.0,
            power_kw=1.0 if 11 <= i <= 18 else 0.0,
            issued_at=observation, provider="open_meteo",
        ) for i in range(27)
    ]
    tariffs = [
        TariffInterval(observation, day_start + timedelta(hours=7), 8.0, True),
        TariffInterval(day_start + timedelta(hours=7), stop, 30.0, False),
    ]
    decision = make_interval_decision(
        battery=prop.battery, current_soc_percent=30, forecast_intervals=forecasts,
        tariff_intervals=tariffs, expected_load_kwh=8, factor=1,
        conservative_multiplier=0.8, inverter_limit_kw=6, forecast_day=day_start.date(),
        forecast_snapshot_id=forecast_snapshot_id(observation), forecast_issued_at=observation,
        soc_observed_at=observation, day_start=day_start, day_stop=stop, created_at=observation,
    )
    assert store.write_forecast(
        prop.id, forecasts, 1, 0.8, forecast_day_start=day_start, forecast_day_stop=stop,
        inverter_limit_kw=6,
    ) == "direct"
    assert store.write_decision(prop.id, decision) == "direct"
    repository = InfluxDashboardRepository(store.config)
    try:
        curve = repository.curve(prop, day_start.date())
        assert curve["plan"]["available"] is True
        assert curve["summary"]["target_soc_percent"] == pytest.approx(decision.target_soc_percent)
        points = curve["series"]["planned_soc_percent"]
        assert len(points) == len(decision.plan_points)
        assert points[0]["at"] == observation.isoformat()
        assert points[-1]["at"] == stop.isoformat()
    finally:
        repository.close()
