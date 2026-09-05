from __future__ import annotations

import base64
import json
import math
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from solar_battery_forecaster.config import InverterConfig
from solar_battery_forecaster.models import Telemetry
from solar_battery_forecaster.outbound import RequestPacer, default_pacer

REGION_URLS = {
    "eu": "https://openapi-eu.sigencloud.com",
    "ap": "https://openapi-apac.sigencloud.com",
    "mea": "https://openapi-eu.sigencloud.com",
    "cn": "https://openapi-cn.sigencloud.com",
    "anz": "https://openapi-aus.sigencloud.com",
    "la": "https://openapi-us.sigencloud.com",
    "na": "https://openapi-us.sigencloud.com",
    "jp": "https://openapi-jp.sigencloud.com",
}


class SigenergyError(RuntimeError):
    pass


class SigenergyCloud:
    """Small client for the official Sigenergy developer AppKey flow.

    The OpenAPI requires developer approval. Read-only collection is intentionally
    separate from future control support.
    """

    name = "sigenergy_cloud"

    def __init__(
        self,
        config: InverterConfig,
        client: httpx.AsyncClient | None = None,
        pacer: RequestPacer | None = None,
    ) -> None:
        if not config.app_key or not config.app_secret or not config.system_id:
            raise ValueError("Sigenergy credentials are required by the telemetry worker")
        self.config = config
        self.base_url = REGION_URLS[config.region]
        self._client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expiry = 0.0
        self._pacer = pacer or default_pacer()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _authenticate(self) -> None:
        encoded = base64.b64encode(
            f"{self.config.app_key}:{self.config.app_secret}".encode()
        ).decode()
        response = await self._pacer.request(
            self._client,
            "POST",
            f"{self.base_url}/openapi/auth/login/key",
            service="inverter provider",
            json={"key": encoded},
        )
        payload = response.json()
        self._check(payload)
        data = self._decode_data(payload.get("data", {}))
        if not isinstance(data, dict) or not isinstance(data.get("accessToken"), str):
            raise SigenergyError("Sigenergy authentication response was invalid")
        expires_in = float(data.get("expiresIn", 43199))
        if not math.isfinite(expires_in) or not 60 <= expires_in <= 604_800:
            raise SigenergyError("Sigenergy authentication expiry was invalid")
        self._token = data["accessToken"]
        self._token_expiry = time.time() + expires_in

    async def _get(self, path: str) -> dict[str, Any]:
        if not self._token or time.time() >= self._token_expiry - 600:
            await self._authenticate()
        response = await self._pacer.request(
            self._client,
            "GET",
            f"{self.base_url}{path}",
            service="inverter provider",
            headers={"Authorization": f"Bearer {self._token}"},
            params={"systemId": self.config.system_id},
        )
        payload = response.json()
        self._check(payload)
        data = self._decode_data(payload.get("data", {}))
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _decode_data(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @staticmethod
    def _check(payload: dict[str, Any]) -> None:
        code = payload.get("code", -1)
        if code != 0:
            raise SigenergyError(f"Sigenergy API returned error code {code}")

    async def collect(self) -> Telemetry:
        system_id = self.config.system_id
        if system_id is None:
            raise SigenergyError("Sigenergy system identifier is unavailable")
        flow = await self._get(f"/openapi/systems/{system_id}/energyFlow")
        summary = await self._get(f"/openapi/systems/{system_id}/summary")
        item = normalize_telemetry(flow, summary)
        validate_telemetry(item, self.config.rated_power_kw)
        return item


def _number(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_telemetry(flow: dict[str, Any], summary: dict[str, Any]) -> Telemetry:
    return Telemetry(
        observed_at=datetime.now(UTC),
        pv_power_kw=_number(flow, "pvPower"),
        grid_power_kw=_number(flow, "gridPower"),
        battery_power_kw=_number(flow, "batteryPower"),
        load_power_kw=_number(flow, "loadPower"),
        battery_soc_percent=_number(flow, "batterySoc"),
        daily_pv_kwh=_number(summary, "dailyPowerGeneration"),
        lifetime_pv_kwh=_number(summary, "lifetimePowerGeneration"),
    )


def validate_telemetry(item: Telemetry, inverter_power_kw: float) -> None:
    """Reject values that could poison recommendations or conceal a broken payload."""
    values = {
        "pv_power_kw": item.pv_power_kw,
        "grid_power_kw": item.grid_power_kw,
        "battery_power_kw": item.battery_power_kw,
        "load_power_kw": item.load_power_kw,
        "battery_soc_percent": item.battery_soc_percent,
        "daily_pv_kwh": item.daily_pv_kwh,
        "lifetime_pv_kwh": item.lifetime_pv_kwh,
    }
    for name, value in values.items():
        if value is not None and not math.isfinite(value):
            raise SigenergyError(f"invalid non-finite telemetry field: {name}")
    if item.battery_soc_percent is not None and not 0 <= item.battery_soc_percent <= 100:
        raise SigenergyError("battery state of charge is outside 0-100%")
    if item.pv_power_kw is not None and not 0 <= item.pv_power_kw <= inverter_power_kw * 1.25:
        raise SigenergyError("PV power is outside the configured inverter limit")
    if item.battery_power_kw is not None and abs(item.battery_power_kw) > inverter_power_kw * 1.25:
        raise SigenergyError("battery power is outside the configured inverter limit")
    if item.load_power_kw is not None and item.load_power_kw < 0:
        raise SigenergyError("load power cannot be negative")
    if item.daily_pv_kwh is not None and item.daily_pv_kwh < 0:
        raise SigenergyError("daily PV energy cannot be negative")
    if item.lifetime_pv_kwh is not None and item.lifetime_pv_kwh < 0:
        raise SigenergyError("lifetime PV energy cannot be negative")
