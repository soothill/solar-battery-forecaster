from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from solar_battery_forecaster.config import AppConfig, ConfigScope, load_config
from solar_battery_forecaster.operations import (
    ForecastPlanOperation,
    Operation,
    ReconciliationOperation,
    TariffOperation,
    TelemetryOperation,
)
from solar_battery_forecaster.storage import InfluxStore

WORKERS: dict[str, type[Operation]] = {
    "telemetry": TelemetryOperation,
    "tariff": TariffOperation,
    "forecast-plan": ForecastPlanOperation,
    "reconciliation": ReconciliationOperation,
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Solar forecast and battery planning workers")
    result.add_argument("command", choices=["validate", *WORKERS])
    result.add_argument("--config", default="config.yaml")
    result.add_argument("--log-level", default="INFO")
    result.add_argument("--scope", choices=[*WORKERS, "dashboard"])
    result.add_argument(
        "--once",
        action="store_true",
        help="run one due scan/cycle for diagnostics, then exit",
    )
    return result


def worker_interval(config: AppConfig, command: str) -> float:
    if command == "telemetry":
        return config.schedule.telemetry_seconds
    if command == "tariff":
        return config.schedule.tariff_minutes * 60
    return config.schedule.worker_scan_seconds


async def _run(args: argparse.Namespace) -> int:
    if args.command == "validate":
        if args.scope is None:
            raise ValueError("validate requires --scope")
        scope: ConfigScope = args.scope
    else:
        scope = args.command
    config = load_config(args.config, scope=scope)
    if args.command == "validate":
        store = InfluxStore(config.influxdb)
        try:
            if not await asyncio.to_thread(store.ping):
                raise RuntimeError("InfluxDB health check failed")
        finally:
            await asyncio.to_thread(store.close)
        print(f"configuration valid; {len(config.properties)} property/properties")
        return 0

    operation = WORKERS[args.command](config)
    try:
        if args.once:
            return 0 if await operation.run_cycle() else 1
        else:
            await operation.run_forever(worker_interval(config, args.command))
        return 0
    finally:
        await operation.close()


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True
    logging.getLogger("urllib3").disabled = True
    logging.getLogger("influxdb_client").disabled = True
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except (ValueError, RuntimeError) as exc:
        print(f"error: operation failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
