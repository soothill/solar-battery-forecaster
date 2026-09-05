from __future__ import annotations

from solar_battery_forecaster.models import TariffInterval


def validated_tariff_timeline(
    intervals: list[TariffInterval],
) -> list[TariffInterval]:
    """Return one ordered timeline, rejecting ambiguous or overlapping intervals."""
    timeline = sorted(intervals, key=lambda value: (value.start, value.end))
    previous: TariffInterval | None = None
    for item in timeline:
        if item.end <= item.start:
            raise ValueError("tariff interval has a non-positive duration")
        if previous is not None and item.start < previous.end:
            raise ValueError("tariff intervals overlap")
        previous = item
    return timeline
