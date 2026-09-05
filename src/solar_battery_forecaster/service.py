from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from solar_battery_forecaster.adapters.factory import (
    forecast_adapter,
    inverter_adapter,
    tariff_adapter,
)
from solar_battery_forecaster.config import AppConfig, PropertyConfig
from solar_battery_forecaster.models import TariffInterval, forecast_snapshot_id
from solar_battery_forecaster.planner import correction_factor, make_decision
from solar_battery_forecaster.storage import InfluxStore

LOGGER = logging.getLogger(__name__)


def covered_duration_hours(
    intervals: list[TariffInterval], start: datetime, stop: datetime
) -> float:
    covered = 0.0
    cursor = start
    for item in sorted(intervals, key=lambda value: value.start):
        interval_start = max(start, item.start)
        interval_stop = min(stop, item.end)
        if interval_stop <= interval_start:
            continue
        if interval_start > cursor + timedelta(seconds=1):
            break
        if interval_stop > cursor:
            covered += (interval_stop - max(cursor, interval_start)).total_seconds() / 3600
            cursor = interval_stop
        if cursor >= stop:
            break
    return covered


def cheap_window(
    intervals: list[TariffInterval], start: datetime, stop: datetime
) -> tuple[float, float | None]:
    hours = 0.0
    weighted_price = 0.0
    for item in intervals:
        overlap_start = max(start, item.start)
        overlap_stop = min(stop, item.end)
        if not item.is_cheap or overlap_stop <= overlap_start:
            continue
        duration = (overlap_stop - overlap_start).total_seconds() / 3600
        hours += duration
        weighted_price += duration * item.price_pence_per_kwh
    return hours, weighted_price / hours if hours else None


class CollectorService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.store = InfluxStore(config.influxdb)

    async def close(self) -> None:
        await asyncio.to_thread(self.store.close)

    async def collect_telemetry(self, prop: PropertyConfig) -> None:
        adapter = inverter_adapter(prop)
        try:
            item = await adapter.collect()
            await asyncio.to_thread(
                self.store.write_telemetry, prop.id, adapter.name, item
            )
            LOGGER.info("stored telemetry for %s", prop.id)
        finally:
            await adapter.close()

    async def collect_tariff(self, prop: PropertyConfig) -> None:
        adapter = tariff_adapter(prop)
        try:
            intervals = await adapter.fetch()
            await asyncio.to_thread(
                self.store.write_tariffs, prop.id, adapter.name, intervals
            )
            LOGGER.info("stored %d tariff intervals for %s", len(intervals), prop.id)
        finally:
            await adapter.close()

    async def collect_forecast_and_plan(self, prop: PropertyConfig) -> None:
        forecast_source = forecast_adapter(prop)
        tariff_source = tariff_adapter(prop)
        timezone = ZoneInfo(prop.timezone)
        now = datetime.now(timezone)
        tomorrow = now.date() + timedelta(days=1)
        forecast_start = datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone)
        forecast_stop = forecast_start + timedelta(days=1)
        expected_points = int(
            (forecast_stop.astimezone(UTC) - forecast_start.astimezone(UTC)).total_seconds()
            / 3600
        )
        snapshot = await asyncio.to_thread(
            self.store.complete_forecast_snapshot,
            prop.id,
            forecast_source.name,
            forecast_start,
            forecast_stop,
            expected_points,
        )
        try:
            if snapshot is None:
                all_intervals, tariff_intervals = await asyncio.gather(
                    forecast_source.fetch(prop), tariff_source.fetch(now)
                )
                intervals = [
                    item
                    for item in all_intervals
                    if item.start.astimezone(timezone).date() == tomorrow
                ]
            else:
                tariff_intervals = await tariff_source.fetch(now)
                intervals = []
        finally:
            await asyncio.gather(forecast_source.close(), tariff_source.close())

        if snapshot is None:
            if not intervals:
                raise RuntimeError(f"forecast provider returned no intervals for {tomorrow}")
            if (
                len(intervals) != expected_points
                or intervals[0].start != forecast_start
                or intervals[-1].end != forecast_stop
                or any(
                    current.end - current.start != timedelta(hours=1)
                    or current.end != following.start
                    for current, following in zip(intervals, intervals[1:], strict=False)
                )
            ):
                raise RuntimeError(
                    f"forecast provider returned incomplete coverage for {tomorrow}"
                )
        await asyncio.to_thread(
            self.store.write_tariffs, prop.id, tariff_source.name, tariff_intervals
        )

        if snapshot is None:
            ratios = await asyncio.to_thread(self.store.recent_daily_ratios, prop.id)
            factor = correction_factor(ratios, prop.forecast.initial_correction_factor)
            await asyncio.to_thread(
                self.store.write_forecast,
                prop.id,
                intervals,
                factor,
                prop.forecast.conservative_multiplier,
            )
            raw_solar_kwh = sum(item.energy_kwh for item in intervals)
            decision_snapshot_id = forecast_snapshot_id(intervals[0].issued_at)
            decision_forecast_issued_at = intervals[0].issued_at
        else:
            raw_solar_kwh = snapshot.raw_energy_kwh
            factor = snapshot.correction_factor
            decision_snapshot_id = snapshot.snapshot_id
            decision_forecast_issued_at = snapshot.issued_at
            snapshot_age = now.astimezone(UTC) - snapshot.issued_at
            if snapshot_age < -timedelta(minutes=5) or snapshot_age > timedelta(hours=36):
                raise RuntimeError(f"stored forecast snapshot is stale for {prop.id}")
            LOGGER.info("preserved existing overnight forecast for %s", prop.id)

        if (
            not math.isfinite(raw_solar_kwh)
            or not 0 <= raw_solar_kwh <= prop.inverter.rated_power_kw * expected_points
            or not math.isfinite(factor)
            or not 0.25 <= factor <= 2
        ):
            raise RuntimeError(f"forecast snapshot is invalid for {prop.id}")

        soc_reading = await asyncio.to_thread(self.store.latest_soc_reading, prop.id)
        if soc_reading is None:
            LOGGER.warning("forecast stored for %s; no battery SoC is available yet", prop.id)
            return
        current_soc, observed_at = soc_reading
        if not math.isfinite(current_soc) or not 0 <= current_soc <= 100:
            raise RuntimeError(f"battery SoC is invalid for {prop.id}")
        now_utc = now.astimezone(UTC)
        age = now_utc - observed_at.astimezone(UTC)
        if age < -timedelta(minutes=5) or age > timedelta(
            seconds=self.config.schedule.telemetry_stale_after_seconds
        ):
            raise RuntimeError(f"battery SoC is stale for {prop.id}")
        charge_window_stop = now + timedelta(hours=12)
        required_coverage = (charge_window_stop - now).total_seconds() / 3600
        coverage = covered_duration_hours(tariff_intervals, now, charge_window_stop)
        if coverage + 1e-6 < required_coverage:
            raise RuntimeError(f"tariff coverage is incomplete for {prop.id}")
        cheap_duration_hours, cheap_rate_average = cheap_window(
            tariff_intervals, now, charge_window_stop
        )
        decision = make_decision(
            battery=prop.battery,
            current_soc_percent=current_soc,
            raw_solar_kwh=raw_solar_kwh,
            factor=factor,
            conservative_multiplier=prop.forecast.conservative_multiplier,
            expected_load_kwh=prop.load.expected_kwh_until_next_cheap_window,
            forecast_snapshot_id=decision_snapshot_id,
            forecast_issued_at=decision_forecast_issued_at,
            soc_observed_at=observed_at,
            tariff_coverage_start=now,
            tariff_coverage_stop=charge_window_stop,
            tariff_coverage_hours=coverage,
            cheap_duration_hours=cheap_duration_hours,
            cheap_rate_average_pence=cheap_rate_average,
        )
        await asyncio.to_thread(self.store.write_decision, prop.id, decision)
        LOGGER.info(
            "recommendation for %s: target %.1f%% (grid %.2f kWh)",
            prop.id,
            decision.target_soc_percent,
            decision.grid_charge_kwh,
        )

    async def reconcile(self, prop: PropertyConfig) -> None:
        timezone = ZoneInfo(prop.timezone)
        today = datetime.now(timezone).date()
        day = today - timedelta(days=1)
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone)
        stop = datetime.combine(today, datetime.min.time(), tzinfo=timezone)
        forecast, actual, completeness = await asyncio.to_thread(
            self.store.day_totals,
            prop.id,
            start,
            stop,
            self.config.schedule.telemetry_seconds,
            prop.forecast.adapter,
        )
        if forecast < 0.1 or actual < 0 or completeness < 0.95:
            LOGGER.warning(
                "not reconciling %s for %s: forecast=%.3f actual=%.3f completeness=%.1f%%",
                prop.id,
                day,
                forecast,
                actual,
                completeness * 100,
            )
            return
        ratio = actual / forecast
        existing = await asyncio.to_thread(self.store.recent_daily_ratios, prop.id)
        updated_factor = correction_factor(
            [*existing, ratio], prop.forecast.initial_correction_factor
        )
        await asyncio.to_thread(
            self.store.write_daily_result,
            prop.id,
            day,
            forecast,
            actual,
            ratio,
            updated_factor,
        )
        LOGGER.info("reconciled %s for %s: actual/forecast %.3f", prop.id, day, ratio)

    async def safe_run(self, operation: str, prop: PropertyConfig) -> bool:
        try:
            await getattr(self, operation)(prop)
            return True
        except Exception:
            LOGGER.exception("%s failed for %s", operation, prop.id)
            return False

    async def collect_once(self, *, strict: bool = True) -> None:
        failures: list[str] = []
        for prop in self.config.properties:
            for operation in (
                "collect_telemetry",
                "collect_tariff",
                "collect_forecast_and_plan",
            ):
                if not await self.safe_run(operation, prop):
                    failures.append(f"{prop.id}:{operation}")
        if strict and failures:
            raise RuntimeError("collection failed: " + ", ".join(failures))

    async def collect_startup(self) -> None:
        for prop in self.config.properties:
            for operation in ("collect_telemetry", "collect_tariff"):
                await self.safe_run(operation, prop)

    async def run(self) -> None:
        scheduler = AsyncIOScheduler(timezone=UTC)
        schedule = self.config.schedule
        for prop in self.config.properties:
            scheduler.add_job(
                self.safe_run,
                "interval",
                seconds=schedule.telemetry_seconds,
                args=["collect_telemetry", prop],
                id=f"telemetry-{prop.id}",
                max_instances=1,
                coalesce=True,
            )
            scheduler.add_job(
                self.safe_run,
                "interval",
                minutes=schedule.tariff_minutes,
                args=["collect_tariff", prop],
                id=f"tariff-{prop.id}",
                max_instances=1,
                coalesce=True,
            )
            scheduler.add_job(
                self.safe_run,
                CronTrigger(
                    hour=schedule.forecast_hour,
                    minute=schedule.forecast_minute,
                    timezone=ZoneInfo(prop.timezone),
                ),
                args=["collect_forecast_and_plan", prop],
                id=f"forecast-{prop.id}",
                max_instances=1,
            )
            scheduler.add_job(
                self.safe_run,
                CronTrigger(
                    hour=schedule.reconciliation_hour,
                    minute=schedule.reconciliation_minute,
                    timezone=ZoneInfo(prop.timezone),
                ),
                args=["reconcile", prop],
                id=f"reconcile-{prop.id}",
                max_instances=1,
            )

        scheduler.start()
        LOGGER.info("collector started for %d properties", len(self.config.properties))
        await self.collect_startup()
        try:
            await asyncio.Event().wait()
        finally:
            scheduler.shutdown(wait=False)
