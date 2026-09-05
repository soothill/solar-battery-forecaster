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


class BufferedOnceOperation(FailedOnceOperation):
    def __init__(self, config: object) -> None:
        super().__init__(config)
        self.store = SimpleNamespace(has_undelivered=lambda: True)

    async def run_cycle(self) -> bool:
        return True


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


@pytest.mark.asyncio
async def test_once_returns_nonzero_when_delivery_remains_buffered(monkeypatch) -> None:
    config = SimpleNamespace(properties=[], schedule=SimpleNamespace())
    created: list[BufferedOnceOperation] = []

    def operation_factory(value: object) -> BufferedOnceOperation:
        operation = BufferedOnceOperation(value)
        created.append(operation)
        return operation

    monkeypatch.setattr(cli, "load_config", lambda path, scope: config)
    monkeypatch.setitem(cli.WORKERS, "telemetry", operation_factory)

    result = await cli._run(
        Namespace(command="telemetry", config="unused.yaml", scope=None, once=True)
    )

    assert result == 1
    assert created[0].closed is True


@pytest.mark.asyncio
async def test_once_test_double_without_delivery_status_can_succeed(monkeypatch) -> None:
    config = SimpleNamespace(properties=[], schedule=SimpleNamespace())

    class SuccessfulOnceOperation(FailedOnceOperation):
        async def run_cycle(self) -> bool:
            return True

    monkeypatch.setattr(cli, "load_config", lambda path, scope: config)
    monkeypatch.setitem(cli.WORKERS, "telemetry", SuccessfulOnceOperation)

    result = await cli._run(
        Namespace(command="telemetry", config="unused.yaml", scope=None, once=True)
    )

    assert result == 0


def test_outbox_operator_parser_requires_explicit_scope_and_action() -> None:
    args = cli.parser().parse_args(
        ["outbox", "export-quarantine", "--scope", "telemetry", "--output", "/tmp/export"]
    )
    assert args.command == "outbox"
    assert args.outbox_action == "export-quarantine"
    assert args.scope == "telemetry"
