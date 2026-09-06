from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from solar_battery_forecaster.config import BatteryConfig
from solar_battery_forecaster.models import ForecastInterval, TariffInterval
from solar_battery_forecaster.planner import make_interval_decision


def plan(
    *,
    solar_hour=14,
    load_hour=7,
    load=8,
    capacity=9,
    power=6,
    cheap_hours=(0, 1),
    day=None,
    current_soc=10,
    cheap_day_only=False,
):
    day = day or datetime(2026, 9, 6, tzinfo=UTC)
    stop = (day + timedelta(days=1)).astimezone(UTC)
    start = day.astimezone(UTC) - timedelta(hours=2)
    hours = int((stop - start).total_seconds() / 3600)
    forecast, tariff, loads = [], [], []
    for index in range(hours):
        left = start + timedelta(hours=index)
        right = left + timedelta(hours=1)
        local = left.astimezone(day.tzinfo)
        energy = 9 if local.hour == solar_hour and local.date() == day.date() else 0
        forecast.append(ForecastInterval(left, right, energy, energy, start, "test"))
        cheap = local.hour in cheap_hours and (not cheap_day_only or local.date() == day.date())
        tariff.append(TariffInterval(left, right, 7 if cheap else 30, cheap))
        loads.append(
            (left, right, load if local.hour == load_hour and local.date() == day.date() else 0)
        )
    return make_interval_decision(
        battery=BatteryConfig(
            usable_capacity_kwh=capacity,
            max_charge_power_kw=power,
            charge_efficiency=1,
            reserve_kwh=1,
        ),
        current_soc_percent=current_soc,
        forecast_intervals=forecast,
        tariff_intervals=tariff,
        expected_load_kwh=load,
        factor=1,
        conservative_multiplier=1,
        inverter_limit_kw=10,
        forecast_day=day.date(),
        forecast_snapshot_id="snapshot",
        forecast_issued_at=start,
        soc_observed_at=start,
        day_start=day,
        day_stop=stop,
        created_at=start,
        load_intervals=loads,
    )


def test_solar_timing_changes_recommendation_and_end_points():
    morning = plan(solar_hour=6)
    afternoon = plan(solar_hour=14)
    assert morning.raw_solar_kwh == afternoon.raw_solar_kwh == 9
    assert afternoon.grid_charge_kwh > morning.grid_charge_kwh
    assert afternoon.grid_charge_kwh == pytest.approx(9)
    assert afternoon.unavoidable_grid_import_kwh == 0
    assert afternoon.plan_points[0].at == afternoon.soc_observed_at
    assert afternoon.plan_points[0].soc_percent == 10
    assert afternoon.target_soc_at.hour == 2
    assert afternoon.plan_points[-1].at == afternoon.plan_stop


def test_capacity_and_window_shortfalls_are_separate():
    capacity = plan(solar_hour=-1, load=20, capacity=9)
    assert capacity.capacity_shortfall_kwh == pytest.approx(12)
    assert capacity.window_shortfall_kwh == 0
    assert capacity.unavoidable_grid_import_kwh == pytest.approx(12)
    window = plan(solar_hour=-1, load=8, power=1, cheap_hours=(0,))
    assert window.capacity_shortfall_kwh == 0
    assert window.window_shortfall_kwh == pytest.approx(8)
    assert window.charge_limited_by_window


def test_later_cheap_charge_is_separate_from_primary_overnight():
    result = plan(solar_hour=-1, load=12, capacity=9, load_hour=23, cheap_hours=(0, 1, 23))
    assert result.horizon_grid_charge_kwh > result.grid_charge_kwh
    assert result.estimated_charge_cost_pence == pytest.approx(result.grid_charge_kwh * 7)
    assert result.target_soc_at.hour == 2


def test_only_evening_cheap_period_does_not_become_overnight_recommendation():
    result = plan(solar_hour=-1, load=3, cheap_hours=(23,), load_hour=23, cheap_day_only=True)
    assert result.grid_charge_kwh == 0
    assert result.estimated_charge_cost_pence is None
    assert result.horizon_grid_charge_kwh > 0


@pytest.mark.parametrize(
    ("day", "expected"), [(datetime(2026, 3, 29), 25), (datetime(2026, 10, 25), 27)]
)
def test_dst_horizon_is_measured_in_utc(day, expected):
    result = plan(day=day.replace(tzinfo=ZoneInfo("Europe/London")))
    assert result.tariff_coverage_hours == expected
    assert len(result.plan_points) == expected + 1
    assert all(
        b.at - a.at == timedelta(hours=1)
        for a, b in zip(result.plan_points, result.plan_points[1:], strict=False)
    )


def test_no_cheap_period_discloses_imports_and_missing_reserve():
    result = plan(solar_hour=-1, cheap_hours=())
    assert result.grid_charge_kwh == 0
    assert result.unavoidable_grid_import_kwh == 8
    assert result.reserve_shortfall_kwh == 1
    assert result.window_shortfall_kwh == 9


def test_out_of_operating_range_observation_is_not_fabricated():
    with pytest.raises(ValueError, match="outside the modeled operational range"):
        plan(current_soc=5)
