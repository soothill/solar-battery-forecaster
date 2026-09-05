from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solar_battery_forecaster.config import OutboxConfig
from solar_battery_forecaster.models import ForecastSnapshot

SCHEMA_VERSION = 1
EVENT_DOMAIN = b"solar-battery-forecaster:influx-outbox:v1\0"
QUEUE_COLUMNS = (
    "seq",
    "event_id",
    "stream_key",
    "worker",
    "property_id",
    "org",
    "bucket",
    "schema_version",
    "logical_kind",
    "logical_key",
    "min_timestamp",
    "max_timestamp",
    "created_at",
    "payload",
    "payload_bytes",
    "checksum",
    "metadata_json",
    "metadata_checksum",
    "attempts",
    "last_failure_class",
)


class OutboxError(RuntimeError):
    pass


class OutboxFullError(OutboxError):
    pass


class OutboxCorruptionError(OutboxError):
    pass


@dataclass(frozen=True)
class OutboxStatus:
    pending_records: int
    pending_bytes: int
    quarantined_records: int
    blocked_streams: int
    database_bytes: int
    retry_not_before: float
    delivery_paused: bool
    pending_by_property: dict[str, dict[str, int]]
    oldest_pending_at: str | None
    oldest_pending_age_seconds: float | None
    delivered_total: int
    last_confirmed_delivery: str | None
    filesystem_free_bytes: int
    database_max_bytes: int
    max_records: int
    filesystem_min_free_bytes: int
    journal_headroom_bytes: int
    collection_reserve_bytes: int
    last_failure_class: str | None
    last_failure_at: str | None
    pause_reason: str | None


class DurableOutbox:
    def __init__(self, state_directory: Path, config: OutboxConfig, worker: str) -> None:
        self.config = config
        self.worker = worker
        self.state_directory = state_directory
        if state_directory.is_symlink():
            raise OutboxError("outbox state directory must not be a symbolic link")
        created = not state_directory.exists()
        state_directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if created:
            state_directory.chmod(0o700)
        if state_directory.stat().st_mode & 0o077:
            raise OutboxError("outbox state directory permissions must be 0700")
        self.path = state_directory / "outbox.sqlite3"
        if self.path.is_symlink():
            raise OutboxError("outbox database must not be a symbolic link")
        if self.path.exists() and self.path.stat().st_mode & 0o077:
            raise OutboxError("outbox database permissions must be 0600")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_mode & 0o077):
                raise OutboxError("outbox sidecar permissions are unsafe")
        old_umask = os.umask(0o077)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            self.connection = connection
            self.path.chmod(0o600)
            self.connection.row_factory = sqlite3.Row
            self._configure()
            self._migrate()
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.path}{suffix}")
                if sidecar.exists():
                    sidecar.chmod(0o600)
        except (sqlite3.DatabaseError, OutboxCorruptionError) as exc:
            if connection is not None:
                connection.close()
            if isinstance(exc, OutboxCorruptionError):
                raise
            raise OutboxCorruptionError("unable to open outbox database") from exc
        finally:
            os.umask(old_umask)
        try:
            self.verify()
        except sqlite3.DatabaseError as exc:
            self.connection.close()
            raise OutboxCorruptionError("outbox database failed structural validation") from exc

    def _configure(self) -> None:
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA secure_delete=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA wal_autocheckpoint=100")

    def _migrate(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise OutboxCorruptionError("unsupported outbox schema version")
        if version == SCHEMA_VERSION:
            return
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE streams (
                  stream_key TEXT PRIMARY KEY,
                  state TEXT NOT NULL DEFAULT 'ready',
                  reason_class TEXT,
                  CHECK (state IN ('ready', 'blocked'))
                );
                CREATE TABLE queue (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id TEXT UNIQUE NOT NULL,
                  stream_key TEXT NOT NULL REFERENCES streams(stream_key),
                  worker TEXT NOT NULL,
                  property_id TEXT NOT NULL,
                  org TEXT NOT NULL,
                  bucket TEXT NOT NULL,
                  schema_version INTEGER NOT NULL,
                  logical_kind TEXT NOT NULL,
                  logical_key TEXT NOT NULL,
                  min_timestamp TEXT NOT NULL,
                  max_timestamp TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  payload BLOB NOT NULL,
                  payload_bytes INTEGER NOT NULL,
                  checksum TEXT NOT NULL,
                  metadata_json TEXT NOT NULL,
                  metadata_checksum TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  last_failure_class TEXT
                );
                CREATE INDEX queue_stream_seq ON queue(stream_key, seq);
                CREATE INDEX queue_logical ON queue(logical_kind, logical_key);
                CREATE TABLE quarantine AS SELECT *, '' AS quarantined_at, '' AS reason
                  FROM queue WHERE 0;
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                PRAGMA user_version=1;
                """
            )

    def close(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        self.connection.close()

    def _database_bytes(self) -> int:
        return sum(
            candidate.stat().st_size
            for candidate in [self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")]
            if candidate.exists()
        )

    def _capacity_bytes(self) -> int:
        page_size = int(self.connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(self.connection.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(self.connection.execute("PRAGMA freelist_count").fetchone()[0])
        return max(0, page_count - free_pages) * page_size

    def status(self) -> OutboxStatus:
        pending_records, pending_bytes = self.connection.execute(
            "SELECT count(*), coalesce(sum(payload_bytes), 0) FROM queue"
        ).fetchone()
        quarantined = self.connection.execute("SELECT count(*) FROM quarantine").fetchone()[0]
        blocked = self.connection.execute(
            "SELECT count(*) FROM streams WHERE state='blocked'"
        ).fetchone()[0]
        retry = self._meta_float("retry_not_before")
        pending_by_property = {
            str(row["property_id"]): {
                "records": int(row["records"]),
                "bytes": int(row["bytes"]),
            }
            for row in self.connection.execute(
                """SELECT property_id, count(*) AS records,
                coalesce(sum(payload_bytes), 0) AS bytes
                FROM queue GROUP BY property_id ORDER BY property_id"""
            )
        }
        oldest = self.connection.execute("SELECT min(created_at) FROM queue").fetchone()[0]
        oldest_at = str(oldest) if oldest else None
        oldest_age = None
        if oldest_at is not None:
            oldest_age = max(
                0.0,
                (datetime.now(UTC) - datetime.fromisoformat(oldest_at)).total_seconds(),
            )
        return OutboxStatus(
            pending_records=int(pending_records),
            pending_bytes=int(pending_bytes),
            quarantined_records=int(quarantined),
            blocked_streams=int(blocked),
            database_bytes=self._database_bytes(),
            retry_not_before=retry,
            delivery_paused=self._meta("delivery_paused") == "1",
            pending_by_property=pending_by_property,
            oldest_pending_at=oldest_at,
            oldest_pending_age_seconds=oldest_age,
            delivered_total=int(self._meta("delivered_total") or 0),
            last_confirmed_delivery=self._meta("last_confirmed_delivery") or None,
            filesystem_free_bytes=shutil.disk_usage(self.state_directory).free,
            database_max_bytes=self.config.database_max_bytes,
            max_records=self.config.max_records,
            filesystem_min_free_bytes=self.config.filesystem_min_free_bytes,
            journal_headroom_bytes=self.config.journal_headroom_bytes,
            collection_reserve_bytes=self.config.collection_reserve_bytes,
            last_failure_class=self._meta("last_failure_class") or None,
            last_failure_at=self._meta("last_failure_at") or None,
            pause_reason=self._meta("pause_reason") or None,
        )

    def admit_collection(
        self, property_id: str | None = None, reserve_bytes: int | None = None
    ) -> None:
        reserve = reserve_bytes or self.config.collection_reserve_bytes
        if reserve <= 0 or reserve > self.config.max_record_bytes:
            raise OutboxFullError("outbox record reserve is invalid")
        status = self.status()
        if status.pending_records + status.quarantined_records >= self.config.max_records:
            raise OutboxFullError("outbox record limit reached")
        if property_id is not None:
            stream_key = f"{self.worker}:{property_id}"
            blocked = self.connection.execute(
                "SELECT 1 FROM streams WHERE stream_key=? AND state='blocked'", (stream_key,)
            ).fetchone()
            if blocked:
                raise OutboxCorruptionError("outbox stream is blocked")
        if self._capacity_bytes() + reserve + self.config.journal_headroom_bytes > (
            self.config.database_max_bytes
        ):
            raise OutboxFullError("outbox byte limit reached")
        free = shutil.disk_usage(self.state_directory).free
        if free - reserve < self.config.filesystem_min_free_bytes:
            raise OutboxFullError("outbox filesystem reserve would be breached")

    def enqueue(
        self,
        *,
        property_id: str,
        org: str,
        bucket: str,
        logical_kind: str,
        logical_key: str,
        min_timestamp: datetime,
        max_timestamp: datetime,
        payload: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if not payload or len(payload) > self.config.max_record_bytes:
            raise OutboxFullError("outbox record size is invalid")
        stream_key = f"{self.worker}:{property_id}"
        checksum = hashlib.sha256(payload).hexdigest()
        metadata_json = json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))
        metadata_checksum = hashlib.sha256(metadata_json.encode()).hexdigest()
        identity = json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "worker": self.worker,
                "property": property_id,
                "org": org,
                "bucket": bucket,
                "logical_kind": logical_kind,
                "logical_key": logical_key,
                "min_timestamp": min_timestamp.astimezone(UTC).isoformat(),
                "max_timestamp": max_timestamp.astimezone(UTC).isoformat(),
                "checksum": checksum,
                "metadata_checksum": metadata_checksum,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        event_id = hashlib.sha256(EVENT_DOMAIN + identity + b"\0" + payload).hexdigest()
        if self.connection.execute("SELECT 1 FROM queue WHERE event_id=?", (event_id,)).fetchone():
            return event_id
        self.admit_collection(property_id, len(payload))
        now = datetime.now(UTC).isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT OR IGNORE INTO streams(stream_key) VALUES (?)", (stream_key,)
            )
            self.connection.execute(
                """INSERT OR IGNORE INTO queue(
                event_id, stream_key, worker, property_id, org, bucket, schema_version,
                logical_kind, logical_key, min_timestamp, max_timestamp, created_at,
                payload, payload_bytes, checksum, metadata_json, metadata_checksum)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    stream_key,
                    self.worker,
                    property_id,
                    org,
                    bucket,
                    SCHEMA_VERSION,
                    logical_kind,
                    logical_key,
                    min_timestamp.astimezone(UTC).isoformat(),
                    max_timestamp.astimezone(UTC).isoformat(),
                    now,
                    payload,
                    len(payload),
                    checksum,
                    metadata_json,
                    metadata_checksum,
                ),
            )
            self.connection.commit()
        except (OSError, sqlite3.DatabaseError) as exc:
            self.connection.rollback()
            raise OutboxError("unable to persist outbox record") from exc
        return event_id

    def can_attempt_direct(self, property_id: str) -> bool:
        if self._meta("delivery_paused") == "1":
            return False
        if time.time() < self._meta_float("retry_not_before"):
            return False
        stream_key = f"{self.worker}:{property_id}"
        if self.connection.execute(
            "SELECT 1 FROM streams WHERE stream_key=? AND state='blocked'", (stream_key,)
        ).fetchone():
            return False
        queued = self.connection.execute(
            "SELECT 1 FROM queue WHERE stream_key=? LIMIT 1", (stream_key,)
        ).fetchone()
        quarantined = self.connection.execute(
            "SELECT 1 FROM quarantine WHERE stream_key=? LIMIT 1", (stream_key,)
        ).fetchone()
        return queued is None and quarantined is None

    def record_failure(self, event_id: str, exc: Exception) -> str:
        row = self.connection.execute(
            "SELECT * FROM queue WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise OutboxError("cannot record failure for unknown outbox event")
        attempts = int(row["attempts"]) + 1
        status_code = self._status_code(exc)
        failure_class = f"http_{status_code}" if status_code is not None else type(exc).__name__
        if status_code in {401, 403}:
            with self.connection:
                self.connection.execute(
                    "UPDATE queue SET attempts=?, last_failure_class=? WHERE seq=?",
                    (attempts, failure_class, row["seq"]),
                )
                self._set_meta("delivery_paused", "1")
                self._set_meta("pause_reason", failure_class)
                self._record_last_failure(failure_class)
            return "worker_paused"
        if (
            status_code is not None
            and 400 <= status_code < 500
            and status_code not in {408, 425, 429}
        ):
            with self.connection:
                self.connection.execute(
                    "UPDATE queue SET attempts=?, last_failure_class=? WHERE seq=?",
                    (attempts, failure_class, row["seq"]),
                )
                self.connection.execute(
                    "UPDATE streams SET state='blocked', reason_class=? WHERE stream_key=?",
                    (failure_class, row["stream_key"]),
                )
                self._set_meta("last_stream", str(row["stream_key"]))
                self._record_last_failure(failure_class)
            return "stream_blocked"
        delay = min(
            self.config.retry_max_seconds,
            self.config.retry_base_seconds * (2 ** min(attempts - 1, 16)),
        )
        with self.connection:
            self.connection.execute(
                "UPDATE queue SET attempts=?, last_failure_class=? WHERE seq=?",
                (attempts, failure_class, row["seq"]),
            )
            self._set_meta("retry_not_before", str(time.time() + delay))
            self._record_last_failure(failure_class)
        return "retry_later"

    def record_direct_success(self) -> None:
        with self.connection:
            self._record_confirmed_delivery()

    def _record_confirmed_delivery(self) -> None:
        delivered_total = int(self._meta("delivered_total") or 0) + 1
        self._set_meta("delivered_total", str(delivered_total))
        self._set_meta("last_confirmed_delivery", datetime.now(UTC).isoformat())

    def _record_last_failure(self, failure_class: str) -> None:
        self._set_meta("last_failure_class", failure_class)
        self._set_meta("last_failure_at", datetime.now(UTC).isoformat())

    def _heads(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """SELECT q.* FROM queue q JOIN streams s USING(stream_key)
                WHERE s.state='ready' AND NOT EXISTS (
                  SELECT 1 FROM queue older
                  WHERE older.stream_key=q.stream_key AND older.seq < q.seq)
                ORDER BY q.stream_key"""
            )
        )

    def drain(self, deliver: Callable[[str, str, str], None], force: bool = False) -> int:
        if self._meta("delivery_paused") == "1":
            return 0
        if not force and time.time() < self._meta_float("retry_not_before"):
            return 0
        delivered = 0
        delivered_bytes = 0
        last_stream = self._meta("last_stream")
        while delivered < self.config.drain_max_records:
            heads = self._heads()
            if not heads:
                break
            ordered = [row for row in heads if row["stream_key"] > last_stream]
            ordered.extend(row for row in heads if row["stream_key"] <= last_stream)
            row = ordered[0]
            payload = bytes(row["payload"])
            if delivered and delivered_bytes + len(payload) > self.config.drain_max_bytes:
                break
            if not self._row_checksums_valid(row):
                self._quarantine(row, "checksum_mismatch")
                last_stream = str(row["stream_key"])
                self._set_meta("last_stream", last_stream)
                continue
            try:
                deliver(str(row["bucket"]), str(row["org"]), payload.decode("utf-8"))
            except Exception as exc:
                disposition = self.record_failure(str(row["event_id"]), exc)
                if disposition == "stream_blocked":
                    last_stream = str(row["stream_key"])
                    continue
                break
            with self.connection:
                self.connection.execute("DELETE FROM queue WHERE seq=?", (row["seq"],))
                self._set_meta("retry_not_before", "0")
                self._set_meta("last_stream", str(row["stream_key"]))
                self._record_confirmed_delivery()
            delivered += 1
            delivered_bytes += len(payload)
            last_stream = str(row["stream_key"])
        return delivered

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        value = getattr(exc, "status", None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _quarantine(self, row: sqlite3.Row, reason: str) -> None:
        values = [row[column] for column in QUEUE_COLUMNS]
        with self.connection:
            self.connection.execute(
                """INSERT INTO quarantine(
                seq, event_id, stream_key, worker, property_id, org, bucket, schema_version,
                logical_kind, logical_key, min_timestamp, max_timestamp, created_at, payload,
                payload_bytes, checksum, metadata_json, metadata_checksum, attempts,
                last_failure_class, quarantined_at, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [*values, datetime.now(UTC).isoformat(), reason],
            )
            self.connection.execute("DELETE FROM queue WHERE seq=?", (row["seq"],))
            self.connection.execute(
                "UPDATE streams SET state='blocked', reason_class=? WHERE stream_key=?",
                (reason, row["stream_key"]),
            )

    def pending(self, logical_kind: str, logical_key: str) -> bool:
        query = "SELECT 1 FROM {} WHERE logical_kind=? AND logical_key=? LIMIT 1"
        return any(
            self.connection.execute(query.format(table), (logical_kind, logical_key)).fetchone()
            for table in ("queue", "quarantine")
        )

    def pending_forecasts(
        self, property_id: str, provider: str, start: datetime, stop: datetime
    ) -> list[ForecastSnapshot]:
        rows = list(
            self.connection.execute(
                """SELECT * FROM queue
            WHERE property_id=? AND logical_kind='forecast_snapshot'""",
                (property_id,),
            )
        )
        result: list[ForecastSnapshot] = []
        for row in rows:
            if not self._row_checksums_valid(row):
                self._quarantine(row, "checksum_mismatch")
                continue
            metadata = json.loads(row["metadata_json"])
            if (
                metadata.get("provider") != provider
                or metadata.get("forecast_start") != start.astimezone(UTC).isoformat()
                or metadata.get("forecast_stop") != stop.astimezone(UTC).isoformat()
            ):
                continue
            result.append(
                ForecastSnapshot(
                    provider=provider,
                    snapshot_id=str(metadata["snapshot_id"]),
                    issued_at=datetime.fromisoformat(str(metadata["issued_at"])),
                    point_count=int(metadata["point_count"]),
                    raw_energy_kwh=float(metadata["raw_energy_kwh"]),
                    correction_factor=float(metadata["correction_factor"]),
                )
            )
        return result

    def verify(self) -> int:
        result = self.connection.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise OutboxCorruptionError("outbox database quick_check failed")
        quarantined = 0
        rows = list(self.connection.execute("SELECT * FROM queue"))
        for row in rows:
            if not self._row_checksums_valid(row):
                self._quarantine(row, "checksum_mismatch")
                quarantined += 1
        return quarantined

    @staticmethod
    def _row_checksums_valid(row: sqlite3.Row) -> bool:
        payload = bytes(row["payload"])
        payload_valid = hashlib.sha256(payload).hexdigest() == row["checksum"]
        metadata_valid = (
            hashlib.sha256(str(row["metadata_json"]).encode()).hexdigest()
            == row["metadata_checksum"]
        )
        identity = json.dumps(
            {
                "schema": row["schema_version"],
                "worker": row["worker"],
                "property": row["property_id"],
                "org": row["org"],
                "bucket": row["bucket"],
                "logical_kind": row["logical_kind"],
                "logical_key": row["logical_key"],
                "min_timestamp": row["min_timestamp"],
                "max_timestamp": row["max_timestamp"],
                "checksum": row["checksum"],
                "metadata_checksum": row["metadata_checksum"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        event_valid = (
            hashlib.sha256(EVENT_DOMAIN + identity + b"\0" + payload).hexdigest() == row["event_id"]
        )
        return payload_valid and metadata_valid and event_valid

    def retry(self, stream_key: str | None = None) -> int:
        with self.connection:
            if stream_key is None:
                cursor = self.connection.execute(
                    "UPDATE queue SET attempts=0, last_failure_class=NULL"
                )
                self.connection.execute(
                    """UPDATE streams SET state='ready', reason_class=NULL
                    WHERE reason_class IS NULL OR reason_class != 'checksum_mismatch'"""
                )
                self._set_meta("delivery_paused", "0")
                self._set_meta("pause_reason", "")
            else:
                cursor = self.connection.execute(
                    """UPDATE queue SET attempts=0, last_failure_class=NULL
                    WHERE stream_key=?""",
                    (stream_key,),
                )
                self.connection.execute(
                    """UPDATE streams SET state='ready', reason_class=NULL
                    WHERE stream_key=? AND
                    (reason_class IS NULL OR reason_class != 'checksum_mismatch')""",
                    (stream_key,),
                )
            self._set_meta("retry_not_before", "0")
        return cursor.rowcount

    def export_quarantine(self, destination: Path) -> int:
        if destination.exists():
            raise OutboxError("quarantine export destination already exists")
        rows = self.connection.execute("SELECT * FROM quarantine ORDER BY seq")
        old_umask = os.umask(0o077)
        try:
            with destination.open("x", encoding="utf-8") as handle:
                count = 0
                for row in rows:
                    item = dict(row)
                    item["payload"] = bytes(item["payload"]).decode("utf-8", "replace")
                    handle.write(json.dumps(item, sort_keys=True) + "\n")
                    count += 1
        finally:
            os.umask(old_umask)
        return count

    def _meta(self, key: str) -> str:
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else ""

    def _meta_float(self, key: str) -> float:
        value = self._meta(key)
        return float(value) if value else 0.0

    def _set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            """INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )

    def status_json(self) -> str:
        return json.dumps(asdict(self.status()), sort_keys=True)
