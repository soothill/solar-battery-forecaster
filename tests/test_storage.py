from datetime import UTC, datetime, timedelta

from solar_battery_forecaster.models import BatteryDecision
from solar_battery_forecaster.storage import InfluxStore


def test_decision_persists_input_provenance() -> None:
    created = datetime(2026, 9, 5, 20, 30, tzinfo=UTC)
    issued = created - timedelta(minutes=2)
    observed = created - timedelta(minutes=1)
    decision = BatteryDecision(
        created_at=created,
        current_soc_percent=20,
        target_soc_percent=70,
        grid_charge_kwh=5,
        raw_solar_kwh=6,
        corrected_solar_kwh=5.4,
        conservative_solar_kwh=4.3,
        expected_load_kwh=8,
        reserve_kwh=1,
        correction_factor=0.9,
        forecast_snapshot_id="2026-09-05T20:28:00.000000Z",
        forecast_issued_at=issued,
        soc_observed_at=observed,
        tariff_coverage_start=created,
        tariff_coverage_stop=created + timedelta(hours=12),
        tariff_coverage_hours=12,
        reason="recommendation_only: test",
    )
    captured: list[object] = []
    store = object.__new__(InfluxStore)
    store._write = captured.extend

    store.write_decision("home", decision)

    line = captured[0].to_line_protocol()
    assert 'forecast_snapshot_id="2026-09-05T20:28:00.000000Z"' in line
    assert f'forecast_issued_at="{issued.isoformat()}"' in line
    assert f'soc_observed_at="{observed.isoformat()}"' in line
    assert "tariff_coverage_hours=12i" in line
    assert f'tariff_coverage_stop="{(created + timedelta(hours=12)).isoformat()}"' in line
