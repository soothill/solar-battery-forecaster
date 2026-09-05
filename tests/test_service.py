from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from solar_battery_forecaster.models import TariffInterval
from solar_battery_forecaster.service import (
    CollectorService,
    cheap_window,
    covered_duration_hours,
)


@pytest.mark.asyncio
async def test_safe_run_reports_failure() -> None:
    service = object.__new__(CollectorService)

    async def failing_operation(prop: object) -> None:
        raise ValueError("provider unavailable")

    service.failing_operation = failing_operation
    prop = SimpleNamespace(id="home")
    assert await service.safe_run("failing_operation", prop) is False


@pytest.mark.asyncio
async def test_collect_once_raises_after_attempting_every_operation() -> None:
    service = object.__new__(CollectorService)
    service.config = SimpleNamespace(properties=[SimpleNamespace(id="home")])
    attempted: list[str] = []

    async def record(operation: str, prop: object) -> bool:
        attempted.append(operation)
        return operation != "collect_tariff"

    service.safe_run = record
    with pytest.raises(RuntimeError, match="home:collect_tariff"):
        await service.collect_once()
    assert attempted == [
        "collect_telemetry",
        "collect_tariff",
        "collect_forecast_and_plan",
    ]


@pytest.mark.asyncio
async def test_scheduled_initial_collection_is_non_strict() -> None:
    service = object.__new__(CollectorService)
    service.config = SimpleNamespace(properties=[SimpleNamespace(id="home")])

    async def fail(operation: str, prop: object) -> bool:
        return False

    service.safe_run = fail
    await service.collect_once(strict=False)


@pytest.mark.asyncio
async def test_startup_does_not_capture_forecast() -> None:
    service = object.__new__(CollectorService)
    service.config = SimpleNamespace(properties=[SimpleNamespace(id="home")])
    attempted: list[str] = []

    async def record(operation: str, prop: object) -> bool:
        attempted.append(operation)
        return True

    service.safe_run = record
    await service.collect_startup()
    assert attempted == ["collect_telemetry", "collect_tariff"]


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
