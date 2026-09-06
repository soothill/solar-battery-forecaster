from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from solar_battery_forecaster.config import BatteryConfig
from solar_battery_forecaster.models import (
    ForecastInterval,
    ForecastSnapshot,
    StoredTariffs,
    TariffInterval,
    forecast_snapshot_id,
)
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
        TariffInterval(start + timedelta(minutes=30), start + timedelta(hours=1), 15, False),
    ]
    assert validated_tariff_timeline(list(reversed(boundary))) == boundary

    overlapping = [
        boundary[0],
        TariffInterval(start + timedelta(minutes=29), start + timedelta(hours=1), 15, False),
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
async def test_outbox_admission_failure_prevents_property_collection() -> None:
    operation = object.__new__(Operation)
    operation.name = "telemetry"
    operation.store = SimpleNamespace(
        admit_collection=lambda property_id: (_ for _ in ()).throw(RuntimeError("full"))
    )
    attempted: list[str] = []

    async def record(prop: object, **kwargs: object) -> None:
        attempted.append(prop.id)

    operation.run_property = record
    assert await operation.run_property_safely(SimpleNamespace(id="home")) is False
    assert attempted == []


@pytest.mark.asyncio
async def test_cycle_replays_outbox_before_properties() -> None:
    operation = object.__new__(Operation)
    operation.config = SimpleNamespace(
        properties=[SimpleNamespace(id="one")],
        schedule=SimpleNamespace(property_phase_seconds=0),
    )
    events: list[str] = []
    operation.store = SimpleNamespace(replay=lambda: events.append("replay"))

    async def record(prop: object, **kwargs: object) -> bool:
        events.append(prop.id)
        return True

    operation.run_property_safely = record
    assert await operation.run_cycle() is True
    assert events == ["replay", "one"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("elapsed", "delay"), [(20, 280), (350, 250), (650, 250)])
async def test_run_forever_uses_start_to_start_deadlines(monkeypatch, elapsed, delay) -> None:
    operation = object.__new__(Operation)
    events: list[object] = []

    async def overrun_cycle() -> bool:
        events.append("cycle-finished")
        return False

    async def stop_after_sleep(delay: float) -> None:
        events.append(delay)
        raise StopAsyncIteration

    operation.run_cycle = overrun_cycle
    clock = iter([0.0, float(elapsed), float(elapsed)])
    monkeypatch.setattr(
        "solar_battery_forecaster.operations.time", SimpleNamespace(monotonic=lambda: next(clock))
    )
    monkeypatch.setattr("solar_battery_forecaster.operations.asyncio.sleep", stop_after_sleep)
    with pytest.raises(StopAsyncIteration):
        await operation.run_forever(300)
    assert events == ["cycle-finished", delay]


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
    operation.store = SimpleNamespace(daily_result_exists=lambda property_id, day: False)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("local_day", [datetime(2026, 3, 29), datetime(2026, 10, 25)])
async def test_production_planner_preserves_bridge_and_reuses_snapshot(local_day):
    zone = ZoneInfo("Europe/London")
    day = local_day.replace(tzinfo=zone)
    now = (day - timedelta(hours=2, minutes=30)).astimezone(UTC)
    observed = now - timedelta(minutes=5)
    start, stop = observed.replace(minute=0), (day + timedelta(days=1)).astimezone(UTC)
    intervals = [
        ForecastInterval(
            start + timedelta(hours=i), start + timedelta(hours=i + 1), 0.0, 0.0, now, "test"
        )
        for i in range(int((stop - start).total_seconds() / 3600))
    ]
    tariffs = [TariffInterval(start, stop, 7.0, True)]
    stored = {"snapshot": None, "fetches": 0, "decisions": []}

    async def fetch(prop):
        stored["fetches"] += 1
        return intervals

    def write_forecast(property_id, saved, factor, multiplier, **kwargs):
        assert saved[0].start <= observed
        assert kwargs["forecast_day_start"] == day
        stored["snapshot"] = ForecastSnapshot(
            "test",
            forecast_snapshot_id(now),
            now,
            int((stop - day.astimezone(UTC)).total_seconds() / 3600),
            0.0,
            factor,
            tuple(saved),
        )
        return "buffered"

    def write_decision(property_id, decision):
        stored["decisions"].append(decision)
        return "buffered"

    operation = object.__new__(ForecastPlanOperation)
    operation.reporter = None
    operation.store = SimpleNamespace(
        latest_soc_reading=lambda _: (20.0, observed),
        complete_forecast_snapshot=lambda *args: stored["snapshot"],
        recent_daily_ratios=lambda _: [],
        write_forecast=write_forecast,
        tariff_intervals=lambda *args: StoredTariffs(tariffs, now),
        write_decision=write_decision,
    )
    operation.config = SimpleNamespace(schedule=SimpleNamespace(telemetry_stale_after_seconds=900))
    operation.adapters = {"home": SimpleNamespace(name="test", fetch=fetch)}
    prop = SimpleNamespace(
        id="home",
        timezone="Europe/London",
        inverter=SimpleNamespace(rated_power_kw=6),
        forecast=SimpleNamespace(initial_correction_factor=1, conservative_multiplier=0.8),
        battery=BatteryConfig(usable_capacity_kwh=9, max_charge_power_kw=3),
        load=SimpleNamespace(expected_kwh_until_next_cheap_window=8),
        tariff=SimpleNamespace(stale_after_minutes=480),
    )
    await operation._plan(prop, day.date(), now)
    await operation._plan(prop, day.date(), now)
    assert stored["fetches"] == 1
    first, second = stored["decisions"]
    assert first.plan_version == second.plan_version == 1
    assert first.plan_points == second.plan_points
    assert first.plan_points[0].at == observed
    assert first.plan_points[-1].at == stop
    assert first.tariff_coverage_hours == (stop - observed).total_seconds() / 3600
