from __future__ import annotations

import os
import shutil
import tracemalloc
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from influxdb_client import Point
from pydantic import ValidationError

from solar_battery_forecaster.config import OutboxConfig
from solar_battery_forecaster.models import (
    BatteryDecision,
    ForecastInterval,
    TariffInterval,
    Telemetry,
)
from solar_battery_forecaster.outbox import (
    DurableOutbox,
    OutboxCorruptionError,
    OutboxError,
    OutboxFullError,
)
from solar_battery_forecaster.storage import InfluxStore


def config(state_directory: Path, **overrides: object) -> OutboxConfig:
    return OutboxConfig(state_directory=state_directory, **overrides)


def enqueue(
    outbox: DurableOutbox,
    property_id: str,
    logical_key: str,
    payload: bytes,
) -> str:
    at = datetime(2026, 9, 5, tzinfo=UTC)
    return outbox.enqueue(
        property_id=property_id,
        org="org",
        bucket="bucket",
        logical_kind="test",
        logical_key=logical_key,
        min_timestamp=at,
        max_timestamp=at,
        payload=payload,
    )


@pytest.mark.parametrize("reserve", [1_048_576, 2_097_152, 4_194_304])
def test_collection_reserve_is_independent_of_record_size(tmp_path, reserve):
    state = tmp_path / "state"
    if reserve < 2_097_152:
        with pytest.raises(ValidationError, match="cover one maximum record"):
            config(state, collection_reserve_bytes=reserve)
        return
    outbox = DurableOutbox(state, config(state, collection_reserve_bytes=reserve), "telemetry")
    outbox.admit_collection("home")
    with pytest.raises(OutboxFullError, match="record size"):
        enqueue(outbox, "home", "large", b"x" * (outbox.config.max_record_bytes + 1))
    outbox.close()


def test_near_capacity_restart_verification_has_bounded_python_memory(tmp_path):
    state = tmp_path / "state"
    settings = config(state)
    outbox = DurableOutbox(state, settings, "telemetry")
    for index in range(110):
        enqueue(outbox, f"home-{index % 10}", str(index), b"m field=1 " + b"x" * 1_048_566)
    outbox.close()
    tracemalloc.start()
    try:
        reopened = DurableOutbox(state, settings, "telemetry")
        assert reopened.status().pending_records == 110
        delivered = 0
        while reopened.status().pending_records:
            batch = reopened.drain(lambda bucket, org, payload: None, force=True)
            assert batch > 0
            delivered += batch
        assert delivered == 110
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 8 * 1_048_576
        reopened.close()
    finally:
        tracemalloc.stop()


def test_keyset_reader_selects_exact_property_and_kind(tmp_path):
    state = tmp_path / "state"
    outbox = DurableOutbox(state, config(state), "forecast-plan")
    at = datetime(2026, 9, 5, tzinfo=UTC)
    expected = []
    try:
        for index, (property_id, kind) in enumerate([
            ("home", "forecast_snapshot"), ("other", "forecast_snapshot"),
            ("home", "battery_decision"), ("home", "forecast_snapshot"),
        ]):
            event_id = outbox.enqueue(
                property_id=property_id, org="org", bucket="bucket", logical_kind=kind,
                logical_key=str(index), min_timestamp=at, max_timestamp=at,
                payload=b"measurement value=1i 1",
            )
            if property_id == "home" and kind == "forecast_snapshot":
                expected.append(event_id)
        rows = list(outbox._queue_rows(property_id="home", logical_kind="forecast_snapshot"))
        assert [row["event_id"] for row in rows] == expected
        assert len(list(outbox._queue_rows(property_id="home"))) == 3
        assert len(list(outbox._queue_rows(logical_kind="forecast_snapshot"))) == 3
        assert len(list(outbox._queue_rows())) == 4
        assert not list(outbox._queue_rows(property_id="missing"))
    finally:
        outbox.close()


def test_fallback_commit_precedes_replay_and_ambiguous_failure_replays_whole_record(
    tmp_path: Path,
) -> None:
    outbox = DurableOutbox(tmp_path / "state", config(tmp_path / "state"), "telemetry")
    event_id = enqueue(outbox, "one", "first", b"measurement value=1i 1")
    attempts: list[str] = []

    def ambiguous(bucket: str, org: str, payload: str) -> None:
        attempts.append(payload)
        raise TimeoutError

    assert outbox.status().pending_records == 1
    assert outbox.drain(ambiguous) == 0
    assert outbox.status().pending_records == 1
    assert outbox.retry() == 1
    delivered: list[str] = []
    assert outbox.drain(lambda bucket, org, payload: delivered.append(payload), force=True) == 1
    assert delivered == attempts
    assert len(event_id) == 64
    assert outbox.status().pending_records == 0
    outbox.close()


def test_committed_record_survives_process_restart_before_delivery(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outbox = DurableOutbox(state, config(state), "telemetry")
    enqueue(outbox, "home", "one", b"m value=1i 1")
    outbox.close()

    reopened = DurableOutbox(state, config(state), "telemetry")
    delivered: list[str] = []
    assert reopened.drain(lambda bucket, org, payload: delivered.append(payload)) == 1
    assert delivered == ["m value=1i 1"]
    reopened.close()


def test_duplicate_event_is_deterministic_and_fifo_is_fair(tmp_path: Path) -> None:
    settings = config(tmp_path / "state", drain_max_records=10)
    outbox = DurableOutbox(tmp_path / "state", settings, "tariff")
    first = enqueue(outbox, "a", "a1", b"m value=1i 1")
    assert enqueue(outbox, "a", "a1", b"m value=1i 1") == first
    enqueue(outbox, "a", "a2", b"m value=2i 2")
    enqueue(outbox, "b", "b1", b"m value=3i 3")
    delivered: list[str] = []
    assert outbox.drain(lambda bucket, org, payload: delivered.append(payload)) == 3
    assert delivered == ["m value=1i 1", "m value=3i 3", "m value=2i 2"]
    outbox.close()


def test_global_failure_is_paced_and_multi_property_recovery_is_ordered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox = DurableOutbox(tmp_path / "state", config(tmp_path / "state"), "telemetry")
    enqueue(outbox, "a", "a1", b"m value=1i 1")
    enqueue(outbox, "b", "b1", b"m value=2i 2")
    now = [1_000.0]
    monkeypatch.setattr("solar_battery_forecaster.outbox.time.time", lambda: now[0])

    def outage(bucket: str, org: str, payload: str) -> None:
        raise ConnectionError

    assert outbox.drain(outage) == 0
    assert outbox.status().retry_not_before == 1_005.0
    delivered: list[str] = []
    assert outbox.drain(lambda bucket, org, payload: delivered.append(payload)) == 0
    now[0] = 1_005.0
    assert outbox.drain(lambda bucket, org, payload: delivered.append(payload)) == 2
    assert delivered == ["m value=1i 1", "m value=2i 2"]
    outbox.close()


def test_checksum_quarantine_preserves_data_and_blocks_only_its_stream(
    tmp_path: Path,
) -> None:
    outbox = DurableOutbox(tmp_path / "state", config(tmp_path / "state"), "telemetry")
    enqueue(outbox, "a", "a1", b"m value=1i 1")
    enqueue(outbox, "b", "b1", b"m value=2i 2")
    outbox.connection.execute("UPDATE queue SET payload=? WHERE property_id='a'", (b"corrupt",))
    outbox.connection.commit()
    delivered: list[str] = []
    assert outbox.drain(lambda bucket, org, payload: delivered.append(payload)) == 1
    status = outbox.status()
    assert delivered == ["m value=2i 2"]
    assert status.quarantined_records == 1
    assert status.blocked_streams == 1
    with pytest.raises(OutboxCorruptionError, match="blocked"):
        outbox.admit_collection("a")
    outbox.admit_collection("b")
    export = tmp_path / "quarantine.jsonl"
    assert outbox.export_quarantine(export) == 1
    assert oct(export.stat().st_mode & 0o777) == "0o600"
    assert "corrupt" in export.read_text(encoding="utf-8")
    outbox.close()


def test_structural_corruption_fails_closed_and_preserves_database(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    database = state / "outbox.sqlite3"
    database.write_bytes(b"not sqlite")
    database.chmod(0o600)
    with pytest.raises(OutboxCorruptionError):
        DurableOutbox(state, config(state), "telemetry")
    assert database.read_bytes() == b"not sqlite"


def test_unknown_schema_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outbox = DurableOutbox(state, config(state), "telemetry")
    outbox.connection.execute("PRAGMA user_version=99")
    outbox.close()
    with pytest.raises(OutboxCorruptionError, match="schema"):
        DurableOutbox(state, config(state), "telemetry")


def test_capacity_rejects_without_evicting_existing_records(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outbox = DurableOutbox(state, config(state, max_records=100), "telemetry")
    for index in range(100):
        enqueue(outbox, "home", str(index), f"m value={index}i {index}".encode())
    with pytest.raises(OutboxFullError, match="record limit"):
        enqueue(outbox, "home", "overflow", b"m value=101i 101")
    assert outbox.status().pending_records == 100
    outbox.close()


def test_filesystem_reserve_rejects_collection_before_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    outbox = DurableOutbox(state, config(state), "telemetry")
    reserve = outbox.config.collection_reserve_bytes
    minimum = outbox.config.filesystem_min_free_bytes
    monkeypatch.setattr(
        "solar_battery_forecaster.outbox.shutil.disk_usage",
        lambda path: shutil._ntuple_diskusage(1_000_000_000, 1, minimum + reserve - 1),
    )

    with pytest.raises(OutboxFullError, match="filesystem reserve"):
        outbox.admit_collection("home")
    assert outbox.status().pending_records == 0
    outbox.close()


def test_oversized_record_is_rejected_before_direct_delivery(tmp_path: Path) -> None:
    writer = RecordingWriter()
    store = store_with_writer(tmp_path, writer)
    at = datetime(2026, 9, 5, tzinfo=UTC)
    store.outbox.config.max_record_bytes = 16_384

    with pytest.raises(OutboxFullError, match="record reserve"):
        store._write(
            "telemetry",
            [Point("m").field("value", "x" * 17_000).time(at)],
            property_id="home",
            logical_kind="test",
            logical_key="oversized",
            min_timestamp=at,
            max_timestamp=at,
        )

    assert writer.records == []
    store.outbox.close()


def test_symbolic_link_state_directory_is_rejected(tmp_path: Path) -> None:
    real_state = tmp_path / "real-state"
    real_state.mkdir(mode=0o700)
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(real_state, target_is_directory=True)

    with pytest.raises(OutboxError, match="symbolic link"):
        DurableOutbox(linked_state, config(linked_state), "telemetry")


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_record_bytes": 16_777_217},
        {"max_records": 99},
        {"retry_base_seconds": 301, "retry_max_seconds": 300},
        {"database_max_bytes": 1_048_576, "journal_headroom_bytes": 1_048_576},
    ],
)
def test_outbox_configuration_rejects_unsafe_bounds(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OutboxConfig(**overrides)


class FailingWriter:
    def __init__(self) -> None:
        self.records: list[str] = []

    def write(self, **kwargs: object) -> None:
        self.records.append(str(kwargs["record"]))
        raise TimeoutError


class RecordingWriter:
    def __init__(self) -> None:
        self.records: list[str] = []

    def write(self, **kwargs: object) -> None:
        self.records.append(str(kwargs["record"]))


class HttpFailure(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status


class AuthFailingWriter(RecordingWriter):
    def write(self, **kwargs: object) -> None:
        super().write(**kwargs)
        raise HttpFailure(401)


def store_with_outbox(tmp_path: Path) -> InfluxStore:
    store = object.__new__(InfluxStore)
    store.config = SimpleNamespace(
        org="org",
        telemetry_bucket="telemetry",
        tariff_bucket="tariff",
        planning_bucket="planning",
    )
    store.writer = FailingWriter()
    store.outbox = DurableOutbox(tmp_path / "state", config(tmp_path / "state"), "forecast-plan")
    return store


def store_with_writer(tmp_path: Path, writer: object) -> InfluxStore:
    store = store_with_outbox(tmp_path)
    store.writer = writer
    return store


def decision(at: datetime) -> BatteryDecision:
    return BatteryDecision(
        created_at=at,
        current_soc_percent=20,
        target_soc_percent=70,
        grid_charge_kwh=5,
        raw_solar_kwh=6,
        corrected_solar_kwh=5.4,
        conservative_solar_kwh=4.3,
        expected_load_kwh=8,
        reserve_kwh=1,
        correction_factor=0.9,
        forecast_day=at.date() + timedelta(days=1),
        forecast_snapshot_id="snapshot",
        forecast_issued_at=at,
        soc_observed_at=at,
        tariff_coverage_start=at,
        tariff_coverage_stop=at + timedelta(hours=12),
        tariff_coverage_hours=12,
        reason="recommendation_only: test",
    )


def test_all_five_write_apis_fall_back_and_logical_markers_work(
    tmp_path: Path,
) -> None:
    store = store_with_outbox(tmp_path)
    at = datetime(2026, 9, 5, tzinfo=UTC)
    telemetry = Telemetry(observed_at=at, pv_power_kw=1)
    tariff = TariffInterval(at, at + timedelta(minutes=30), 7, True)
    forecast = ForecastInterval(at, at + timedelta(hours=1), 1, 1, at, "provider")
    item = decision(at)

    dispositions = [
        store.write_telemetry("home", "source", telemetry),
        store.write_tariffs("home", "octopus", [tariff]),
        store.write_forecast("home", [forecast], 0.9, 0.8),
        store.write_decision("home", item),
        store.write_daily_result("home", date(2026, 9, 4), 5, 4, 0.8, 0.9),
    ]

    assert dispositions == ["buffered"] * 5
    assert store.outbox.status().pending_records == 5
    assert store.outbox.pending("battery_decision", "home:2026-09-06")
    assert store.outbox.pending("pv_daily", "home:2026-09-04")
    store._records = lambda query: []
    assert store.decision_exists("home", date(2026, 9, 6)) is True
    assert store.daily_result_exists("home", date(2026, 9, 4)) is True
    pending = store.outbox.pending_forecasts("home", "provider", at, at + timedelta(hours=1))
    assert len(pending) == 1
    assert pending[0].raw_energy_kwh == 1
    stored_snapshot = store.complete_forecast_snapshot(
        "home", "provider", at, at + timedelta(hours=1), expected_points=1
    )
    assert stored_snapshot is not None
    assert stored_snapshot.snapshot_id == pending[0].snapshot_id
    rows = list(store.outbox.connection.execute("SELECT * FROM queue ORDER BY seq"))
    assert {row["logical_kind"] for row in rows} == {
        "telemetry",
        "tariff_batch",
        "forecast_snapshot",
        "battery_decision",
        "pv_daily",
    }
    assert all(row["payload_bytes"] == len(row["payload"]) for row in rows)
    assert all(row["worker"] == "forecast-plan" for row in rows)
    assert all(row["org"] == "org" and row["schema_version"] == 1 for row in rows)
    assert all(row["min_timestamp"] and row["max_timestamp"] for row in rows)
    assert all(len(row["checksum"]) == 64 and len(row["event_id"]) == 64 for row in rows)
    store.outbox.close()


def test_all_five_write_apis_direct_success_leave_no_payload_rows(tmp_path: Path) -> None:
    writer = RecordingWriter()
    store = store_with_writer(tmp_path, writer)
    at = datetime(2026, 9, 5, tzinfo=UTC)
    dispositions = [
        store.write_telemetry("home", "source", Telemetry(observed_at=at, pv_power_kw=1)),
        store.write_tariffs(
            "home", "octopus", [TariffInterval(at, at + timedelta(minutes=30), 7, True)]
        ),
        store.write_forecast(
            "home",
            [ForecastInterval(at, at + timedelta(hours=1), 1, 1, at, "provider")],
            0.9,
            0.8,
        ),
        store.write_decision("home", decision(at)),
        store.write_daily_result("home", date(2026, 9, 4), 5, 4, 0.8, 0.9),
    ]

    assert dispositions == ["direct"] * 5
    assert len(writer.records) == 5
    status = store.outbox.status()
    assert status.pending_records == 0
    assert status.delivered_total == 5
    assert status.last_confirmed_delivery is not None
    store.outbox.close()


def test_direct_timeout_persists_exact_attempted_payload_and_failure_state(
    tmp_path: Path,
) -> None:
    writer = FailingWriter()
    store = store_with_writer(tmp_path, writer)
    at = datetime(2026, 9, 5, tzinfo=UTC)

    store.write_telemetry("home", "source", Telemetry(observed_at=at, pv_power_kw=1))

    row = store.outbox.connection.execute("SELECT * FROM queue").fetchone()
    assert row is not None
    assert bytes(row["payload"]).decode() == writer.records[0]
    assert row["attempts"] == 1
    assert row["last_failure_class"] == "TimeoutError"
    assert store.outbox.status().retry_not_before > 0
    store.outbox.close()


def test_confirmed_direct_write_is_not_enqueued_when_counter_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = RecordingWriter()
    store = store_with_writer(tmp_path, writer)
    monkeypatch.setattr(
        store.outbox,
        "record_direct_success",
        lambda: (_ for _ in ()).throw(OSError("counter unavailable")),
    )
    at = datetime(2026, 9, 5, tzinfo=UTC)

    disposition = store.write_telemetry("home", "source", Telemetry(observed_at=at, pv_power_kw=1))

    assert disposition == "direct"
    assert len(writer.records) == 1
    assert store.outbox.status().pending_records == 0
    store.outbox.close()


def test_same_stream_backlog_cannot_be_bypassed_but_healthy_peer_can_write_direct(
    tmp_path: Path,
) -> None:
    writer = RecordingWriter()
    store = store_with_writer(tmp_path, writer)
    at = datetime(2026, 9, 5, tzinfo=UTC)
    enqueue(store.outbox, "a", "older", b"m value=1i 1")

    store.write_telemetry("a", "source", Telemetry(observed_at=at, pv_power_kw=1))
    store.write_telemetry("b", "source", Telemetry(observed_at=at, pv_power_kw=2))

    assert len(writer.records) == 1
    assert "property=b" in writer.records[0]
    assert store.outbox.status().pending_records == 2
    store.outbox.close()


def test_auth_pause_allows_capacity_safe_buffering_without_an_early_retry(
    tmp_path: Path,
) -> None:
    writer = AuthFailingWriter()
    store = store_with_writer(tmp_path, writer)
    at = datetime(2026, 9, 5, tzinfo=UTC)

    store.write_telemetry("a", "source", Telemetry(observed_at=at, pv_power_kw=1))
    store.admit_collection("b")
    store.write_telemetry("b", "source", Telemetry(observed_at=at, pv_power_kw=2))

    status = store.outbox.status()
    assert len(writer.records) == 1
    assert status.delivery_paused is True
    assert status.pending_records == 2
    assert set(status.pending_by_property) == {"a", "b"}
    assert all(item["records"] == 1 for item in status.pending_by_property.values())
    assert all(item["bytes"] > 0 for item in status.pending_by_property.values())
    assert status.oldest_pending_at is not None
    assert status.oldest_pending_age_seconds is not None
    assert status.last_failure_class == "http_401"
    assert status.last_failure_at is not None
    assert status.pause_reason == "http_401"
    assert status.database_max_bytes == store.outbox.config.database_max_bytes
    assert status.filesystem_free_bytes > 0
    store.outbox.close()


def test_direct_first_crash_window_is_explicitly_documented() -> None:
    adr = (Path(__file__).parents[1] / "docs/adr/0002-durable-influx-outbox.md").read_text(
        encoding="utf-8"
    )
    assert "before the fallback commit can lose the local copy" in adr


def test_drained_freelist_capacity_restores_admission(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outbox = DurableOutbox(
        state,
        config(
            state,
            database_max_bytes=16_777_216,
            max_record_bytes=16_384,
            collection_reserve_bytes=16_384,
            drain_max_bytes=1_048_576,
            drain_max_records=100,
        ),
        "telemetry",
    )
    payload = b'm value="' + b"x" * 15_000 + b'" 1'
    for index in range(40):
        enqueue(outbox, "home", str(index), payload + str(index).encode())
    assert outbox.drain(lambda bucket, org, record: None) == 40
    assert outbox.connection.execute("PRAGMA freelist_count").fetchone()[0] > 0
    outbox.config.database_max_bytes = (
        outbox._capacity_bytes()
        + outbox.config.journal_headroom_bytes
        + outbox.config.collection_reserve_bytes
        + 1
    )
    assert (
        outbox._database_bytes()
        + outbox.config.journal_headroom_bytes
        + outbox.config.collection_reserve_bytes
        > outbox.config.database_max_bytes
    )
    outbox.admit_collection("home")
    outbox.close()


def test_database_and_sidecars_are_private(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outbox = DurableOutbox(state, config(state), "telemetry")
    enqueue(outbox, "home", "one", b"m value=1i 1")
    assert state.stat().st_mode & 0o777 == 0o700
    for item in state.iterdir():
        assert item.stat().st_mode & 0o077 == 0
    outbox.close()
    assert os.path.exists(state / "outbox.sqlite3")
