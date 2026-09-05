from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from solar_battery_forecaster.models import TariffInterval
from solar_battery_forecaster.operations import (
    ForecastPlanOperation,
    Operation,
    ReconciliationOperation,
    cheap_window,
    covered_duration_hours,
    validated_tariff_timeline,
)


def test_tariff_coverage_and_weighted_cheap_price() -> None:
    start = datetime(2026, 9, 5, tzinfo=UTC)
    intervals = [
        TariffInterval(
            start=start + timedelta(minutes=30 * index),
            end=start + timedelta(minutes=30 * (index + 1)),
            price_pence_per_kwh=5 + index,
            is_cheap=index < 2,
        )
        for index in range(4)
    ]
    stop = start + timedelta(hours=2)
    assert covered_duration_hours(intervals, start, stop) == 2
    duration, price = cheap_window(intervals, start, stop)
    assert duration == 1
    assert price == pytest.approx(5.5)


def test_tariff_coverage_stops_at_a_gap() -> None:
    start = datetime(2026, 9, 5, tzinfo=UTC)
    intervals = [
        TariffInterval(start, start + timedelta(minutes=30), 5, True),
        TariffInterval(
            start + timedelta(hours=1),
            start + timedelta(hours=2),
            5,
            True,
        ),
    ]
    assert covered_duration_hours(intervals, start, start + timedelta(hours=2)) == 0.5


def test_tariff_timeline_allows_boundaries_and_rejects_overlap() -> None:
    start = datetime(2026, 9, 5, tzinfo=UTC)
    boundary = [
        TariffInterval(start, start + timedelta(minutes=30), 5, True),
        TariffInterval(
            start + timedelta(minutes=30), start + timedelta(hours=1), 15, False
        ),
    ]
    assert validated_tariff_timeline(list(reversed(boundary))) == boundary

    overlapping = [
        boundary[0],
        TariffInterval(
            start + timedelta(minutes=29), start + timedelta(hours=1), 15, False
        ),
    ]
    with pytest.raises(ValueError, match="overlap"):
        validated_tariff_timeline(overlapping)


@pytest.mark.asyncio
async def test_safe_operation_log_does_not_include_exception_details(caplog) -> None:
    operation = object.__new__(Operation)
    operation.name = "test"

    async def fail(prop: object, **kwargs: object) -> None:
        raise RuntimeError("https://secret.invalid/system/123?latitude=50")

    operation.run_property = fail
    result = await operation.run_property_safely(SimpleNamespace(id="home"))
    assert result is False
    assert "secret.invalid" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_property_cycle_is_sequential(monkeypatch) -> None:
    operation = object.__new__(Operation)
    operation.config = SimpleNamespace(
        properties=[SimpleNamespace(id="one"), SimpleNamespace(id="two")],
        schedule=SimpleNamespace(property_phase_seconds=0.25),
    )
    events: list[str] = []

    async def record(prop: object, **kwargs: object) -> bool:
        events.append(prop.id)
        return True

    async def record_sleep(delay: float) -> None:
        events.append(f"sleep:{delay}")

    operation.run_property_safely = record
    monkeypatch.setattr("solar_battery_forecaster.operations.asyncio.sleep", record_sleep)
    assert await operation.run_cycle() is True
    assert events == ["one", "sleep:0.25", "two"]


@pytest.mark.asyncio
async def test_property_cycle_reports_failure_after_running_all_properties() -> None:
    operation = object.__new__(Operation)
    operation.config = SimpleNamespace(
        properties=[SimpleNamespace(id="one"), SimpleNamespace(id="two")],
        schedule=SimpleNamespace(property_phase_seconds=0),
    )
    attempted: list[str] = []

    async def record(prop: object, **kwargs: object) -> bool:
        attempted.append(prop.id)
        return prop.id != "one"

    operation.run_property_safely = record

    assert await operation.run_cycle() is False
    assert attempted == ["one", "two"]


@pytest.mark.asyncio
async def test_run_forever_waits_full_interval_after_overrun(monkeypatch) -> None:
    operation = object.__new__(Operation)
    events: list[object] = []

    async def overrun_cycle() -> bool:
        events.append("cycle-finished")
        return False

    async def stop_after_sleep(delay: float) -> None:
        events.append(delay)
        raise StopAsyncIteration

    operation.run_cycle = overrun_cycle
    monkeypatch.setattr("solar_battery_forecaster.operations.asyncio.sleep", stop_after_sleep)
    with pytest.raises(StopAsyncIteration):
        await operation.run_forever(300)
    assert events == ["cycle-finished", 300]


def forecast_scan(
    existing: bool = False, job_success: bool = True
) -> tuple[ForecastPlanOperation, list[object]]:
    prop = SimpleNamespace(id="home", timezone="Europe/London")
    store = SimpleNamespace(decision_exists=lambda property_id, day: existing)
    operation = object.__new__(ForecastPlanOperation)
    operation.store = store
    operation.config = SimpleNamespace(
        properties=[prop],
        schedule=SimpleNamespace(
            forecast_hour=21,
            forecast_minute=30,
            property_phase_seconds=0,
        ),
    )
    planned: list[object] = []

    async def record(prop: object, **kwargs: object) -> bool:
        planned.append(kwargs["forecast_day"])
        return job_success

    operation.run_property_safely = record
    return operation, planned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "local_time",
    [
        datetime(2026, 6, 1, 0, 1, tzinfo=ZoneInfo("Europe/London")),
        datetime(2026, 6, 1, 12, 0, tzinfo=ZoneInfo("Europe/London")),
        datetime(2026, 6, 1, 21, 29, tzinfo=ZoneInfo("Europe/London")),
    ],
)
async def test_forecast_scan_does_nothing_before_today_due(local_time: datetime) -> None:
    operation, planned = forecast_scan()
    assert await operation.run_cycle(local_time.astimezone(UTC)) is True
    assert planned == []


@pytest.mark.asyncio
async def test_forecast_scan_plans_only_local_tomorrow_after_due() -> None:
    operation, planned = forecast_scan()
    local_time = datetime(2026, 6, 1, 21, 31, tzinfo=ZoneInfo("Europe/London"))
    assert await operation.run_cycle(local_time.astimezone(UTC)) is True
    assert [day.isoformat() for day in planned] == ["2026-06-02"]


@pytest.mark.asyncio
async def test_forecast_scan_reports_due_job_failure() -> None:
    operation, planned = forecast_scan(job_success=False)
    local_time = datetime(2026, 6, 1, 21, 31, tzinfo=ZoneInfo("Europe/London"))

    assert await operation.run_cycle(local_time.astimezone(UTC)) is False
    assert [day.isoformat() for day in planned] == ["2026-06-02"]


@pytest.mark.asyncio
async def test_forecast_scan_restart_after_due_is_idempotent() -> None:
    operation, planned = forecast_scan(existing=True)
    local_time = datetime(2026, 6, 1, 23, 0, tzinfo=ZoneInfo("Europe/London"))
    assert await operation.run_cycle(local_time.astimezone(UTC)) is True
    assert planned == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("local_time", "expected_day"),
    [
        (
            datetime(2026, 3, 29, 21, 31, tzinfo=ZoneInfo("Europe/London")),
            "2026-03-30",
        ),
        (
            datetime(2026, 10, 25, 21, 31, tzinfo=ZoneInfo("Europe/London")),
            "2026-10-26",
        ),
    ],
)
async def test_forecast_scan_uses_local_tomorrow_across_dst_boundaries(
    local_time: datetime, expected_day: str
) -> None:
    operation, planned = forecast_scan()
    assert await operation.run_cycle(local_time.astimezone(UTC)) is True
    assert [day.isoformat() for day in planned] == [expected_day]


@pytest.mark.asyncio
async def test_reconciliation_scan_reports_due_job_failure() -> None:
    prop = SimpleNamespace(id="home", timezone="Europe/London")
    operation = object.__new__(ReconciliationOperation)
    operation.store = SimpleNamespace(
        daily_result_exists=lambda property_id, day: False
    )
    operation.config = SimpleNamespace(
        properties=[prop],
        schedule=SimpleNamespace(
            reconciliation_hour=0,
            reconciliation_minute=15,
            reconciliation_catch_up_days=1,
            property_phase_seconds=0,
        ),
    )
    attempted: list[object] = []

    async def fail(prop: object, **kwargs: object) -> bool:
        attempted.append(kwargs["day"])
        return False

    operation.run_property_safely = fail
    local_time = datetime(2026, 6, 2, 12, 0, tzinfo=ZoneInfo("Europe/London"))

    assert await operation.run_cycle(local_time.astimezone(UTC)) is False
    assert [day.isoformat() for day in attempted] == ["2026-06-01"]
