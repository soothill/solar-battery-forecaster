from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Literal, TypeAlias

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENV_PATTERN = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
ConfigScope: TypeAlias = Literal[
    "telemetry", "tariff", "forecast-plan", "reconciliation", "dashboard"
]
SENSITIVE_PROVIDER_KEYS = {
    "api_key",
    "app_key",
    "app_secret",
    "client_secret",
    "password",
    "system_id",
    "token",
}


class InfluxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    org: str
    telemetry_bucket: str = Field(min_length=1)
    tariff_bucket: str = Field(min_length=1)
    planning_bucket: str = Field(min_length=1)
    token: str


class ScheduleConfig(BaseModel):
    telemetry_seconds: int = Field(default=300, ge=300)
    telemetry_stale_after_seconds: int = Field(default=900, ge=300)
    tariff_minutes: int = Field(default=360, ge=30)
    forecast_hour: int = Field(default=21, ge=0, le=23)
    forecast_minute: int = Field(default=30, ge=0, le=59)
    reconciliation_hour: int = Field(default=0, ge=0, le=23)
    reconciliation_minute: int = Field(default=15, ge=0, le=59)
    worker_scan_seconds: int = Field(default=60, ge=10)
    property_phase_seconds: float = Field(default=2, ge=0, le=60)
    reconciliation_catch_up_days: int = Field(default=7, ge=1, le=30)


class HttpConfig(BaseModel):
    minimum_spacing_seconds: float = Field(default=0.5, ge=0, le=60)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_seconds: float = Field(default=1, ge=0.1, le=60)
    retry_max_seconds: float = Field(default=30, ge=1, le=300)
    retry_after_max_seconds: float = Field(default=300, ge=1, le=3600)
    jitter_seconds: float = Field(default=0.5, ge=0, le=10)


class ArrayConfig(BaseModel):
    name: str
    panel_count: int = Field(gt=0)
    panel_power_w: float = Field(gt=0)
    tilt_degrees: float = Field(ge=0, le=90)
    azimuth_degrees: float = Field(ge=0, lt=360)
    performance_ratio: float = Field(default=0.84, gt=0, le=1)

    @property
    def capacity_kwp(self) -> float:
        return self.panel_count * self.panel_power_w / 1000


class InverterConfig(BaseModel):
    adapter: str = Field(default="sigenergy_cloud", min_length=1)
    rated_power_kw: float = Field(gt=0)
    region: Literal["eu", "ap", "mea", "cn", "anz", "la", "na", "jp"] = "eu"
    app_key: str | None = None
    app_secret: str | None = None
    system_id: str | None = None


class BatteryConfig(BaseModel):
    usable_capacity_kwh: float = Field(gt=0)
    minimum_soc_percent: float = Field(default=10, ge=0, le=100)
    maximum_soc_percent: float = Field(default=100, ge=0, le=100)
    reserve_kwh: float = Field(default=1, ge=0)
    max_charge_power_kw: float = Field(gt=0)
    charge_efficiency: float = Field(default=0.94, gt=0, le=1)

    @model_validator(mode="after")
    def check_soc_range(self) -> BatteryConfig:
        if self.minimum_soc_percent >= self.maximum_soc_percent:
            raise ValueError("minimum_soc_percent must be below maximum_soc_percent")
        return self


class ForecastConfig(BaseModel):
    adapter: str = Field(default="open_meteo", min_length=1)
    initial_correction_factor: float = Field(default=1.0, ge=0.25, le=2)
    conservative_multiplier: float = Field(default=0.8, gt=0, le=1)


class LoadConfig(BaseModel):
    expected_kwh_until_next_cheap_window: float = Field(default=8, ge=0)


class TariffConfig(BaseModel):
    adapter: str = Field(default="octopus", min_length=1)
    product_code: str
    tariff_code: str
    cheap_rate_threshold_pence: float = Field(default=10, ge=-100)
    stale_after_minutes: int = Field(default=480, ge=30, le=2880)


class PropertyConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    timezone: str = "Europe/London"
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    arrays: list[ArrayConfig] = Field(min_length=1)
    inverter: InverterConfig
    battery: BatteryConfig
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)
    load: LoadConfig = Field(default_factory=LoadConfig)
    tariff: TariffConfig


class AppConfig(BaseModel):
    influxdb: InfluxConfig
    http: HttpConfig = Field(default_factory=HttpConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    properties: list[PropertyConfig] = Field(min_length=1)

    @field_validator("properties")
    @classmethod
    def unique_property_ids(cls, value: list[PropertyConfig]) -> list[PropertyConfig]:
        ids = [item.id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("property IDs must be unique")
        return value


def _expand_env(value: object) -> object:
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, str) and (match := ENV_PATTERN.match(value)):
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"required environment variable {name} is not set")
        return os.environ[name]
    return value


def _strip_sensitive_provider_values(
    value: object, allowed_keys: frozenset[str] = frozenset()
) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_sensitive_provider_values(item, allowed_keys)
            for key, item in value.items()
            if key not in SENSITIVE_PROVIDER_KEYS or key in allowed_keys
        }
    if isinstance(value, list):
        return [_strip_sensitive_provider_values(item, allowed_keys) for item in value]
    return value


def _prepare_scope(raw: dict[str, object], scope: ConfigScope) -> dict[str, object]:
    prepared = deepcopy(raw)
    influx = prepared.get("influxdb")
    if not isinstance(influx, dict):
        raise ValueError("influxdb configuration must be a mapping")
    tokens = influx.pop("tokens", None)
    if not isinstance(tokens, dict) or scope not in tokens:
        raise ValueError(f"influxdb token is not configured for {scope}")
    influx["token"] = tokens[scope]

    owners = {
        "inverter": "telemetry",
        "tariff": "tariff",
        "forecast": "forecast-plan",
    }
    supported_secrets = {
        "inverter": frozenset({"app_key", "app_secret", "system_id"}),
        "tariff": frozenset(),
        "forecast": frozenset(),
    }
    properties = prepared.get("properties")
    if not isinstance(properties, list):
        raise ValueError("properties configuration must be a list")
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        for section, owner in owners.items():
            if section in prop:
                allowed = supported_secrets[section] if scope == owner else frozenset()
                prop[section] = _strip_sensitive_provider_values(prop[section], allowed)
    return prepared


def load_config(path: str | Path, scope: ConfigScope | None = None) -> AppConfig:
    with Path(path).open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    if scope is not None:
        raw = _prepare_scope(raw, scope)
    return AppConfig.model_validate(_expand_env(raw))
