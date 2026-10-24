from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from solar_battery_forecaster.config import PropertyConfig
from solar_battery_forecaster.models import (
    ForecastInterval,
    HistoricalIrradianceInterval,
    TariffInterval,
    Telemetry,
)


class IrradianceHistoryAdapter(Protocol):
    name: str
    model: str

    async def fetch(
        self, prop: PropertyConfig, day: date
    ) -> list[HistoricalIrradianceInterval]: ...

    async def close(self) -> None: ...


class ForecastAdapter(Protocol):
    name: str

    async def fetch(self, prop: PropertyConfig) -> list[ForecastInterval]: ...

    async def close(self) -> None: ...


class InverterAdapter(Protocol):
    name: str

    async def collect(self) -> Telemetry: ...

    async def close(self) -> None: ...


class TariffAdapter(Protocol):
    name: str

    async def fetch(self, start: datetime | None = None) -> list[TariffInterval]: ...

    async def close(self) -> None: ...
