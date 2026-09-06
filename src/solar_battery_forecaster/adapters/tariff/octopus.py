from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

import httpx

from solar_battery_forecaster.config import TariffConfig
from solar_battery_forecaster.models import TariffInterval
from solar_battery_forecaster.outbound import RequestPacer, default_pacer
from solar_battery_forecaster.tariffs import validated_tariff_timeline

BASE_URL = "https://api.octopus.energy/v1"
MAX_TARIFF_INTERVALS = 100
MAX_TARIFF_PAGES = 10


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
        start = start or datetime.now(UTC)
        if start.utcoffset() is None:
            raise ValueError("tariff horizon must be timezone-aware")
        start = start.astimezone(UTC)
        end = start + timedelta(days=2)
        query_start = start - timedelta(hours=2)
        url = (
            f"{BASE_URL}/products/{self.config.product_code}/electricity-tariffs/"
            f"{self.config.tariff_code}/standard-unit-rates/"
        )
        params = {
                "period_from": query_start.isoformat().replace("+00:00", "Z"),
                "period_to": end.isoformat().replace("+00:00", "Z"),
                "page_size": 100,
            }
        rows = []
        next_url = url
        seen = set()
        endpoint = urlsplit(url)
        for page in range(MAX_TARIFF_PAGES):
            if next_url in seen:
                raise ValueError("tariff provider returned cyclic pagination")
            seen.add(next_url)
            payload = await self._pacer.request_json(
                self._client, "GET", next_url, service="tariff provider",
                **({"params": params} if page == 0 else {}),
            )
            if not isinstance(payload, dict):
                raise ValueError("tariff provider returned an invalid response")
            page_rows = payload.get("results", [])
            if not isinstance(page_rows, list) or len(page_rows) > MAX_TARIFF_INTERVALS:
                raise ValueError("tariff provider returned an invalid interval list")
            rows.extend(page_rows)
            following = payload.get("next")
            if following is None:
                break
            if not isinstance(following, str) or len(following) > 4096:
                raise ValueError("tariff provider returned invalid pagination")
            next_url = urljoin(url, following)
            target = urlsplit(next_url)
            if (
                target.scheme != endpoint.scheme or target.netloc != endpoint.netloc
                or target.path != endpoint.path or target.fragment
            ):
                raise ValueError("tariff provider returned invalid pagination endpoint")
        else:
            raise ValueError("tariff provider exceeded pagination limit")
        intervals: list[TariffInterval] = []
        for row in rows:
            price = float(row["value_inc_vat"])
            interval_start = datetime.fromisoformat(row["valid_from"].replace("Z", "+00:00"))
            interval_end = (
                end if row["valid_to"] is None
                else datetime.fromisoformat(row["valid_to"].replace("Z", "+00:00"))
            )
            if interval_start.utcoffset() is None or interval_end.utcoffset() is None:
                raise ValueError("tariff provider returned a timezone-naive interval")
            interval_start = interval_start.astimezone(UTC)
            interval_end = interval_end.astimezone(UTC)
            if not math.isfinite(price) or not -100 <= price <= 1_000:
                raise ValueError("tariff provider returned an invalid price")
            if interval_end <= interval_start:
                raise ValueError("tariff provider returned an invalid interval")
            interval_start = max(interval_start, query_start)
            interval_end = min(interval_end, end)
            if interval_end <= interval_start:
                continue
            intervals.append(
                TariffInterval(
                    start=interval_start,
                    end=interval_end,
                    price_pence_per_kwh=price,
                    is_cheap=price <= self.config.cheap_rate_threshold_pence,
                )
            )
        return validated_tariff_timeline(intervals)
