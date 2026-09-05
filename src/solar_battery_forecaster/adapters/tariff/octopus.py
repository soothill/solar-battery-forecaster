from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import httpx

from solar_battery_forecaster.config import TariffConfig
from solar_battery_forecaster.models import TariffInterval
from solar_battery_forecaster.outbound import RequestPacer, default_pacer

BASE_URL = "https://api.octopus.energy/v1"
MAX_TARIFF_INTERVALS = 100


class OctopusTariff:
    name = "octopus"

    def __init__(
        self,
        config: TariffConfig,
        client: httpx.AsyncClient | None = None,
        pacer: RequestPacer | None = None,
    ) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None
        self._pacer = pacer or default_pacer()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, start: datetime | None = None) -> list[TariffInterval]:
        start = (start or datetime.now(UTC)).astimezone(UTC)
        end = start + timedelta(days=2)
        query_start = start - timedelta(hours=2)
        url = (
            f"{BASE_URL}/products/{self.config.product_code}/electricity-tariffs/"
            f"{self.config.tariff_code}/standard-unit-rates/"
        )
        response = await self._pacer.request(
            self._client,
            "GET",
            url,
            service="tariff provider",
            params={
                "period_from": query_start.isoformat().replace("+00:00", "Z"),
                "period_to": end.isoformat().replace("+00:00", "Z"),
                "page_size": 100,
            },
        )
        rows = response.json().get("results", [])
        if not isinstance(rows, list) or len(rows) > MAX_TARIFF_INTERVALS:
            raise ValueError("tariff provider returned an invalid interval list")
        intervals: list[TariffInterval] = []
        for row in rows:
            price = float(row["value_inc_vat"])
            interval_start = datetime.fromisoformat(row["valid_from"].replace("Z", "+00:00"))
            interval_end = datetime.fromisoformat(row["valid_to"].replace("Z", "+00:00"))
            duration = interval_end - interval_start
            if not math.isfinite(price) or not -100 <= price <= 1_000:
                raise ValueError("tariff provider returned an invalid price")
            if duration <= timedelta(0) or duration > timedelta(hours=2):
                raise ValueError("tariff provider returned an invalid interval")
            intervals.append(
                TariffInterval(
                    start=interval_start,
                    end=interval_end,
                    price_pence_per_kwh=price,
                    is_cheap=price <= self.config.cheap_rate_threshold_pence,
                )
            )
        return sorted(intervals, key=lambda item: item.start)
