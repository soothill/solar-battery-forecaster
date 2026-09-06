from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import get_type_hints

from solar_battery_forecaster.models import BatteryDecision
from solar_battery_forecaster.storage import DeliveryDisposition, InfluxStore


def test_store_delivery_return_annotations_match_runtime_contract() -> None:
    assert get_type_hints(InfluxStore.__init__)["return"] is type(None)
    assert get_type_hints(InfluxStore._write)["return"] == DeliveryDisposition


class ForecastRecord:
    def __init__(self, snapshot: str, issued_at: datetime, energy: float) -> None:
        self.values = {
            "snapshot": snapshot,
            "issued_at_epoch": issued_at.timestamp(),
            "raw_energy_kwh": energy,
            "correction_factor": 0.9,
        }
        self.at = datetime(2026, 9, 6, tzinfo=UTC) + timedelta(hours=energy % 2)

    def get_time(self) -> datetime:
        return self.at


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
        forecast_day=created.date() + timedelta(days=1),
        forecast_snapshot_id="2026-09-05T20:28:00.000000Z",
        forecast_issued_at=issued,
        soc_observed_at=observed,
        tariff_coverage_start=created,
        tariff_coverage_stop=created + timedelta(hours=12),
        tariff_coverage_hours=12,
        reason="recommendation_only: test",
    )
    captured: list[object] = []
    buckets: list[str] = []
    store = object.__new__(InfluxStore)

    def capture(bucket: str, points: list[object], **metadata: object) -> None:
        buckets.append(bucket)
        captured.extend(points)

    store.config = SimpleNamespace(planning_bucket="planning")
    store._write = capture

    store.write_decision("home", decision)

    line = captured[0].to_line_protocol()
    assert buckets == ["planning"]
    assert 'forecast_snapshot_id="2026-09-05T20:28:00.000000Z"' in line
    assert f'forecast_issued_at="{issued.isoformat()}"' in line
    assert f'soc_observed_at="{observed.isoformat()}"' in line
    assert "tariff_coverage_hours=12i" in line
    assert f'tariff_coverage_stop="{(created + timedelta(hours=12)).isoformat()}"' in line


def test_complete_forecast_selects_newest_complete_snapshot() -> None:
    old_issued = datetime(2026, 9, 5, 20, tzinfo=UTC)
    new_issued = old_issued + timedelta(hours=1)
    records = [
        ForecastRecord("new", new_issued, 3),
        ForecastRecord("old", old_issued, 1),
        ForecastRecord("new", new_issued, 4),
        ForecastRecord("old", old_issued, 2),
    ]
    store = object.__new__(InfluxStore)
    store.config = SimpleNamespace(planning_bucket="test")
    store._records = lambda query: records

    snapshot = store.complete_forecast_snapshot(
        "home",
        "provider",
        datetime(2026, 9, 6, tzinfo=UTC),
        datetime(2026, 9, 6, 2, tzinfo=UTC),
        expected_points=2,
    )

    assert snapshot is not None
    assert snapshot.snapshot_id == "new"
    assert snapshot.issued_at == new_issued
    assert snapshot.raw_energy_kwh == 7
