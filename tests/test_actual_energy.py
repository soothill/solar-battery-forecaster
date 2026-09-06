from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from solar_battery_forecaster.actual_energy import evaluate_actual_energy

START = datetime(2026, 9, 5, tzinfo=UTC)
STOP = START + timedelta(days=1)


def samples():
    return [(START + timedelta(minutes=5 * i), i / 10) for i in range(288)]


def test_complete_counter_is_canonical_total():
    result = evaluate_actual_energy(samples(), START, STOP, now=STOP)
    assert result.energy_kwh == 28.7
    assert result.coverage_fraction == 1
    assert result.calibration_eligible


@pytest.mark.parametrize("mode", ["reset", "gap", "duplicates", "late_start", "nan"])
def test_bad_counter_days_are_not_used_for_calibration(mode):
    rows = samples()
    if mode == "reset":
        rows[100] = (rows[100][0], 0)
    elif mode == "gap":
        del rows[50:100]
    elif mode == "duplicates":
        rows[100] = rows[99]
    elif mode == "late_start":
        rows = rows[10:]
    else:
        rows[100] = (rows[100][0], float("nan"))
    assert not evaluate_actual_energy(rows, START, STOP, now=STOP).calibration_eligible


def test_current_day_partial_counter_remains_useful_but_not_calibration():
    result = evaluate_actual_energy(samples()[:100], START, STOP, now=START + timedelta(hours=9))
    assert result.energy_kwh == 9.9
    assert result.quality == "partial"
    assert "partial_day" in result.reason_codes
    assert not result.calibration_eligible


@pytest.mark.parametrize("day", [datetime(2026, 3, 29), datetime(2026, 10, 25)])
def test_dst_actual_coverage_uses_elapsed_day_length(day):
    local = day.replace(tzinfo=ZoneInfo("Europe/London"))
    start, stop = local.astimezone(UTC), (local + timedelta(days=1)).astimezone(UTC)
    count = int((stop - start).total_seconds() / 300)
    rows = [(start + timedelta(seconds=i * 300), i / 10) for i in range(count)]
    result = evaluate_actual_energy(rows, start, stop, now=stop)
    assert result.coverage_fraction == 1
    assert result.calibration_eligible
