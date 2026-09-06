from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from solar_battery_forecaster.config import InfluxConfig
from solar_battery_forecaster.dashboard import (
    InfluxDashboardRepository,
    local_day_bounds,
)


def test_local_day_bounds_handle_british_summer_time() -> None:
    start, stop = local_day_bounds(date(2026, 6, 1), "Europe/London")
    assert start == datetime(2026, 5, 31, 23, tzinfo=UTC)
    assert stop == datetime(2026, 6, 1, 23, tzinfo=UTC)


def test_local_day_bounds_handle_dst_transition() -> None:
    start, stop = local_day_bounds(date(2026, 10, 25), "Europe/London")
    assert (stop - start).total_seconds() == 25 * 60 * 60


def test_repository_rejects_property_id_before_building_query() -> None:
    repository = InfluxDashboardRepository(
        InfluxConfig(
            url="https://influx.invalid",
            org="test",
            telemetry_bucket="telemetry",
            tariff_bucket="tariff",
            planning_bucket="planning",
            token="secret",
        ),
        client=object(),
    )
    with pytest.raises(ValueError, match="invalid property ID"):
        repository._points(  # noqa: SLF001 - verifies the query boundary
            "energy_telemetry",
            "pv_power_kw",
            '../x") |> yield(name: "injected")',
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
        )


def test_dashboard_selects_newest_complete_forecast_snapshot() -> None:
    start = datetime(2026, 9, 6, tzinfo=UTC)
    old_issued = start - timedelta(hours=2)
    new_issued = start - timedelta(hours=1)

    class Record:
        def __init__(self, at: datetime, snapshot: str, issued: datetime, value: float) -> None:
            self.values = {
                "snapshot": snapshot,
                "issued_at_epoch": issued.timestamp(),
                "conservative_energy_kwh": value,
                "conservative_power_kw": value,
            }
            self.at = at

        def get_time(self) -> datetime:
            return self.at

    records = [
        Record(start, "z-old", old_issued, 1),
        Record(start + timedelta(hours=1), "z-old", old_issued, 2),
        Record(start, "a-new", new_issued, 3),
        Record(start + timedelta(hours=1), "a-new", new_issued, 4),
    ]
    query_api = SimpleNamespace(query=lambda **kwargs: [SimpleNamespace(records=records)])
    client = SimpleNamespace(query_api=lambda: query_api, close=lambda: None)
    repository = InfluxDashboardRepository(
        InfluxConfig(
            url="https://influx.invalid",
            org="test",
            telemetry_bucket="telemetry",
            tariff_bucket="tariff",
            planning_bucket="planning",
            token="secret",
        ),
        client=client,
    )

    points = repository._forecast_points(  # noqa: SLF001 - verifies snapshot selection
        "conservative_energy_kwh",
        "home",
        "open_meteo",
        start,
        start + timedelta(hours=2),
    )

    assert [point.value for point in points] == [3, 4]


class Row:
    def __init__(self, at, **values):
        self.at = at
        self.values = values

    def get_time(self):
        return self.at

    def get_value(self):
        return self.values.get("_value")


@pytest.fixture
def dashboard_case():
    start = datetime(2026, 9, 6, tzinfo=UTC)
    stop = start + timedelta(days=1)
    observed = start - timedelta(hours=2)
    prop = SimpleNamespace(
        id="home",
        timezone="UTC",
        forecast=SimpleNamespace(adapter="open_meteo"),
        battery=SimpleNamespace(usable_capacity_kwh=9, minimum_soc_percent=10),
    )
    fields = dict(
        property="home",
        forecast_day="2026-09-06",
        snapshot="new",
        decision="decision-new",
        decision_id="decision-new",
        forecast_snapshot_id="new",
        plan_version=1,
        plan_point_count=4,
        plan_start=observed.isoformat(),
        plan_stop=stop.isoformat(),
        soc_observed_at=observed.isoformat(),
        target_soc_at=start.isoformat(),
        current_soc_percent=20,
        target_soc_percent=60,
        grid_charge_kwh=4,
        correction_factor=1,
        capacity_shortfall_kwh=0,
        window_shortfall_kwh=0,
        unavoidable_grid_import_kwh=2,
        reserve_shortfall_kwh=0,
        load_model="uniform_elapsed",
    )
    decision = Row(observed, **fields)
    points = [
        Row(
            at,
            property="home",
            forecast_day="2026-09-06",
            snapshot="new",
            decision="decision-new",
            soc_percent=soc,
            stored_kwh=1,
        )
        for at, soc in [(observed, 20), (start, 60), (start + timedelta(hours=1), 50), (stop, 30)]
    ]
    data = {
        "battery_decision": [decision],
        "battery_plan": points,
        "pv_forecast": [
            Row(
                start + timedelta(hours=i),
                snapshot="new",
                issued_at_epoch=observed.timestamp(),
                conservative_power_kw=1,
                conservative_energy_kwh=1,
            )
            for i in range(24)
        ],
        "energy_telemetry": [],
        "electricity_tariff": [],
    }

    def query(**kwargs):
        measurement = next(name for name in data if f'== "{name}"' in kwargs["query"])
        return [SimpleNamespace(records=data[measurement])]

    repository = InfluxDashboardRepository(
        InfluxConfig(
            url="https://influx.invalid",
            org="test",
            telemetry_bucket="telemetry",
            tariff_bucket="tariff",
            planning_bucket="planning",
            token="test",
        ),
        SimpleNamespace(query_api=lambda: SimpleNamespace(query=query)),
    )
    return repository, prop, start, stop, data


def test_plan_uses_persisted_endpoints_and_never_borrows_optional_fields(dashboard_case):
    repository, prop, start, stop, data = dashboard_case
    old = dict(
        data["battery_decision"][0].values,
        decision="older",
        decision_id="older",
        estimated_charge_cost_pence=500,
    )
    data["battery_decision"].append(Row(start - timedelta(hours=3), **old))
    result = repository.curve(prop, start.date())
    assert result["plan"]["available"]
    assert result["summary"]["estimated_charge_cost_pence"] is None
    assert result["series"]["planned_soc_percent"][0] == {
        "at": (start - timedelta(hours=2)).isoformat(),
        "value": 20,
    }
    assert result["series"]["planned_soc_percent"][-1]["at"] == stop.isoformat()


@pytest.mark.parametrize(
    "field,value",
    [
        ("forecast_day", "2026-09-05"),
        ("snapshot", "old"),
        ("property", "other"),
        ("decision_id", "other"),
        ("plan_version", 0),
        ("plan_point_count", 5),
        ("soc_observed_at", "2026-09-05T20:00:00+00:00"),
        ("current_soc_percent", 40),
        ("target_soc_percent", 99),
        ("target_soc_at", "2026-09-06T00:30:00+00:00"),
    ],
)
def test_incoherent_decision_is_unavailable_without_losing_forecast(dashboard_case, field, value):
    repository, prop, start, _, data = dashboard_case
    data["battery_decision"][0].values[field] = value
    result = repository.curve(prop, start.date())
    assert not result["plan"]["available"]
    assert result["summary"]["target_soc_percent"] is None
    assert result["series"]["planned_soc_percent"] == []
    assert result["summary"]["forecast_generation_kwh"] == 24


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_decision", "wrong_snapshot"])
def test_partial_or_mixed_plan_never_displays(dashboard_case, mutation):
    repository, prop, start, _, data = dashboard_case
    if mutation == "missing":
        data["battery_plan"].pop(1)
    elif mutation == "duplicate":
        data["battery_plan"][1] = data["battery_plan"][0]
    else:
        data["battery_plan"][1].values[mutation.removeprefix("wrong_")] = "other"
    assert not repository.curve(prop, start.date())["plan"]["available"]


def test_missing_or_future_day_plan_is_unavailable(dashboard_case):
    repository, prop, start, _, data = dashboard_case
    data["battery_decision"].clear()
    assert repository.curve(prop, start.date())["summary"]["target_soc_percent"] is None
    assert not repository.curve(prop, (start + timedelta(days=1)).date())["plan"]["available"]


def test_counter_reset_is_shared_canonical_quality(dashboard_case):
    repository, prop, start, _, data = dashboard_case
    data["energy_telemetry"] = [Row(start, _value=5), Row(start + timedelta(minutes=5), _value=1)]
    result = repository.curve(prop, start.date())
    assert result["summary"]["actual_generation_kwh"] is None
    assert result["summary"]["actual_energy_quality"] == "invalid"
    assert "counter_reset" in result["summary"]["actual_energy_reason_codes"]


@pytest.mark.parametrize("mutation", ["duplicate_hour", "missing_power", "missing_energy"])
def test_forecast_bundle_requires_aligned_complete_fields(dashboard_case, mutation):
    repository, prop, start, _, data = dashboard_case
    if mutation == "duplicate_hour":
        data["pv_forecast"][1].at = start
    else:
        field = (
            "conservative_power_kw" if mutation == "missing_power" else "conservative_energy_kwh"
        )
        del data["pv_forecast"][1].values[field]
    result = repository.curve(prop, start.date())
    assert result["summary"]["forecast_generation_kwh"] is None
    assert not result["plan"]["available"]


def test_web_default_day_uses_property_timezone(monkeypatch):
    from solar_battery_forecaster import web

    class FrozenDateTime:
        @staticmethod
        def now(zone):
            return datetime(2026, 1, 1, 23, 30, tzinfo=UTC).astimezone(zone)

    monkeypatch.setattr(web, "datetime", FrozenDateTime)
    handler = object.__new__(web.DashboardHandler)
    handler.config = SimpleNamespace(properties=[SimpleNamespace(id="home", timezone="Asia/Tokyo")])
    results = []
    handler.repository = SimpleNamespace(curve=lambda prop, day: {"day": day.isoformat()})
    handler._json = lambda status, payload: results.append(payload)
    handler._serve_api("/api/v1/properties/home/curve", {})
    assert results == [{"day": "2026-01-02"}]


@pytest.mark.parametrize("failure", [None, "server", "repository"])
def test_dashboard_sigterm_finishes_requests_and_always_closes_resources(monkeypatch, failure):
    import signal

    from solar_battery_forecaster import web

    events = []
    handlers = []
    previous = signal.SIG_DFL
    config = SimpleNamespace(properties=[], observability=object())
    monkeypatch.setattr("sys.argv", ["solar-battery-dashboard"])
    monkeypatch.setattr(web, "load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(web, "create_reporter", lambda *args: "reporter")
    monkeypatch.setattr(web, "close_reporter", lambda reporter: events.append(reporter))
    monkeypatch.setattr(web.signal, "getsignal", lambda number: previous)
    monkeypatch.setattr(web.signal, "signal", lambda number, handler: handlers.append(handler))

    def close_resource(name):
        events.append(name)
        if failure == name:
            raise RuntimeError(name)

    def handle_request():
        events.append("request")
        handlers[0](signal.SIGTERM, None)
        events.append("request_finished")

    server = SimpleNamespace(
        handle_request=handle_request,
        server_close=lambda: close_resource("server"),
        RequestHandlerClass=SimpleNamespace(
            repository=SimpleNamespace(close=lambda: close_resource("repository"))
        ),
    )
    monkeypatch.setattr(web, "make_server", lambda *args: server)
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            web.main()
    else:
        web.main()
    assert server.timeout == 0.5
    assert events == ["request", "request_finished", "server", "repository", "reporter"]
    assert handlers[-1] == previous
