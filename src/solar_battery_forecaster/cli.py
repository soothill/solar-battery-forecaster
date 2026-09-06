from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from solar_battery_forecaster.config import (
    AppConfig,
    ConfigScope,
    ObservabilityConfig,
    load_config,
)
from solar_battery_forecaster.observability import close_reporter, create_reporter
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
OUTBOX_ACTIONS = ["status", "verify", "drain", "export-quarantine", "retry"]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Solar forecast and battery planning workers")
    result.add_argument("command", choices=["validate", "outbox", *WORKERS])
    result.add_argument("outbox_action", nargs="?", choices=OUTBOX_ACTIONS)
    result.add_argument("--config", default="config.yaml")
    result.add_argument("--log-level", default="INFO")
    result.add_argument("--scope", choices=[*WORKERS, "dashboard"])
    result.add_argument("--output", help="new mode-0600 path for quarantine export")
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
    if args.command in {"validate", "outbox"}:
        if args.scope is None:
            raise ValueError(f"{args.command} requires --scope")
        scope: ConfigScope = args.scope
    else:
        scope = args.command
    config = load_config(args.config, scope=scope)
    if args.command == "outbox":
        if scope == "dashboard" or config.outbox is None:
            raise ValueError("outbox commands require a writer scope")
        if args.outbox_action is None:
            raise ValueError("outbox requires an action")
        store = InfluxStore(config.influxdb, config.outbox, scope)
        try:
            outbox = store.outbox
            if outbox is None:
                raise RuntimeError("outbox is unavailable")
            if args.outbox_action == "status":
                print(outbox.status_json())
            elif args.outbox_action == "verify":
                outbox.verify()
                if outbox.status().quarantined_records:
                    raise RuntimeError("outbox verification quarantined corrupt records")
                print("outbox verified")
            elif args.outbox_action == "drain":
                print(f"delivered {store.replay(force=True)} record(s)")
            elif args.outbox_action == "retry":
                print(f"reset {outbox.retry()} pending record(s)")
            else:
                if args.output is None:
                    raise ValueError("export-quarantine requires --output")
                print(f"exported {outbox.export_quarantine(Path(args.output))} record(s)")
            return 0
        finally:
            await asyncio.to_thread(store.close)
    if args.command == "validate":
        store = InfluxStore(config.influxdb)
        try:
            if not await asyncio.to_thread(store.ping):
                raise RuntimeError("InfluxDB health check failed")
        finally:
            await asyncio.to_thread(store.close)
        print(f"configuration valid; {len(config.properties)} property/properties")
        return 0

    reporter = create_reporter(
        getattr(config, "observability", ObservabilityConfig()),
        args.command,
        [item.id for item in config.properties],
    )
    operation: Operation | None = None
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal_installed = False
    try:
        operation = WORKERS[args.command](config)
        if hasattr(operation, "reporter"):
            operation.reporter = reporter
        if args.once:
            monitored = getattr(operation, "run_monitored_cycle", operation.run_cycle)
            cycle_succeeded = await monitored()
            store = getattr(operation, "store", None)
            has_undelivered = getattr(store, "has_undelivered", None)
            undelivered = (
                await asyncio.to_thread(has_undelivered) if callable(has_undelivered) else False
            )
            return 0 if cycle_succeeded and not undelivered else 1
        else:
            # Finish the current cycle so a synchronous write is not closed underneath
            # its executor thread. Stop promptly when waiting for the next deadline.
            loop.add_signal_handler(signal.SIGTERM, stopping.set)
            signal_installed = True
            await operation.run_forever(worker_interval(config, args.command), stopping)
        return 0
    finally:
        try:
            if operation is not None:
                await operation.close()
        finally:
            close_reporter(reporter)
            if signal_installed:
                loop.remove_signal_handler(signal.SIGTERM)
                signal.signal(signal.SIGTERM, previous_sigterm)


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
