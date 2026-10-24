from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_open_meteo import property_config

from solar_battery_forecaster.adapters.forecast.open_meteo import geometry_id
from solar_battery_forecaster.adapters.irradiance.open_meteo import MODEL, OpenMeteoSatellite
from solar_battery_forecaster.cli import worker_interval
from solar_battery_forecaster.config import HttpConfig, OutboxConfig
from solar_battery_forecaster.models import (
    ArrayIrradiance,
    ForecastInterval,
    ForecastIrradianceInterval,
    HistoricalIrradianceInterval,
)
from solar_battery_forecaster.observability import StatusRepository
from solar_battery_forecaster.operations import IrradianceHistoryOperation
from solar_battery_forecaster.outbound import RequestPacer
from solar_battery_forecaster.outbox import DurableOutbox
from solar_battery_forecaster.storage import InfluxStore


def pacer():
    return RequestPacer(**HttpConfig(minimum_spacing_seconds=0).model_dump())


def payload(day):
    start = datetime.combine(day, datetime.min.time())
    return {
        "utc_offset_seconds": 0,
        "hourly_units": {"shortwave_radiation": "W/m²", "global_tilted_irradiance": "W/m²"},
        "hourly": {
            "time": [(start + timedelta(hours=i)).isoformat() for i in range(48)],
            "shortwave_radiation": [100.0] * 48,
            "global_tilted_irradiance": [200.0] * 48,
        },
    }


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
async def test_satellite_utc_days_geometry_preceding_hour_and_nulls(day):
    requests = []
    data = payload(day)
    data["hourly"]["shortwave_radiation"][1] = None
    data["hourly"]["global_tilted_irradiance"][1] = 0
    data["hourly"]["global_tilted_irradiance"][24] = 321

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=data)

    prop = property_config()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenMeteoSatellite(client, pacer()).fetch(prop, day)
    assert len(result) == 24
    assert result[0].start == datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    assert result[-1].end == result[0].start + timedelta(days=1)
    assert len(result[0].irradiance) == 2
    assert result[0].irradiance[0].ghi_w_m2 is None
    assert result[0].irradiance[0].gti_w_m2 == 0
    assert result[-1].irradiance[0].gti_w_m2 == 321
    assert result[1].irradiance[0].ghi_w_m2 == 100
    assert result[1].irradiance[0].gti_w_m2 == 200
    assert result[0].source_kind == "satellite_derived"
    assert result[0].model == MODEL
    assert [request.url.params["azimuth"] for request in requests] == ["-90.0", "90.0"]
    assert requests[0].url.host == "satellite-api.open-meteo.com"
    assert requests[0].url.params["end_date"] == (day + timedelta(days=1)).isoformat()
    assert requests[0].url.params["models"] == MODEL
    assert result[0].irradiance[0].geometry != result[0].irradiance[1].geometry
    moved = prop.model_copy(update={"latitude": prop.latitude + 1})
    assert geometry_id(moved, moved.arrays[0]) != result[0].irradiance[0].geometry


@pytest.mark.parametrize("fault", [
    "units", "duplicate", "unaligned", "offset", "count", "length", "bool", "negative", "infinite",
])
async def test_satellite_rejects_invalid_response(fault):
    day = date(2026, 9, 5)
    data = payload(day)
    hourly = data["hourly"]
    if fault == "units":
        data["hourly_units"]["shortwave_radiation"] = "kWh/m²"
    elif fault == "duplicate":
        hourly["time"][2] = hourly["time"][1]
    elif fault == "unaligned":
        hourly["time"][2] = "2026-09-05T02:30"
    elif fault == "offset":
        hourly["time"][2] = "2026-09-05T02:00+01:00"
    elif fault == "count":
        for values in hourly.values():
            values.append(values[-1])
    elif fault == "length":
        hourly["shortwave_radiation"].pop()
    else:
        hourly["global_tilted_irradiance"][1] = {
            "bool": True, "negative": -1, "infinite": "Infinity",
        }[fault]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=data),
    )) as client:
        with pytest.raises(ValueError):
            await OpenMeteoSatellite(client, pacer()).fetch(property_config(), day)


def history(day=date(2026, 9, 5), value=250.0, collected=None):
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return [HistoricalIrradianceInterval(
        start + timedelta(hours=i), start + timedelta(hours=i + 1),
        collected or start + timedelta(days=1), "open_meteo_satellite", MODEL,
        (ArrayIrradiance("roof", 35.0, 180.0, value, value, "geometry-id"),),
    ) for i in range(24)]


def capture_store():
    store = object.__new__(InfluxStore)
    store.config = SimpleNamespace(planning_bucket="planning", org="org")
    batches = []

    def capture(bucket, points, **metadata):
        batches.append(([point.to_line_protocol() for point in points], metadata))
        return "direct"

    store._write = capture
    return store, batches


def test_forecast_frozen_raw_snapshot_same_batch_and_unscaled():
    store, batches = capture_store()
    at = datetime(2026, 9, 5, tzinfo=UTC)
    raw = ArrayIrradiance("roof", 35.0, 180.0, None, 500.0, "geometry-id")
    interval = ForecastInterval(at, at + timedelta(hours=1), 2, 2, at, "open_meteo",
                                irradiance=(raw,))
    with pytest.raises(FrozenInstanceError):
        raw.gti_w_m2 = 0
    store.write_forecast("test-home", [interval], 0.5, 0.8)
    first = batches[0][0]
    assert len(first) == 2
    assert first[0].startswith("pv_forecast,")
    assert first[1].startswith("irradiance_forecast,")
    assert "gti_w_m2=500" in first[1] and "ghi_valid=false" in first[1]
    assert "ghi_w_m2=" not in first[1]
    assert "snapshot=2026-09-05T00:00:00.000000Z" in first[0]
    assert "snapshot=2026-09-05T00:00:00.000000Z" in first[1]
    store.write_forecast("test-home", [replace(interval, issued_at=at + timedelta(hours=1))], 1, 1)
    assert batches[1][0][1].split(" ")[0] != first[1].split(" ")[0]
    assert interval.irradiance == (raw,)
    # Legacy manually constructed intervals remain supported.
    assert ForecastInterval(at, at + timedelta(hours=1), 0, 0, at, "old").irradiance == ()


def test_history_stable_identity_revision_and_null_coverage():
    store, batches = capture_store()
    day = date(2026, 9, 5)
    store.write_irradiance_history("test-home", day, day + timedelta(days=1), history())
    store.write_irradiance_history("test-home", day, day + timedelta(days=2), history(
        value=0.0, collected=datetime(2026, 9, 7, tzinfo=UTC),
    ))
    for old, new in zip(batches[0][0], batches[1][0], strict=True):
        assert old.split(" ")[0] == new.split(" ")[0]
        assert old.rsplit(" ", 1)[1] == new.rsplit(" ", 1)[1]
    assert "value_w_m2=0" in batches[1][0][0]
    assert "source_kind=satellite_derived" in batches[1][0][0]
    assert "collected_at=" not in batches[1][0][0].split(" ")[0]
    store.write_irradiance_history("test-home", day, day + timedelta(days=3), history(value=None))
    assert len(batches[2][0]) == 2
    assert all(line.startswith("irradiance_history_collection,") for line in batches[2][0])
    assert all("valid_hours=0i" in line and "complete=false" in line for line in batches[2][0])
    assert all("pv_forecast" not in line for batch, _ in batches for line in batch)


def test_history_failure_pending_restart_and_exact_replay(tmp_path):
    settings = OutboxConfig(state_directory=tmp_path / "state")
    store = object.__new__(InfluxStore)
    store.config = SimpleNamespace(planning_bucket="planning", org="org")
    store.outbox = DurableOutbox(settings.state_directory, settings, "irradiance-history")
    attempted = []

    def fail(bucket, org, payload):
        attempted.append(payload)
        raise TimeoutError

    store._deliver = fail
    rows = history()
    day = rows[0].start.date()
    key = store.irradiance_history_key(
        "test-home", day, rows[0].provider, rows[0].model, (("roof", "geometry-id"),),
    )
    outcome = store.write_irradiance_history("test-home", day, day + timedelta(days=1), rows)
    assert outcome == "buffered"
    store._records = lambda query: pytest.fail("pending marker must avoid offline query")
    assert store.irradiance_history_collected(key, day, day + timedelta(days=2))
    store.outbox.close()
    store.outbox = DurableOutbox(settings.state_directory, settings, "irradiance-history")
    assert store.irradiance_history_collected(key, day, day + timedelta(days=2))
    delivered = []
    store._deliver = lambda bucket, org, payload: delivered.append(payload)
    assert store.replay(force=True) == 1
    assert delivered == attempted
    assert store.outbox.status().pending_records == 0
    store.outbox.close()


async def test_worker_daily_refresh_dedup_bounded_days_and_disabled():
    prop = property_config()
    prop.irradiance_history.enabled = True
    prop.irradiance_history.catch_up_days = 2
    disabled = property_config().model_copy(update={"id": "disabled"})
    operation = object.__new__(IrradianceHistoryOperation)
    operation.reporter = None
    operation.config = SimpleNamespace(properties=[prop, disabled],
                                       schedule=SimpleNamespace(property_phase_seconds=0))
    seen, calls = set(), []
    operation.adapters = {prop.id: SimpleNamespace(name="satellite", model=MODEL)}
    operation.store = SimpleNamespace(
        replay=lambda: 0, irradiance_history_key=InfluxStore.irradiance_history_key,
        irradiance_history_collected=lambda key, day, refresh: (key, refresh) in seen,
    )

    async def collect(prop, day, refresh_day):
        calls.append(day)
        key = InfluxStore.irradiance_history_key(
            prop.id, day, "satellite", MODEL,
            tuple((array.name, geometry_id(prop, array)) for array in prop.arrays),
        )
        seen.add((key, refresh_day))
        return True

    operation.run_property_safely = collect
    at = datetime(2026, 10, 26, tzinfo=UTC)
    assert await operation.run_cycle(at)
    assert await operation.run_cycle(at + timedelta(hours=1))
    assert calls == [date(2026, 10, 24), date(2026, 10, 25)]
    assert await operation.run_cycle(at + timedelta(days=1))
    assert calls[-2:] == [date(2026, 10, 25), date(2026, 10, 26)]
    assert worker_interval(operation.config, "irradiance-history") == 3600


def test_optional_history_health_is_required_only_when_enabled(tmp_path):
    disabled = StatusRepository(tmp_path / "dashboard", 90).read()
    enabled = StatusRepository(tmp_path / "dashboard", 90, irradiance_history_enabled=True).read()
    assert "irradiance-history" not in {item["service"] for item in disabled["services"]}
    optional = [item for item in enabled["services"] if item["service"] == "irradiance-history"]
    assert len(optional) == 1 and optional[0]["lifecycle"] == "missing"


async def test_missing_hours_remain_missing_and_do_not_invent_zero():
    day = date(2026, 9, 5)
    data = payload(day)
    for values in data["hourly"].values():
        del values[1]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=data),
    )) as client:
        rows = await OpenMeteoSatellite(client, pacer()).fetch(property_config(), day)
    assert rows[0].irradiance[0].ghi_w_m2 is None
    assert rows[0].irradiance[0].gti_w_m2 is None
    assert rows[1].irradiance[0].gti_w_m2 == 200


def test_history_due_read_failure_is_not_absence_and_geometry_changes_identity():
    store = object.__new__(InfluxStore)
    store.config = SimpleNamespace(planning_bucket="planning")
    day = date(2026, 9, 5)
    key = store.irradiance_history_key("test-home", day, "provider", MODEL, (("roof", "one"),))
    other = store.irradiance_history_key("test-home", day, "provider", MODEL, (("roof", "two"),))
    assert key != other

    def fail(query):
        raise ConnectionError

    store._records = fail
    with pytest.raises(ConnectionError):
        store.irradiance_history_collected(key, day, day + timedelta(days=1))


def test_forecast_rejects_stale_raw_attachment_and_honors_empty_raw_transport():
    store, batches = capture_store()
    at = datetime(2026, 9, 5, tzinfo=UTC)
    sample = ArrayIrradiance("roof", 35, 180, 123, 200, "geometry")
    pv = ForecastInterval(at, at + timedelta(hours=1), 1, 1, at, "test", irradiance=(sample,))
    raw = ForecastIrradianceInterval(at, pv.end, at - timedelta(hours=1), "test", (sample,))
    with pytest.raises(ValueError, match="snapshot"):
        store.write_forecast("test-home", [pv], 1, 1, irradiance_intervals=(raw,))
    assert not batches
    store.write_forecast("test-home", [pv], 1, 1, irradiance_intervals=())
    assert len(batches[0][0]) == 1
    assert batches[0][0][0].startswith("pv_forecast,")
