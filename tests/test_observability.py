import json
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from solar_battery_forecaster.config import ObservabilityConfig, SyslogConfig
from solar_battery_forecaster.observability import (
    STATUS_MAX_BYTES,
    HealthReporter,
    RemoteSyslog,
    StatusRepository,
)
from solar_battery_forecaster.web import make_server


def observability_config(path: Path) -> ObservabilityConfig:
    return ObservabilityConfig(status_directory=path, heartbeat_seconds=30)


def test_health_projection_is_atomic_bounded_sanitized_and_group_readable(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "telemetry"
    directory.mkdir(mode=0o750)
    reporter = HealthReporter("telemetry", observability_config(directory), ["private-home"])
    delivery_status = SimpleNamespace(
            pending_records=1,
            pending_bytes=100,
            quarantined_records=0,
            blocked_streams=0,
            database_bytes=4096,
            delivery_paused=False,
            oldest_pending_at=None,
            oldest_pending_age_seconds=None,
            delivered_total=3,
            last_confirmed_delivery=None,
            filesystem_free_bytes=1_000_000,
            database_max_bytes=2_000_000,
            filesystem_min_free_bytes=100_000,
            last_failure_class="TimeoutError",
            last_failure_at=None,
            pause_reason=None,
            pending_by_property={"private-home": {"records": 1, "bytes": 100}},
    )
    reporter.start()
    try:
        for _index in range(35):
            reporter.begin_cycle()
            reporter.complete_cycle(True)
        reporter.accepted("buffered", delivery_status)
        reporter.write()
        path = directory / "status.json"
        body = path.read_bytes()
        payload = json.loads(body)
        assert len(body) <= STATUS_MAX_BYTES
        assert path.stat().st_mode & 0o777 == 0o640
        assert len(payload["events"]) == 50
        assert "private-home" not in body.decode()
        assert "pending_by_property" not in payload["outbox"]
        assert payload["last_buffered_at"] is not None
    finally:
        reporter.close()


def test_status_repository_reads_only_fixed_services_and_marks_stale(tmp_path: Path) -> None:
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    telemetry = tmp_path / "telemetry"
    telemetry.mkdir()
    (telemetry / "status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service": "telemetry",
                "heartbeat_at": "2020-01-01T00:00:00+00:00",
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "not-allowed").mkdir()
    (tmp_path / "not-allowed" / "status.json").write_text("{}", encoding="utf-8")

    result = StatusRepository(dashboard, stale_after_seconds=90).read()

    assert [item["service"] for item in result["services"]] == [
        "telemetry",
        "tariff",
        "forecast-plan",
        "reconciliation",
        "dashboard",
    ]
    assert result["services"][0]["stale"] is True


def test_status_reader_allowlists_and_resanitizes_untrusted_fields(tmp_path: Path) -> None:
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir(mode=0o750)
    telemetry = tmp_path / "telemetry"
    telemetry.mkdir(mode=0o750)
    events = [
        {
            "at": "2026-09-05T12:00:00+00:00",
            "code": "property_failed",
            "exception_class": "Invalid<script> token=very-secret",
            "message": "<script>alert(1)</script> token=very-secret " + "x" * 800,
            "unknown_event_secret": "must-not-escape",
        }
        for _index in range(60)
    ]
    path = telemetry / "status.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service": "telemetry",
                "pid": 42,
                "lifecycle": "running",
                "started_at": "2026-09-05T11:00:00+00:00",
                "heartbeat_at": "2026-09-05T12:00:00+00:00",
                "unknown_top_level_secret": "must-not-escape",
                "outbox": {
                    "pending_records": 1,
                    "pause_reason": "10 Secret Street 51.5074 serial=SIG123",
                    "unknown": "must-not-escape",
                },
                "syslog": {"enabled": False, "unknown": "must-not-escape"},
                "events": events,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o640)

    service = StatusRepository(dashboard, stale_after_seconds=90).read()["services"][0]
    serialized = json.dumps(service)

    assert len(service["events"]) == 50
    assert all(
        isinstance(event[field], str)
        for event in service["events"]
        for field in ("at", "code", "severity", "message")
    )
    assert all(len(event["message"]) <= 512 for event in service["events"])
    assert "unknown" not in serialized
    assert "must-not-escape" not in serialized
    assert "very-secret" not in serialized
    assert "<script>" not in serialized


def test_reporter_close_publishes_stopped_lifecycle(tmp_path: Path) -> None:
    directory = tmp_path / "dashboard"
    directory.mkdir(mode=0o750)
    reporter = HealthReporter("dashboard", observability_config(directory), [])
    reporter.start()
    reporter.close()
    assert json.loads((directory / "status.json").read_text())["lifecycle"] == "stopped"


def test_reporter_rejects_writable_existing_status_paths(tmp_path: Path) -> None:
    directory = tmp_path / "telemetry"
    directory.mkdir(mode=0o770)
    directory.chmod(0o770)
    with pytest.raises(RuntimeError, match="directory permissions"):
        HealthReporter("telemetry", observability_config(directory), []).start()

    directory.chmod(0o750)
    status_path = directory / "status.json"
    status_path.write_text("{}", encoding="utf-8")
    status_path.chmod(0o660)
    with pytest.raises(RuntimeError, match="status file"):
        HealthReporter("telemetry", observability_config(directory), []).start()


def test_status_reader_rejects_writable_directory_and_file(tmp_path: Path) -> None:
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir(mode=0o750)
    telemetry = tmp_path / "telemetry"
    telemetry.mkdir(mode=0o750)
    path = telemetry / "status.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service": "telemetry",
                "heartbeat_at": "2026-09-05T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o640)
    telemetry.chmod(0o770)
    reader = StatusRepository(dashboard, stale_after_seconds=90)
    assert reader.read()["services"][0]["lifecycle"] == "missing"

    telemetry.chmod(0o750)
    path.chmod(0o660)
    assert reader.read()["services"][0]["lifecycle"] == "missing"


def test_remote_syslog_queue_is_bounded_and_nonblocking() -> None:
    config = SyslogConfig(enabled=True, host="syslog.example", queue_size=10)
    states: list[dict[str, object]] = []
    sender = RemoteSyslog(config, "telemetry", states.append)

    for index in range(11):
        sender.emit("cycle_succeeded" if index % 2 else "cycle_started")

    assert sender.messages.qsize() == 10
    assert sender.dropped == 1
    assert states[-1]["dropped"] == 1


def test_sensitive_exception_text_never_reaches_status_or_syslog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "telemetry"
    directory.mkdir(mode=0o750)
    config = ObservabilityConfig(
        status_directory=directory,
        syslog=SyslogConfig(enabled=True, host="syslog.example", queue_size=10),
    )
    reporter = HealthReporter("telemetry", config, ["private-home"])
    monkeypatch.setattr(reporter.syslog, "start", lambda: None)
    reporter.start()
    sensitive = (
        "10 Secret Street 51.5074,-0.1278 serial=SIG123 account=A-999 "
        "authorization=BearerRaw raw_provider_payload={customer:data}"
    )
    reporter.property_failed(RuntimeError(sensitive))
    reporter.accepted("buffered")
    reporter.write()
    status_bytes = (directory / "status.json").read_bytes()
    syslog_bytes = b"".join(reporter.syslog.messages.queue)
    for value in [
        b"Secret Street",
        b"51.5074",
        b"SIG123",
        b"A-999",
        b"BearerRaw",
        b"customer:data",
    ]:
        assert value not in status_bytes
        assert value not in syslog_bytes
    reporter.close()


def test_collection_path_reporting_never_calls_slow_or_failing_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "telemetry"
    directory.mkdir(mode=0o750)
    reporter = HealthReporter("telemetry", observability_config(directory), [])

    def slow_failure() -> None:
        time.sleep(0.2)
        raise OSError("unavailable")

    monkeypatch.setattr(reporter, "write", slow_failure)
    started = time.monotonic()
    reporter.begin_cycle()
    reporter.accepted("direct")
    reporter.property_failed(RuntimeError("arbitrary provider response"))
    reporter.complete_cycle(False)
    assert time.monotonic() - started < 0.05


def test_status_write_serializes_cached_snapshot_without_querying_it_again(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "telemetry"
    directory.mkdir(mode=0o750)
    reporter = HealthReporter("telemetry", observability_config(directory), [])

    class CountingSnapshot:
        reads = 0

        def __getattr__(self, name: str) -> object:
            self.reads += 1
            return None

    snapshot = CountingSnapshot()
    reporter.accepted("buffered", snapshot)
    reads_after_accept = snapshot.reads
    reporter.write()
    assert snapshot.reads == reads_after_accept


@pytest.mark.parametrize("host", [None, "bad host", "bad/path", "-bad.example"])
def test_enabled_syslog_rejects_invalid_destination(host: str | None) -> None:
    with pytest.raises(ValidationError, match="valid host"):
        SyslogConfig(enabled=True, host=host)


def test_tls_is_the_default_when_syslog_is_enabled() -> None:
    config = SyslogConfig(enabled=True, host="syslog.example")
    assert config.transport == "tls"


def test_dashboard_exposes_no_store_status_api(tmp_path: Path) -> None:
    config = SimpleNamespace(
        properties=[],
        observability=SimpleNamespace(
            status_directory=tmp_path / "dashboard", stale_after_seconds=90
        ),
    )
    status = SimpleNamespace(read=lambda: {"schema_version": 1, "services": []})
    server = make_server(
        config,
        "127.0.0.1",
        0,
        repository=SimpleNamespace(close=lambda: None),
        status_repository=status,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback test server only
            f"http://127.0.0.1:{server.server_port}/api/status", timeout=2
        ) as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert json.load(response) == {"schema_version": 1, "services": []}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
