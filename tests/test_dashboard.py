from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from solar_battery_forecaster.config import InfluxConfig
from solar_battery_forecaster.dashboard import (
    CurvePoint,
    InfluxDashboardRepository,
    local_day_bounds,
    project_battery_soc,
)


def test_local_day_bounds_handle_british_summer_time() -> None:
    start, stop = local_day_bounds(date(2026, 6, 1), "Europe/London")
    assert start == datetime(2026, 5, 31, 23, tzinfo=UTC)
    assert stop == datetime(2026, 6, 1, 23, tzinfo=UTC)


def test_local_day_bounds_handle_dst_transition() -> None:
    start, stop = local_day_bounds(date(2026, 10, 25), "Europe/London")
    assert (stop - start).total_seconds() == 25 * 60 * 60


def test_projection_uses_forecast_and_load_without_exceeding_limits() -> None:
    points = [
        CurvePoint(datetime(2026, 1, 1, hour, tzinfo=UTC), energy)
        for hour, energy in [(1, 0), (2, 2), (3, 10), (4, 0)]
    ]
    result = project_battery_soc(points, 50, 10, 4, 10, 90)
    assert [item.value for item in result] == pytest.approx([42, 50, 90, 82])


def test_projection_is_empty_without_a_forecast() -> None:
    assert project_battery_soc([], 50, 9, 8, 10, 100) == []


def test_repository_rejects_property_id_before_building_query() -> None:
    repository = InfluxDashboardRepository(
        InfluxConfig(
            url="http://influx.invalid",
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
            '../x\") |> yield(name: "injected")',
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
            url="http://influx.invalid",
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
