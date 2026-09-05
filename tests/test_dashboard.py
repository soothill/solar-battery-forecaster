from datetime import UTC, date, datetime

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
        InfluxConfig(url="http://influx.invalid", org="test", bucket="test", token="secret"),
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
