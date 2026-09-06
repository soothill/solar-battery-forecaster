import asyncio
import signal
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


@pytest.mark.asyncio
async def test_sigterm_requests_graceful_stop_and_restores_handler(monkeypatch):
    config = SimpleNamespace(properties=[], schedule=SimpleNamespace(telemetry_seconds=300))
    closed = []
    callbacks = {}
    loop = asyncio.get_running_loop()
    original = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, callback: callbacks.update({sig: callback})
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: callbacks.pop(sig))
    monkeypatch.setattr(cli, "load_config", lambda path, scope: config)
    monkeypatch.setattr(cli, "close_reporter", lambda reporter: closed.append("reporter"))

    class GracefulOperation(FailedOnceOperation):
        async def run_forever(self, interval, stopping):
            assert interval == 300 and not stopping.is_set()
            callbacks[signal.SIGTERM]()
            assert stopping.is_set()
            closed.append("cycle_finished")

        async def close(self):
            closed.append("operation")

    monkeypatch.setitem(cli.WORKERS, "telemetry", GracefulOperation)
    result = await cli._run(Namespace(command="telemetry", config="unused", once=False))
    assert result == 0
    assert closed == ["cycle_finished", "operation", "reporter"]
    assert not callbacks
    assert signal.getsignal(signal.SIGTERM) is original


@pytest.mark.asyncio
async def test_reporter_closes_even_if_operation_close_fails(monkeypatch):
    config = SimpleNamespace(properties=[], schedule=SimpleNamespace())
    closed = []

    class CloseFailure(FailedOnceOperation):
        async def close(self):
            raise RuntimeError("close failed")

    monkeypatch.setattr(cli, "load_config", lambda path, scope: config)
    monkeypatch.setitem(cli.WORKERS, "telemetry", CloseFailure)
    monkeypatch.setattr(cli, "close_reporter", lambda reporter: closed.append(True))
    with pytest.raises(RuntimeError, match="close failed"):
        await cli._run(Namespace(command="telemetry", config="unused", once=True))
    assert closed == [True]
