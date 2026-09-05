from __future__ import annotations

import json
import os
import queue
import re
import socket
import ssl
import stat
import tempfile
import threading
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solar_battery_forecaster.config import ObservabilityConfig, SyslogConfig

STATUS_SCHEMA_VERSION = 1
STATUS_FILE = "status.json"
STATUS_MAX_BYTES = 65_536
STATUS_SERVICES = ("telemetry", "tariff", "forecast-plan", "reconciliation", "dashboard")
EXCEPTION_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
FAILURE_CODE = re.compile(r"^(?:http_[1-5][0-9]{2}|[A-Za-z][A-Za-z0-9_]{0,63})$")
EVENT_DEFINITIONS = {
    "process_started": ("INFO", "Process started"),
    "process_stopping": ("INFO", "Process stopping"),
    "process_stopped": ("INFO", "Process stopped"),
    "cycle_started": ("INFO", "Collection cycle started"),
    "cycle_succeeded": ("INFO", "Collection cycle completed"),
    "cycle_failed": ("ERROR", "Collection cycle failed"),
    "delivery_direct": ("INFO", "Data accepted and confirmed by InfluxDB"),
    "delivery_buffered": ("WARNING", "Data accepted into the local fallback"),
    "delivery_ignored": ("INFO", "No data required delivery"),
    "property_failed": ("ERROR", "Property operation failed ({exception_class})"),
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def operational_event(
    code: str, exception_class: str | None = None, at: str | None = None
) -> dict[str, str]:
    if code not in EVENT_DEFINITIONS:
        raise ValueError("unknown operational event")
    severity, template = EVENT_DEFINITIONS[code]
    safe_class = (
        exception_class
        if exception_class and EXCEPTION_CLASS.fullmatch(exception_class)
        else "Error"
    )
    event = {
        "at": at or utc_now(),
        "code": code,
        "severity": severity,
        "message": template.format(exception_class=safe_class),
    }
    if code == "property_failed":
        event["exception_class"] = safe_class
    return event


class RemoteSyslog:
    def __init__(
        self,
        config: SyslogConfig,
        service: str,
        status_callback: Any,
    ) -> None:
        self.config = config
        self.service = service
        self.status_callback = status_callback
        self.messages: queue.Queue[bytes] = queue.Queue(maxsize=config.queue_size)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.dropped = 0
        self.last_sent_at: str | None = None
        self.last_failure_at: str | None = None
        self.last_failure_class: str | None = None

    def start(self) -> None:
        if not self.config.enabled:
            return
        self.thread = threading.Thread(target=self._run, name="remote-syslog", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=self.config.connect_timeout_seconds + 1)

    def emit(self, code: str, exception_class: str | None = None) -> None:
        if not self.config.enabled:
            return
        event = operational_event(code, exception_class)
        event["service"] = self.service
        payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
        message = f"<14>1 {utc_now()} - solar-battery {os.getpid()} {self.service} - {payload}\n"
        try:
            self.messages.put_nowait(message.encode("utf-8")[:8192])
        except queue.Full:
            self.dropped += 1
            self._publish_status()

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.config.enabled,
            "transport": self.config.transport if self.config.enabled else None,
            "queued": self.messages.qsize(),
            "dropped": self.dropped,
            "last_sent_at": self.last_sent_at,
            "last_failure_at": self.last_failure_at,
            "last_failure_class": self.last_failure_class,
        }

    def _publish_status(self) -> None:
        self.status_callback(self.status())

    def _run(self) -> None:
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                message = self.messages.get(timeout=0.5)
            except queue.Empty:
                continue
            while not self.stop_event.is_set():
                try:
                    self._send(message)
                except (OSError, ssl.SSLError) as exc:
                    self.last_failure_at = utc_now()
                    self.last_failure_class = type(exc).__name__
                    self._publish_status()
                    self.stop_event.wait(delay)
                    delay = min(60.0, delay * 2)
                else:
                    self.last_sent_at = utc_now()
                    self.last_failure_class = None
                    delay = 1.0
                    self._publish_status()
                    break
            self.messages.task_done()

    def _send(self, message: bytes) -> None:
        host = self.config.host
        if host is None:
            return
        if self.config.transport == "udp":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                connection.settimeout(self.config.connect_timeout_seconds)
                connection.sendto(message[:1400], (host, self.config.port))
            return
        with socket.create_connection(
            (host, self.config.port), timeout=self.config.connect_timeout_seconds
        ) as connection:
            if self.config.transport == "tls":
                context = ssl.create_default_context()
                with context.wrap_socket(connection, server_hostname=host) as secured:
                    secured.sendall(message)
            else:
                connection.sendall(message)


class HealthReporter:
    def __init__(
        self,
        service: str,
        config: ObservabilityConfig,
        property_ids: list[str],
    ) -> None:
        if service not in STATUS_SERVICES:
            raise ValueError("unknown status service")
        if config.status_directory is None:
            raise ValueError("status directory is not configured")
        self.service = service
        self.config = config
        self.directory = config.status_directory
        del property_ids
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.events: deque[dict[str, str]] = deque(maxlen=50)
        now = utc_now()
        self.state: dict[str, Any] = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "service": service,
            "pid": os.getpid(),
            "lifecycle": "starting",
            "started_at": now,
            "heartbeat_at": now,
            "last_cycle_started_at": None,
            "last_cycle_completed_at": None,
            "last_cycle_result": None,
            "last_local_accepted_at": None,
            "last_direct_delivery_at": None,
            "last_buffered_at": None,
            "last_confirmed_delivery_at": None,
            "outbox": None,
            "syslog": {"enabled": False},
        }
        self.syslog = RemoteSyslog(config.syslog, service, self._update_syslog)
        self.thread = threading.Thread(target=self._heartbeat, name="status-heartbeat", daemon=True)

    def start(self) -> None:
        if self.directory.is_symlink():
            raise RuntimeError("status directory must not be a symbolic link")
        self.directory.mkdir(parents=True, mode=0o750, exist_ok=True)
        directory_mode = stat.S_IMODE(self.directory.stat().st_mode)
        if directory_mode & 0o027 or directory_mode & 0o050 != 0o050:
            raise RuntimeError("status directory permissions must be group-readable, not writable")
        status_path = self.directory / STATUS_FILE
        if status_path.exists() and (
            status_path.is_symlink() or stat.S_IMODE(status_path.stat().st_mode) & 0o022
        ):
            raise RuntimeError("status file must not be group/other-writable")
        with self.lock:
            self.state.update(lifecycle="running", syslog=self.syslog.status())
            self._add_event("process_started")
        self.write()
        self.syslog.start()
        self.thread.start()

    def close(self) -> None:
        self._set(lifecycle="stopping")
        self._add_event("process_stopping")
        self.stop_event.set()
        self.thread.join(timeout=1)
        self.syslog.close()
        self._set(lifecycle="stopped")
        self._add_event("process_stopped", forward=False)
        with suppress(OSError, RuntimeError):
            self.write()

    def begin_cycle(self) -> None:
        self._set(last_cycle_started_at=utc_now(), last_cycle_result="running")
        self._add_event("cycle_started")

    def complete_cycle(self, succeeded: bool, delivery_status: object | None = None) -> None:
        updates: dict[str, object] = {
            "last_cycle_completed_at": utc_now(),
            "last_cycle_result": "success" if succeeded else "failed",
        }
        if delivery_status is not None:
            updates["outbox"] = self._outbox_projection(delivery_status)
            confirmed = getattr(delivery_status, "last_confirmed_delivery", None)
            if confirmed:
                updates["last_confirmed_delivery_at"] = confirmed
        self._set(**updates)
        self._add_event("cycle_succeeded" if succeeded else "cycle_failed")

    def accepted(self, disposition: str, delivery_status: object | None = None) -> None:
        now = utc_now()
        updates: dict[str, object] = {"last_local_accepted_at": now}
        if disposition == "direct":
            updates.update(last_direct_delivery_at=now, last_confirmed_delivery_at=now)
        elif disposition == "buffered":
            updates["last_buffered_at"] = now
        if delivery_status is not None:
            updates["outbox"] = self._outbox_projection(delivery_status)
        self._set(**updates)
        self._add_event(f"delivery_{disposition}")

    def property_failed(self, error: Exception) -> None:
        self._add_event("property_failed", type(error).__name__)

    @staticmethod
    def _outbox_projection(status: object) -> dict[str, object]:
        fields = (
            "pending_records",
            "pending_bytes",
            "quarantined_records",
            "blocked_streams",
            "database_bytes",
            "delivery_paused",
            "oldest_pending_at",
            "oldest_pending_age_seconds",
            "delivered_total",
            "last_confirmed_delivery",
            "filesystem_free_bytes",
            "database_max_bytes",
            "filesystem_min_free_bytes",
            "last_failure_class",
            "last_failure_at",
            "pause_reason",
        )
        return {field: getattr(status, field, None) for field in fields}

    def _add_event(
        self, code: str, exception_class: str | None = None, *, forward: bool = True
    ) -> None:
        event = operational_event(code, exception_class)
        with self.lock:
            self.events.append(event)
        if forward:
            self.syslog.emit(code, exception_class)

    def _update_syslog(self, value: dict[str, object]) -> None:
        self._set(syslog=value)

    def _set(self, **values: object) -> None:
        with self.lock:
            self.state.update(values)

    def _heartbeat(self) -> None:
        while not self.stop_event.wait(self.config.heartbeat_seconds):
            with suppress(OSError, RuntimeError):
                self.write()

    def write(self) -> None:
        with self.lock:
            payload = dict(self.state)
            payload["heartbeat_at"] = utc_now()
            payload["events"] = list(self.events)
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            while len(body) > STATUS_MAX_BYTES and payload["events"]:
                payload["events"].pop(0)
                body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(body) > STATUS_MAX_BYTES:
            raise RuntimeError("status projection exceeds its size limit")
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as handle:
                temporary = handle.name
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, self.directory / STATUS_FILE)
        finally:
            if temporary is not None and Path(temporary).exists():
                Path(temporary).unlink()


def create_reporter(
    config: ObservabilityConfig, service: str, property_ids: list[str]
) -> HealthReporter | None:
    if config.status_directory is None:
        return None
    reporter = HealthReporter(service, config, property_ids)
    reporter.start()
    return reporter


def close_reporter(reporter: HealthReporter | None) -> None:
    if reporter is not None:
        reporter.close()


class StatusRepository:
    def __init__(self, dashboard_directory: Path, stale_after_seconds: int) -> None:
        self.root = dashboard_directory.parent
        self.stale_after_seconds = stale_after_seconds

    def read(self) -> dict[str, object]:
        now = datetime.now(UTC)
        services: list[dict[str, object]] = []
        for service in STATUS_SERVICES:
            directory = self.root / service
            item: dict[str, object] = {"service": service, "lifecycle": "missing", "stale": True}
            try:
                loaded = json.loads(self._read_file(directory))
                if (
                    not isinstance(loaded, dict)
                    or loaded.get("schema_version") != STATUS_SCHEMA_VERSION
                    or loaded.get("service") != service
                ):
                    raise ValueError("invalid status projection")
                projected = self._project(loaded)
                heartbeat = datetime.fromisoformat(str(projected["heartbeat_at"]))
                projected["stale"] = (
                    now - heartbeat
                ).total_seconds() > self.stale_after_seconds
                item = projected
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                pass
            services.append(item)
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "generated_at": utc_now(),
            "services": services,
        }

    def _project(self, loaded: dict[str, object]) -> dict[str, object]:
        lifecycle = loaded.get("lifecycle")
        result: dict[str, object] = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "service": loaded.get("service"),
            "pid": self._integer(loaded.get("pid")),
            "lifecycle": (
                lifecycle
                if lifecycle in {"starting", "running", "stopping", "stopped"}
                else "unknown"
            ),
            "started_at": self._timestamp(loaded.get("started_at")),
            "heartbeat_at": self._timestamp(loaded.get("heartbeat_at")),
            "last_cycle_started_at": self._timestamp(loaded.get("last_cycle_started_at")),
            "last_cycle_completed_at": self._timestamp(
                loaded.get("last_cycle_completed_at")
            ),
            "last_cycle_result": self._choice(
                loaded.get("last_cycle_result"), {"running", "success", "failed"}
            ),
            "last_local_accepted_at": self._timestamp(
                loaded.get("last_local_accepted_at")
            ),
            "last_direct_delivery_at": self._timestamp(
                loaded.get("last_direct_delivery_at")
            ),
            "last_buffered_at": self._timestamp(loaded.get("last_buffered_at")),
            "last_confirmed_delivery_at": self._timestamp(
                loaded.get("last_confirmed_delivery_at")
            ),
            "outbox": self._outbox(loaded.get("outbox")),
            "syslog": self._syslog(loaded.get("syslog")),
            "events": self._events(loaded.get("events")),
        }
        if result["heartbeat_at"] is None:
            raise ValueError("status heartbeat is invalid")
        return result

    @staticmethod
    def _integer(value: object) -> int | None:
        return value if type(value) is int and 0 <= value <= 2**63 - 1 else None

    @staticmethod
    def _number(value: object) -> int | float | None:
        if type(value) not in {int, float} or not 0 <= value <= 2**63 - 1:
            return None
        return value  # type: ignore[return-value]

    @staticmethod
    def _boolean(value: object) -> bool | None:
        return value if type(value) is bool else None

    @staticmethod
    def _choice(value: object, choices: set[str]) -> str | None:
        return value if isinstance(value, str) and value in choices else None

    @staticmethod
    def _timestamp(value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 64:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return value if parsed.tzinfo is not None else None

    @staticmethod
    def _failure_code(value: object) -> str | None:
        return value if isinstance(value, str) and FAILURE_CODE.fullmatch(value) else None

    def _outbox(self, value: object) -> dict[str, object] | None:
        if not isinstance(value, dict):
            return None
        numeric = (
            "pending_records",
            "pending_bytes",
            "quarantined_records",
            "blocked_streams",
            "database_bytes",
            "oldest_pending_age_seconds",
            "delivered_total",
            "filesystem_free_bytes",
            "database_max_bytes",
            "filesystem_min_free_bytes",
        )
        result = {field: self._number(value.get(field)) for field in numeric}
        result.update(
            delivery_paused=self._boolean(value.get("delivery_paused")),
            oldest_pending_at=self._timestamp(value.get("oldest_pending_at")),
            last_confirmed_delivery=self._timestamp(value.get("last_confirmed_delivery")),
            last_failure_class=self._failure_code(value.get("last_failure_class")),
            last_failure_at=self._timestamp(value.get("last_failure_at")),
            pause_reason=self._failure_code(value.get("pause_reason")),
        )
        return result

    def _syslog(self, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {"enabled": False}
        return {
            "enabled": self._boolean(value.get("enabled")) is True,
            "transport": self._choice(value.get("transport"), {"udp", "tcp", "tls"}),
            "queued": self._integer(value.get("queued")),
            "dropped": self._integer(value.get("dropped")),
            "last_sent_at": self._timestamp(value.get("last_sent_at")),
            "last_failure_at": self._timestamp(value.get("last_failure_at")),
            "last_failure_class": self._failure_code(value.get("last_failure_class")),
        }

    def _events(self, value: object) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, str]] = []
        for event in value[-50:]:
            if not isinstance(event, dict):
                continue
            at = self._timestamp(event.get("at"))
            code = event.get("code")
            if at is None or not isinstance(code, str) or code not in EVENT_DEFINITIONS:
                continue
            exception_class = event.get("exception_class")
            result.append(
                operational_event(
                    code,
                    exception_class if isinstance(exception_class, str) else None,
                    at,
                )
            )
        return result

    @staticmethod
    def _read_file(directory: Path) -> str:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
        directory_fd = os.open(directory, directory_flags)
        try:
            directory_mode = stat.S_IMODE(os.fstat(directory_fd).st_mode)
            if directory_mode & 0o027:
                raise ValueError("unsafe status directory permissions")
            file_fd = os.open(
                STATUS_FILE,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                metadata = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                    or metadata.st_size > STATUS_MAX_BYTES
                ):
                    raise ValueError("unsafe status file")
                with os.fdopen(file_fd, "rb", closefd=False) as handle:
                    body = handle.read(STATUS_MAX_BYTES + 1)
                if len(body) > STATUS_MAX_BYTES:
                    raise ValueError("oversize status file")
                return body.decode("utf-8")
            finally:
                os.close(file_fd)
        finally:
            os.close(directory_fd)
