from __future__ import annotations

import httpx

from solar_battery_forecaster.adapters.forecast.open_meteo import OpenMeteoForecast
from solar_battery_forecaster.adapters.inverter.sigenergy import SigenergyCloud
from solar_battery_forecaster.adapters.protocols import (
    ForecastAdapter,
    InverterAdapter,
    TariffAdapter,
)
from solar_battery_forecaster.adapters.tariff.octopus import OctopusTariff
from solar_battery_forecaster.config import PropertyConfig
from solar_battery_forecaster.outbound import RequestPacer

FORECAST_FACTORIES = {
    "open_meteo": lambda _prop, client, pacer: OpenMeteoForecast(client, pacer),
}
INVERTER_FACTORIES = {
    "sigenergy_cloud": lambda prop, client, pacer: SigenergyCloud(
        prop.inverter, client, pacer
    ),
}
TARIFF_FACTORIES = {
    "octopus": lambda prop, client, pacer: OctopusTariff(prop.tariff, client, pacer),
}


def forecast_adapter(
    prop: PropertyConfig,
    client: httpx.AsyncClient | None = None,
    pacer: RequestPacer | None = None,
) -> ForecastAdapter:
    try:
        return FORECAST_FACTORIES[prop.forecast.adapter](prop, client, pacer)
    except KeyError as exc:
        raise ValueError(f"unsupported forecast adapter: {prop.forecast.adapter}") from exc


def inverter_adapter(
    prop: PropertyConfig,
    client: httpx.AsyncClient | None = None,
    pacer: RequestPacer | None = None,
) -> InverterAdapter:
    try:
        return INVERTER_FACTORIES[prop.inverter.adapter](prop, client, pacer)
    except KeyError as exc:
        raise ValueError(f"unsupported inverter adapter: {prop.inverter.adapter}") from exc


def tariff_adapter(
    prop: PropertyConfig,
    client: httpx.AsyncClient | None = None,
    pacer: RequestPacer | None = None,
) -> TariffAdapter:
    try:
        return TARIFF_FACTORIES[prop.tariff.adapter](prop, client, pacer)
    except KeyError as exc:
        raise ValueError(f"unsupported tariff adapter: {prop.tariff.adapter}") from exc
