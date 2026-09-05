from datetime import UTC, datetime, timedelta

import pytest

from solar_battery_forecaster.config import BatteryConfig
from solar_battery_forecaster.planner import correction_factor, make_decision

FORECAST_ISSUED = datetime(2026, 9, 5, 20, 30, tzinfo=UTC)
SOC_OBSERVED = datetime(2026, 9, 5, 20, 25, tzinfo=UTC)
TARIFF_START = datetime(2026, 9, 5, 20, 30, tzinfo=UTC)
PROVENANCE = {
    "forecast_snapshot_id": "2026-09-05T20:30:00.000000Z",
    "forecast_issued_at": FORECAST_ISSUED,
    "soc_observed_at": SOC_OBSERVED,
    "tariff_coverage_start": TARIFF_START,
    "tariff_coverage_stop": TARIFF_START + timedelta(hours=12),
    "tariff_coverage_hours": 12.0,
}


def battery() -> BatteryConfig:
    return BatteryConfig(
        usable_capacity_kwh=9,
        minimum_soc_percent=10,
        maximum_soc_percent=100,
        reserve_kwh=1,
        max_charge_power_kw=6,
        charge_efficiency=0.9,
    )


def test_factor_requires_enough_history() -> None:
    assert correction_factor([0.8] * 6, default=1.0) == 1.0
    assert correction_factor([0.8] * 7, default=1.0) == 0.8


def test_factor_ignores_outliers_and_is_bounded() -> None:
    assert correction_factor([0.1, 3.0, *([1.8] * 7)]) == 1.5
    assert correction_factor([0.1, 3.0, *([0.3] * 7)]) == 0.5


def test_recommendation_uses_conservative_solar() -> None:
    result = make_decision(
        battery=battery(),
        current_soc_percent=20,
        raw_solar_kwh=5,
        factor=0.8,
        conservative_multiplier=0.75,
        expected_load_kwh=8,
        **PROVENANCE,
    )
    assert result.conservative_solar_kwh == 3
    assert result.target_soc_percent == pytest.approx(70)
    assert result.grid_charge_kwh == pytest.approx(5 / 0.9)


def test_target_is_capped_at_battery_maximum() -> None:
    result = make_decision(
        battery=battery(),
        current_soc_percent=10,
        raw_solar_kwh=0,
        factor=1,
        conservative_multiplier=1,
        expected_load_kwh=50,
        **PROVENANCE,
    )
    assert result.target_soc_percent == 100
    assert result.grid_charge_kwh == pytest.approx(10)


def test_no_grid_charge_when_current_energy_meets_target() -> None:
    result = make_decision(
        battery=battery(),
        current_soc_percent=80,
        raw_solar_kwh=5,
        factor=1,
        conservative_multiplier=1,
        expected_load_kwh=4,
        **PROVENANCE,
    )
    assert result.target_soc_percent == 10
    assert result.grid_charge_kwh == 0


def test_charge_is_limited_by_available_cheap_window() -> None:
    result = make_decision(
        battery=battery(),
        current_soc_percent=10,
        raw_solar_kwh=0,
        factor=1,
        conservative_multiplier=1,
        expected_load_kwh=50,
        **PROVENANCE,
        cheap_duration_hours=0.5,
    )
    assert result.grid_charge_kwh == pytest.approx(3)
    assert result.target_soc_percent == pytest.approx(37)
    assert result.charge_limited_by_window is True
    assert result.cheap_duration_hours == 0.5


def test_recommendation_records_estimated_charge_cost() -> None:
    result = make_decision(
        battery=battery(),
        current_soc_percent=10,
        raw_solar_kwh=0,
        factor=1,
        conservative_multiplier=1,
        expected_load_kwh=2,
        **PROVENANCE,
        cheap_duration_hours=1,
        cheap_rate_average_pence=7.5,
    )
    assert result.estimated_charge_cost_pence == pytest.approx(
        result.grid_charge_kwh * 7.5
    )
    assert result.forecast_snapshot_id == PROVENANCE["forecast_snapshot_id"]
    assert result.forecast_issued_at == FORECAST_ISSUED
    assert result.soc_observed_at == SOC_OBSERVED
    assert result.tariff_coverage_hours == 12
