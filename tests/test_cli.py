from argparse import Namespace
from types import SimpleNamespace

import pytest

from solar_battery_forecaster import cli


class FailedOnceOperation:
    def __init__(self, config: object) -> None:
        self.config = config
        self.closed = False

    async def run_cycle(self) -> bool:
        return False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_once_returns_nonzero_when_cycle_has_any_failure(monkeypatch) -> None:
    config = SimpleNamespace(properties=[], schedule=SimpleNamespace())
    created: list[FailedOnceOperation] = []

    def operation_factory(value: object) -> FailedOnceOperation:
        operation = FailedOnceOperation(value)
        created.append(operation)
        return operation

    monkeypatch.setattr(cli, "load_config", lambda path, scope: config)
    monkeypatch.setitem(cli.WORKERS, "telemetry", operation_factory)

    result = await cli._run(
        Namespace(
            command="telemetry",
            config="unused.yaml",
            scope=None,
            once=True,
        )
    )

    assert result == 1
    assert created[0].closed is True
