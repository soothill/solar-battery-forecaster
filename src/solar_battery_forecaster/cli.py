from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from solar_battery_forecaster.config import load_config
from solar_battery_forecaster.service import CollectorService


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Solar forecast and battery charge collector")
    result.add_argument("command", choices=["validate", "collect-once", "run"])
    result.add_argument("--config", default="config.yaml")
    result.add_argument("--log-level", default="INFO")
    return result


async def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    service = CollectorService(config)
    try:
        if args.command == "validate":
            if not await asyncio.to_thread(service.store.ping):
                raise RuntimeError("InfluxDB did not answer its health check")
            count = len(config.properties)
            print(f"configuration valid; InfluxDB reachable; {count} property/properties")
            return 0
        if args.command == "collect-once":
            await service.collect_once()
            return 0
        await service.run()
        return 0
    finally:
        await service.close()


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
