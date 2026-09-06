from __future__ import annotations

import math
from datetime import UTC

from solar_battery_forecaster.models import TariffInterval


def validated_tariff_timeline(
    intervals: list[TariffInterval],
) -> list[TariffInterval]:
    """Return one ordered timeline, rejecting ambiguous or overlapping intervals."""
    for item in intervals:
        if item.start.utcoffset() is None or item.end.utcoffset() is None:
            raise ValueError("tariff timestamps must be timezone-aware")
        if not math.isfinite(item.price_pence_per_kwh) or not (
            -100 <= item.price_pence_per_kwh <= 1_000
        ):
            raise ValueError("tariff interval has an invalid price")
    timeline = sorted(intervals, key=lambda value: value.start.astimezone(UTC))
    previous: TariffInterval | None = None
    for item in timeline:
        if item.end.astimezone(UTC) <= item.start.astimezone(UTC):
            raise ValueError("tariff interval has a non-positive duration")
        if previous is not None and item.start.astimezone(UTC) < previous.end.astimezone(UTC):
            raise ValueError("tariff intervals overlap")
        previous = item
    return timeline
