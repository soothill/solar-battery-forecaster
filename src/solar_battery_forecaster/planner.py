from __future__ import annotations

import math
import statistics
from datetime import UTC, date, datetime
from uuid import uuid4

from solar_battery_forecaster.config import BatteryConfig
from solar_battery_forecaster.models import (
    BatteryDecision,
    ForecastInterval,
    PlanPoint,
    TariffInterval,
)


def correction_factor(ratios: list[float], default: float = 1.0) -> float:
    """Return a robust factor while ignoring implausible and near-zero-day ratios."""
    valid = [value for value in ratios if 0.25 <= value <= 2.0]
    if len(valid) < 7:
        return default
    return min(1.5, max(0.5, statistics.median(valid[-60:])))


def make_decision(
    *,
    battery: BatteryConfig,
    current_soc_percent: float,
    raw_solar_kwh: float,
    factor: float,
    conservative_multiplier: float,
    expected_load_kwh: float,
    forecast_day: date,
    forecast_snapshot_id: str,
    forecast_issued_at: datetime,
    soc_observed_at: datetime,
    tariff_coverage_start: datetime,
    tariff_coverage_stop: datetime,
    tariff_coverage_hours: float,
    cheap_duration_hours: float | None = None,
    cheap_rate_average_pence: float | None = None,
) -> BatteryDecision:
    """Legacy scalar API; production workers use make_interval_decision."""
    corrected = raw_solar_kwh * factor
    conservative = corrected * conservative_multiplier
    required_stored = min(
        battery.usable_capacity_kwh,
        max(0.0, expected_load_kwh + battery.reserve_kwh - conservative),
    )
    soc_span = battery.maximum_soc_percent - battery.minimum_soc_percent
    stored_now = max(
        0.0,
        (current_soc_percent - battery.minimum_soc_percent)
        / soc_span
        * battery.usable_capacity_kwh,
    )
    requested_grid_charge = max(0.0, required_stored - stored_now) / battery.charge_efficiency
    if cheap_duration_hours is None:
        grid_charge = requested_grid_charge
        duration = 0.0
    else:
        duration = max(0.0, cheap_duration_hours)
        grid_charge = min(requested_grid_charge, battery.max_charge_power_kw * duration)
    target_stored = required_stored
    if grid_charge + 1e-9 < requested_grid_charge:
        target_stored = min(
            battery.usable_capacity_kwh,
            stored_now + grid_charge * battery.charge_efficiency,
        )
    target_soc = battery.minimum_soc_percent + (
        target_stored / battery.usable_capacity_kwh * soc_span
    )
    target_soc = min(battery.maximum_soc_percent, max(battery.minimum_soc_percent, target_soc))
    limited = grid_charge + 1e-9 < requested_grid_charge
    return BatteryDecision(
        created_at=datetime.now(UTC),
        current_soc_percent=current_soc_percent,
        target_soc_percent=target_soc,
        grid_charge_kwh=grid_charge,
        raw_solar_kwh=raw_solar_kwh,
        corrected_solar_kwh=corrected,
        conservative_solar_kwh=conservative,
        expected_load_kwh=expected_load_kwh,
        reserve_kwh=battery.reserve_kwh,
        correction_factor=factor,
        forecast_day=forecast_day,
        forecast_snapshot_id=forecast_snapshot_id,
        forecast_issued_at=forecast_issued_at,
        soc_observed_at=soc_observed_at,
        tariff_coverage_start=tariff_coverage_start,
        tariff_coverage_stop=tariff_coverage_stop,
        tariff_coverage_hours=tariff_coverage_hours,
        reason=(
            "recommendation_only: cheap window limits requested charge"
            if limited
            else "recommendation_only: conservative solar minus expected load and reserve"
        ),
        cheap_duration_hours=duration,
        cheap_rate_average_pence=cheap_rate_average_pence,
        estimated_charge_cost_pence=(
            grid_charge * cheap_rate_average_pence if cheap_rate_average_pence is not None else None
        ),
        charge_limited_by_window=limited,
    )


def make_interval_decision(
    *,
    battery: BatteryConfig,
    current_soc_percent: float,
    forecast_intervals: list[ForecastInterval],
    tariff_intervals: list[TariffInterval],
    expected_load_kwh: float,
    factor: float,
    conservative_multiplier: float,
    inverter_limit_kw: float,
    forecast_day: date,
    forecast_snapshot_id: str,
    forecast_issued_at: datetime,
    soc_observed_at: datetime,
    day_start: datetime,
    day_stop: datetime,
    created_at: datetime | None = None,
    load_intervals: list[tuple[datetime, datetime, float]] | None = None,
) -> BatteryDecision:
    """A bounded, constant-power interval model; never an inverter control schedule.

    The configured load is a daily total. Its elapsed-hour rate also covers the
    observation-to-midnight bridge. Explicit load intervals are available to test
    or supply a measured profile without changing energy-balance semantics.
    """
    primary_cutoff = day_start.replace(hour=12).astimezone(UTC)
    soc_observed_at = soc_observed_at.astimezone(UTC).replace(microsecond=0)
    start, stop = soc_observed_at, day_stop.astimezone(UTC)
    day_start = day_start.astimezone(UTC)
    if start >= stop or start > day_start:
        raise ValueError("plan must begin before the forecast day")
    if (
        not all(
            math.isfinite(v)
            for v in (
                current_soc_percent,
                expected_load_kwh,
                factor,
                conservative_multiplier,
                inverter_limit_kw,
            )
        )
        or not 0 <= current_soc_percent <= 100
        or expected_load_kwh < 0
    ):
        raise ValueError("invalid planning inputs")
    if not battery.minimum_soc_percent <= current_soc_percent <= battery.maximum_soc_percent:
        raise ValueError("observed SoC is outside the modeled operational range")
    if not 0.25 <= factor <= 2 or not 0 < conservative_multiplier <= 1 or inverter_limit_kw <= 0:
        raise ValueError("invalid forecast correction")
    forecasts = sorted(forecast_intervals, key=lambda item: item.start)
    tariffs = sorted(tariff_intervals, key=lambda item: item.start)
    boundaries = {start, stop, day_start}
    if start < primary_cutoff < stop:
        boundaries.add(primary_cutoff)
    for item in [*forecasts, *tariffs]:
        if item.start.tzinfo is None or item.end.tzinfo is None or item.end <= item.start:
            raise ValueError("invalid interval timestamps")
        if item.start.microsecond or item.end.microsecond:
            raise ValueError("interval timestamps must have whole-second precision")
        boundaries.update(t.astimezone(UTC) for t in (item.start, item.end) if start < t < stop)
    if load_intervals:
        for left, right, energy in load_intervals:
            if left.tzinfo is None or right.tzinfo is None or right <= left:
                raise ValueError("invalid load interval")
            if not math.isfinite(energy) or energy < 0:
                raise ValueError("invalid load energy")
            boundaries.update(t.astimezone(UTC) for t in (left, right) if start < t < stop)
    times = sorted(boundaries)
    if len(times) > 512:
        raise ValueError("planning interval limit exceeded")
    # (hours, solar AC kWh, load AC kWh, cheap, price, raw AC kWh, corrected AC kWh)
    slots: list[tuple[float, float, float, bool, float, float, float]] = []
    day_hours = (stop - day_start).total_seconds() / 3600
    for left, right in zip(times, times[1:], strict=False):
        hours = (right - left).total_seconds() / 3600
        matching_forecast = [f for f in forecasts if f.start <= left and f.end >= right]
        matching_tariff = [t for t in tariffs if t.start <= left and t.end >= right]
        if len(matching_forecast) != 1 or len(matching_tariff) != 1:
            raise ValueError("plan requires complete non-overlapping forecast and tariff coverage")
        forecast, tariff = matching_forecast[0], matching_tariff[0]
        duration = (
            forecast.end.astimezone(UTC) - forecast.start.astimezone(UTC)
        ).total_seconds() / 3600
        raw_power = forecast.energy_kwh / duration
        if not math.isfinite(raw_power) or not 0 <= raw_power <= inverter_limit_kw:
            raise ValueError("invalid forecast energy")
        if (
            not isinstance(tariff.is_cheap, bool)
            or not math.isfinite(tariff.price_pence_per_kwh)
            or not -100 <= tariff.price_pence_per_kwh <= 1000
        ):
            raise ValueError("invalid stored tariff")
        corrected = min(inverter_limit_kw, raw_power * factor) * hours
        conservative = corrected * conservative_multiplier
        if forecast.corrected_energy_kwh is not None:
            corrected = forecast.corrected_energy_kwh * hours / duration
        if forecast.conservative_energy_kwh is not None:
            conservative = forecast.conservative_energy_kwh * hours / duration
        if (
            not all(math.isfinite(v) for v in (corrected, conservative))
            or not 0 <= conservative <= corrected <= inverter_limit_kw * hours
        ):
            raise ValueError("invalid stored corrected forecast")
        load = expected_load_kwh * hours / day_hours
        if load_intervals is not None:
            matches = [(a, b, e) for a, b, e in load_intervals if a <= left and b >= right]
            if len(matches) != 1:
                raise ValueError("load profile coverage is incomplete or overlapping")
            a, b, energy = matches[0]
            load = energy * hours / ((b.astimezone(UTC) - a.astimezone(UTC)).total_seconds() / 3600)
        slots.append(
            (
                hours,
                conservative,
                load,
                tariff.is_cheap,
                tariff.price_pence_per_kwh,
                raw_power * hours,
                corrected,
            )
        )
    span = battery.maximum_soc_percent - battery.minimum_soc_percent
    initial = min(
        battery.usable_capacity_kwh,
        max(
            0.0,
            (current_soc_percent - battery.minimum_soc_percent)
            / span
            * battery.usable_capacity_kwh,
        ),
    )
    reserve, efficiency = battery.reserve_kwh, battery.charge_efficiency

    def simulate(capacity: float, power: float) -> tuple[list[float], list[float], float, float]:
        # Required stored energy at each boundary; future cheap opportunities can
        # supply energy only after their actual time. Capacity is applied per boundary.
        required = [reserve] * (len(slots) + 1)
        for index in range(len(slots) - 1, -1, -1):
            hours, solar, load, cheap, *_ = slots[index]
            net = (
                (solar - load) * efficiency
                if solar >= load
                else (solar - load) / battery.discharge_efficiency
            )
            available = power * hours * efficiency if cheap else 0.0
            required[index] = min(capacity, max(reserve, required[index + 1] - net - available))
        stored, imports = initial, 0.0
        energies, charges = [stored], []
        for index, (hours, solar, load, cheap, *_) in enumerate(slots):
            surplus = max(0.0, solar - load) * efficiency
            deficit = max(0.0, load - solar) / battery.discharge_efficiency
            charge = (
                min(
                    power * hours,
                    max(
                        0.0,
                        (min(capacity, required[index + 1]) + deficit - stored - surplus)
                        / efficiency,
                    ),
                )
                if cheap
                else 0.0
            )
            charge = min(charge, max(0.0, (capacity + deficit - stored - surplus) / efficiency))
            available = stored + surplus + charge * efficiency
            discharge = min(deficit, max(0.0, available - reserve))
            imports += (deficit - discharge) * battery.discharge_efficiency
            stored = min(capacity, max(0.0, available - discharge))
            energies.append(stored)
            charges.append(charge)
        return energies, charges, imports, max(0.0, reserve - stored)

    energies, charges, imports, reserve_shortfall = simulate(
        battery.usable_capacity_kwh, battery.max_charge_power_kw
    )
    # Marginal diagnostics use the same initial state, solar and cheap opportunities.
    # Capacity is isolated first, then finite charging power; no double-counting.
    unlimited = max(
        1.0, initial + sum(s[2] for s in slots) / battery.discharge_efficiency + reserve
    )
    _, _, ideal_imports, ideal_reserve = simulate(
        unlimited, unlimited / min(s[0] for s in slots) / efficiency
    )
    _, _, capacity_imports, capacity_reserve = simulate(
        battery.usable_capacity_kwh, unlimited / min(s[0] for s in slots) / efficiency
    )
    capacity_shortfall = max(
        0.0,
        capacity_imports
        - ideal_imports
        + (capacity_reserve - ideal_reserve) * battery.discharge_efficiency,
    )
    window_shortfall = max(
        0.0,
        imports
        - capacity_imports
        + (reserve_shortfall - capacity_reserve) * battery.discharge_efficiency,
    )
    if not any(slot[3] for slot in slots):
        window_shortfall = imports + reserve_shortfall * battery.discharge_efficiency
    # Primary opportunity is the first contiguous cheap block. Later cheap periods
    # are simulated but never silently included in tonight's charge/cost summary.
    primary = []
    for index, slot in enumerate(slots):
        if times[index] >= primary_cutoff:
            break
        if slot[3] and (not primary or index == primary[-1] + 1):
            primary.append(index)
        elif primary:
            break
    target_index = primary[-1] + 1 if primary else 0
    primary_charge = sum(charges[i] for i in primary)
    primary_hours = sum(slots[i][0] for i in primary)
    primary_cost = sum(charges[i] * slots[i][4] for i in primary)
    points = tuple(
        PlanPoint(
            t, battery.minimum_soc_percent + energy / battery.usable_capacity_kwh * span, energy
        )
        for t, energy in zip(times, energies, strict=True)
    )
    day_slots = [s for t, s in zip(times, slots, strict=False) if t >= day_start]
    return BatteryDecision(
        created_at=(created_at or datetime.now(UTC)).astimezone(UTC),
        current_soc_percent=current_soc_percent,
        target_soc_percent=points[target_index].soc_percent,
        grid_charge_kwh=primary_charge,
        raw_solar_kwh=sum(s[5] for s in day_slots),
        corrected_solar_kwh=sum(s[6] for s in day_slots),
        conservative_solar_kwh=sum(s[1] for s in day_slots),
        expected_load_kwh=expected_load_kwh,
        reserve_kwh=reserve,
        correction_factor=factor,
        forecast_day=forecast_day,
        forecast_snapshot_id=forecast_snapshot_id,
        forecast_issued_at=forecast_issued_at,
        soc_observed_at=soc_observed_at,
        tariff_coverage_start=start,
        tariff_coverage_stop=stop,
        tariff_coverage_hours=(stop - start).total_seconds() / 3600,
        reason="recommendation_only: interval balance with retained reserve; uniform elapsed load"
        if load_intervals is None
        else "recommendation_only: interval balance with supplied load",
        cheap_duration_hours=primary_hours,
        cheap_rate_average_pence=(primary_cost / primary_charge if primary_charge else None),
        estimated_charge_cost_pence=primary_cost if primary else None,
        charge_limited_by_window=window_shortfall > 1e-8,
        decision_id=uuid4().hex,
        plan_version=1,
        plan_start=start,
        plan_stop=stop,
        target_soc_at=points[target_index].at,
        plan_points=points,
        capacity_shortfall_kwh=capacity_shortfall,
        window_shortfall_kwh=window_shortfall,
        unavoidable_grid_import_kwh=imports,
        reserve_shortfall_kwh=reserve_shortfall,
        horizon_grid_charge_kwh=sum(charges),
        load_model="uniform_elapsed" if load_intervals is None else "supplied_intervals",
    )
