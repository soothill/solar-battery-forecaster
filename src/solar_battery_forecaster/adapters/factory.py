from __future__ import annotations

from collections.abc import Callable

from solar_battery_forecaster.adapters.forecast.open_meteo import OpenMeteoForecast
from solar_battery_forecaster.adapters.inverter.sigenergy import SigenergyCloud
from solar_battery_forecaster.adapters.protocols import (
    ForecastAdapter,
    InverterAdapter,
    TariffAdapter,
)
from solar_battery_forecaster.adapters.tariff.octopus import OctopusTariff
from solar_battery_forecaster.config import PropertyConfig

FORECAST_FACTORIES: dict[str, Callable[[PropertyConfig], ForecastAdapter]] = {
    "open_meteo": lambda _prop: OpenMeteoForecast(),
}
INVERTER_FACTORIES: dict[str, Callable[[PropertyConfig], InverterAdapter]] = {
    "sigenergy_cloud": lambda prop: SigenergyCloud(prop.inverter),
}
TARIFF_FACTORIES: dict[str, Callable[[PropertyConfig], TariffAdapter]] = {
    "octopus": lambda prop: OctopusTariff(prop.tariff),
}


def forecast_adapter(prop: PropertyConfig) -> ForecastAdapter:
    try:
        return FORECAST_FACTORIES[prop.forecast.adapter](prop)
    except KeyError as exc:
        raise ValueError(f"unsupported forecast adapter: {prop.forecast.adapter}") from exc


def inverter_adapter(prop: PropertyConfig) -> InverterAdapter:
    try:
        return INVERTER_FACTORIES[prop.inverter.adapter](prop)
    except KeyError as exc:
        raise ValueError(f"unsupported inverter adapter: {prop.inverter.adapter}") from exc


def tariff_adapter(prop: PropertyConfig) -> TariffAdapter:
    try:
        return TARIFF_FACTORIES[prop.tariff.adapter](prop)
    except KeyError as exc:
        raise ValueError(f"unsupported tariff adapter: {prop.tariff.adapter}") from exc
