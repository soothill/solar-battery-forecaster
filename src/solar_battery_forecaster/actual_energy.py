"""Canonical quality assessment of a property's within-day PV energy counter."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class ActualEnergy:
    energy_kwh: float | None
    coverage_fraction: float
    quality: str
    reason_codes: tuple[str, ...]
    latest_observed_at: datetime | None
    calibration_eligible: bool


def evaluate_actual_energy(
    samples: list[tuple[datetime, float]],
    start: datetime,
    stop: datetime,
    expected_interval_seconds: int = 300,
    *,
    now: datetime | None = None,
) -> ActualEnergy:
    """Never infer full bucket energy from sparse power samples.

    The daily counter supplies cumulative generation since property midnight.
    Count alone cannot establish coverage. At most one expected cadence is credited
    at each boundary and at most two cadences between observations; longer gaps
    invalidate calibration even if other samples are duplicated or clustered.
    """
    start, stop = start.astimezone(UTC), stop.astimezone(UTC)
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    if stop <= start or expected_interval_seconds <= 0:
        raise ValueError("invalid actual-energy window")
    rows = sorted((t.astimezone(UTC), float(v)) for t, v in samples if start <= t < stop)
    if not rows:
        return ActualEnergy(None, 0.0, "unavailable", ("no_samples",), None, False)
    reasons: set[str] = set()
    if any(not math.isfinite(v) or v < 0 for _, v in rows):
        reasons.add("invalid_counter")
    if len({t for t, _ in rows}) != len(rows):
        reasons.add("duplicate_timestamp")
    if any(b[1] + 1e-8 < a[1] for a, b in zip(rows, rows[1:], strict=False)):
        reasons.add("counter_reset")
    cadence = float(expected_interval_seconds)
    duration = (stop - start).total_seconds()
    covered = min(cadence, max(0.0, (rows[0][0] - start).total_seconds()))
    covered += min(cadence, max(0.0, (min(stop, instant) - rows[-1][0]).total_seconds()))
    for (left, _), (right, _) in zip(rows, rows[1:], strict=False):
        gap = (right - left).total_seconds()
        if gap > cadence * 2:
            reasons.add("sampling_gap")
            continue
        covered += gap
    coverage = min(1.0, covered / duration)
    if (rows[0][0] - start).total_seconds() > cadence:
        reasons.add("missing_day_start")
    if instant < stop:
        reasons.add("partial_day")
    elif (stop - rows[-1][0]).total_seconds() > cadence:
        reasons.add("missing_day_end")
    if coverage < 0.95:
        reasons.add("insufficient_coverage")
    invalid = bool(reasons & {"invalid_counter", "counter_reset", "duplicate_timestamp"})
    energy = None if invalid else rows[-1][1]
    eligible = not reasons
    return ActualEnergy(
        energy,
        coverage,
        "invalid" if invalid else "complete" if eligible else "partial",
        tuple(sorted(reasons)),
        rows[-1][0],
        eligible,
    )
