from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from solar_battery_forecaster.adapters.factory import (
    forecast_adapter,
    inverter_adapter,
    tariff_adapter,
)
from solar_battery_forecaster.config import AppConfig, PropertyConfig
from solar_battery_forecaster.models import (
    ForecastInterval,
    TariffInterval,
    forecast_snapshot_id,
)
from solar_battery_forecaster.outbound import RequestPacer
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


class Operation:
    name = "operation"

    def __init__(self, config: AppConfig, store: InfluxStore | None = None) -> None:
        self.config = config
        self.store = store or InfluxStore(config.influxdb)
        self.client = httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=1))
        http = config.http
        self.pacer = RequestPacer(
            minimum_spacing_seconds=http.minimum_spacing_seconds,
            max_attempts=http.max_attempts,
            retry_base_seconds=http.retry_base_seconds,
            retry_max_seconds=http.retry_max_seconds,
            retry_after_max_seconds=http.retry_after_max_seconds,
            jitter_seconds=http.jitter_seconds,
        )

    async def close(self) -> None:
        await self.client.aclose()
        await asyncio.to_thread(self.store.close)

    async def run_property_safely(self, prop: PropertyConfig, **kwargs: object) -> bool:
        try:
            await self.run_property(prop, **kwargs)
            return True
        except Exception as exc:
            LOGGER.error(
                "%s failed for property %s (%s)",
                self.name,
                prop.id,
                type(exc).__name__,
            )
            return False

    async def run_property(self, prop: PropertyConfig, **kwargs: object) -> None:
        raise NotImplementedError

    async def run_cycle(self) -> None:
        for index, prop in enumerate(self.config.properties):
            await self.run_property_safely(prop)
            if index + 1 < len(self.config.properties):
                await asyncio.sleep(self.config.schedule.property_phase_seconds)

    async def run_forever(self, interval_seconds: float) -> None:
        while True:
            await self.run_cycle()
            await asyncio.sleep(interval_seconds)


class TelemetryOperation(Operation):
    name = "telemetry"

    def __init__(self, config: AppConfig, store: InfluxStore | None = None) -> None:
        super().__init__(config, store)
        self.adapters = {
            prop.id: inverter_adapter(prop, self.client, self.pacer)
            for prop in config.properties
        }

    async def close(self) -> None:
        await asyncio.gather(*(adapter.close() for adapter in self.adapters.values()))
        await super().close()

    async def run_property(self, prop: PropertyConfig, **kwargs: object) -> None:
        adapter = self.adapters[prop.id]
        item = await adapter.collect()
        await asyncio.to_thread(self.store.write_telemetry, prop.id, adapter.name, item)
        LOGGER.info("stored telemetry for %s", prop.id)


class TariffOperation(Operation):
    name = "tariff"

    def __init__(self, config: AppConfig, store: InfluxStore | None = None) -> None:
        super().__init__(config, store)
        self.adapters = {
            prop.id: tariff_adapter(prop, self.client, self.pacer)
            for prop in config.properties
        }

    async def close(self) -> None:
        await asyncio.gather(*(adapter.close() for adapter in self.adapters.values()))
        await super().close()

    async def run_property(self, prop: PropertyConfig, **kwargs: object) -> None:
        adapter = self.adapters[prop.id]
        intervals = await adapter.fetch()
        await asyncio.to_thread(self.store.write_tariffs, prop.id, adapter.name, intervals)
        LOGGER.info("stored %d tariff intervals for %s", len(intervals), prop.id)


class ForecastPlanOperation(Operation):
    name = "forecast-plan"

    def __init__(self, config: AppConfig, store: InfluxStore | None = None) -> None:
        super().__init__(config, store)
        self.adapters = {
            prop.id: forecast_adapter(prop, self.client, self.pacer)
            for prop in config.properties
        }

    async def close(self) -> None:
        await asyncio.gather(*(adapter.close() for adapter in self.adapters.values()))
        await super().close()

    async def run_cycle(self, now: datetime | None = None) -> None:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        for index, prop in enumerate(self.config.properties):
            local_now = instant.astimezone(ZoneInfo(prop.timezone))
            due_at = datetime.combine(
                local_now.date(),
                datetime.min.time(),
                tzinfo=local_now.tzinfo,
            ).replace(
                hour=self.config.schedule.forecast_hour,
                minute=self.config.schedule.forecast_minute,
            )
            if local_now >= due_at:
                forecast_day = local_now.date() + timedelta(days=1)
                exists = await asyncio.to_thread(
                    self.store.decision_exists, prop.id, forecast_day
                )
                if not exists:
                    await self.run_property_safely(
                        prop, forecast_day=forecast_day, now=instant
                    )
            if index + 1 < len(self.config.properties):
                await asyncio.sleep(self.config.schedule.property_phase_seconds)

    async def run_property(self, prop: PropertyConfig, **kwargs: object) -> None:
        forecast_day = kwargs["forecast_day"]
        now = kwargs["now"]
        if not isinstance(forecast_day, date) or not isinstance(now, datetime):
            raise TypeError("forecast-plan requires a date and current time")
        await self._plan(prop, forecast_day, now)

    async def _plan(self, prop: PropertyConfig, forecast_day: date, now: datetime) -> None:
        timezone = ZoneInfo(prop.timezone)
        local_now = now.astimezone(timezone)
        forecast_start = datetime.combine(
            forecast_day, datetime.min.time(), tzinfo=timezone
        )
        forecast_stop = forecast_start + timedelta(days=1)
        expected_points = int(
            (forecast_stop.astimezone(UTC) - forecast_start.astimezone(UTC)).total_seconds()
            / 3600
        )
        adapter = self.adapters[prop.id]
        snapshot = await asyncio.to_thread(
            self.store.complete_forecast_snapshot,
            prop.id,
            adapter.name,
            forecast_start,
            forecast_stop,
            expected_points,
        )
        if snapshot is None:
            all_intervals = await adapter.fetch(prop)
            intervals = [
                item
                for item in all_intervals
                if item.start.astimezone(timezone).date() == forecast_day
            ]
            self._validate_forecast_coverage(
                intervals, forecast_start, forecast_stop, expected_points
            )
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
            snapshot_id = forecast_snapshot_id(intervals[0].issued_at)
            issued_at = intervals[0].issued_at
        else:
            raw_solar_kwh = snapshot.raw_energy_kwh
            factor = snapshot.correction_factor
            snapshot_id = snapshot.snapshot_id
            issued_at = snapshot.issued_at
        snapshot_age = now.astimezone(UTC) - issued_at.astimezone(UTC)
        if snapshot_age < -timedelta(minutes=5) or snapshot_age > timedelta(hours=36):
            raise RuntimeError("forecast snapshot is stale")
        if (
            not math.isfinite(raw_solar_kwh)
            or not 0 <= raw_solar_kwh <= prop.inverter.rated_power_kw * expected_points
            or not math.isfinite(factor)
            or not 0.25 <= factor <= 2
        ):
            raise RuntimeError("forecast snapshot is invalid")

        soc_reading = await asyncio.to_thread(self.store.latest_soc_reading, prop.id)
        if soc_reading is None:
            raise RuntimeError("battery SoC is unavailable")
        current_soc, observed_at = soc_reading
        soc_age = now.astimezone(UTC) - observed_at.astimezone(UTC)
        if (
            not math.isfinite(current_soc)
            or not 0 <= current_soc <= 100
            or soc_age < -timedelta(minutes=5)
            or soc_age
            > timedelta(seconds=self.config.schedule.telemetry_stale_after_seconds)
        ):
            raise RuntimeError("battery SoC is invalid or stale")

        charge_window_stop = local_now + timedelta(hours=12)
        tariff_batch = await asyncio.to_thread(
            self.store.tariff_intervals, prop.id, local_now, charge_window_stop
        )
        if tariff_batch is None:
            raise RuntimeError("stored tariff is unavailable")
        tariff_age = now.astimezone(UTC) - tariff_batch.retrieved_at.astimezone(UTC)
        if tariff_age < -timedelta(minutes=5) or tariff_age > timedelta(
            minutes=prop.tariff.stale_after_minutes
        ):
            raise RuntimeError("stored tariff is stale")
        required_coverage = (charge_window_stop - local_now).total_seconds() / 3600
        coverage = covered_duration_hours(
            tariff_batch.intervals, local_now, charge_window_stop
        )
        if coverage + 1e-6 < required_coverage:
            raise RuntimeError("stored tariff coverage is incomplete")
        cheap_hours, cheap_average = cheap_window(
            tariff_batch.intervals, local_now, charge_window_stop
        )
        decision = make_decision(
            battery=prop.battery,
            current_soc_percent=current_soc,
            raw_solar_kwh=raw_solar_kwh,
            factor=factor,
            conservative_multiplier=prop.forecast.conservative_multiplier,
            expected_load_kwh=prop.load.expected_kwh_until_next_cheap_window,
            forecast_day=forecast_day,
            forecast_snapshot_id=snapshot_id,
            forecast_issued_at=issued_at,
            soc_observed_at=observed_at,
            tariff_coverage_start=local_now,
            tariff_coverage_stop=charge_window_stop,
            tariff_coverage_hours=coverage,
            cheap_duration_hours=cheap_hours,
            cheap_rate_average_pence=cheap_average,
        )
        await asyncio.to_thread(self.store.write_decision, prop.id, decision)
        LOGGER.info("stored recommendation for %s and %s", prop.id, forecast_day)

    @staticmethod
    def _validate_forecast_coverage(
        intervals: list[ForecastInterval],
        start: datetime,
        stop: datetime,
        expected_points: int,
    ) -> None:
        if (
            len(intervals) != expected_points
            or not intervals
            or intervals[0].start != start
            or intervals[-1].end != stop
            or any(
                current.end - current.start != timedelta(hours=1)
                or current.end != following.start
                for current, following in zip(intervals, intervals[1:], strict=False)
            )
        ):
            raise RuntimeError("forecast provider returned incomplete coverage")


class ReconciliationOperation(Operation):
    name = "reconciliation"

    async def run_cycle(self, now: datetime | None = None) -> None:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        for index, prop in enumerate(self.config.properties):
            local_now = instant.astimezone(ZoneInfo(prop.timezone))
            today_due = local_now.replace(
                hour=self.config.schedule.reconciliation_hour,
                minute=self.config.schedule.reconciliation_minute,
                second=0,
                microsecond=0,
            )
            for age in range(self.config.schedule.reconciliation_catch_up_days, 0, -1):
                day = local_now.date() - timedelta(days=age)
                if age == 1 and local_now < today_due:
                    continue
                exists = await asyncio.to_thread(
                    self.store.daily_result_exists, prop.id, day
                )
                if not exists:
                    await self.run_property_safely(prop, day=day)
            if index + 1 < len(self.config.properties):
                await asyncio.sleep(self.config.schedule.property_phase_seconds)

    async def run_property(self, prop: PropertyConfig, **kwargs: object) -> None:
        day = kwargs["day"]
        if not isinstance(day, date):
            raise TypeError("reconciliation requires a date")
        timezone = ZoneInfo(prop.timezone)
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone)
        stop = start + timedelta(days=1)
        forecast, actual, completeness = await asyncio.to_thread(
            self.store.day_totals,
            prop.id,
            start,
            stop,
            self.config.schedule.telemetry_seconds,
            prop.forecast.adapter,
        )
        if forecast < 0.1 or actual < 0 or completeness < 0.95:
            raise RuntimeError("daily data is incomplete")
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
        LOGGER.info("reconciled %s for %s", prop.id, day)
