from __future__ import annotations

import argparse
import importlib.resources
import json
import logging
import mimetypes
import signal
import threading
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from solar_battery_forecaster.config import AppConfig, load_config
from solar_battery_forecaster.dashboard import InfluxDashboardRepository
from solar_battery_forecaster.observability import (
    StatusRepository,
    close_reporter,
    create_reporter,
)

LOGGER = logging.getLogger(__name__)
STATIC_ROOT = importlib.resources.files("solar_battery_forecaster").joinpath("static")


class DashboardHandler(BaseHTTPRequestHandler):
    config: AppConfig
    repository: InfluxDashboardRepository
    status_repository: StatusRepository

    def _headers(self, status: HTTPStatus, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )

    def _json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self._headers(status, "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_api(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/status":
            self._json(HTTPStatus.OK, self.status_repository.read())
            return
        if path == "/api/v1/properties":
            self._json(
                HTTPStatus.OK,
                {
                    "properties": [
                        {"id": item.id, "timezone": item.timezone}
                        for item in self.config.properties
                    ]
                },
            )
            return
        prefix = "/api/v1/properties/"
        suffix = "/curve"
        if not (path.startswith(prefix) and path.endswith(suffix)):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        property_id = path[len(prefix) : -len(suffix)]
        properties = {item.id: item for item in self.config.properties}
        if property_id not in properties:
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown_property"})
            return
        today = datetime.now(ZoneInfo(properties[property_id].timezone)).date().isoformat()
        requested = query.get("date", [today])[0]
        try:
            day = date.fromisoformat(requested)
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "date_must_be_iso_8601"})
            return
        try:
            payload = self.repository.curve(properties[property_id], day)
        except Exception as exc:
            LOGGER.error("dashboard query failed (%s)", type(exc).__name__)
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "data_unavailable"})
            return
        self._json(HTTPStatus.OK, payload)

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.removeprefix("/")
        candidate = PurePosixPath(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        resource = STATIC_ROOT.joinpath(*candidate.parts)
        if not resource.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = resource.read_bytes()
        content_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        self._headers(HTTPStatus.OK, content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        request = urlsplit(self.path)
        if request.path.startswith("/api/"):
            self._serve_api(request.path, parse_qs(request.query))
        else:
            self._serve_static(request.path)

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.debug("dashboard request completed")


def make_server(
    config: AppConfig,
    host: str,
    port: int,
    repository: InfluxDashboardRepository | None = None,
    status_repository: StatusRepository | None = None,
) -> ThreadingHTTPServer:
    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.config = config
    BoundHandler.repository = repository or InfluxDashboardRepository(
        config.influxdb, expected_interval_seconds=config.schedule.telemetry_seconds
    )
    if config.observability.status_directory is None and status_repository is None:
        raise ValueError("dashboard status directory is not configured")
    BoundHandler.status_repository = status_repository or StatusRepository(
        config.observability.status_directory,  # type: ignore[arg-type]
        config.observability.stale_after_seconds,
    )
    return ThreadingHTTPServer((host, port), BoundHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only solar planning dashboard")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True
    logging.getLogger("urllib3").disabled = True
    logging.getLogger("influxdb_client").disabled = True
    config = load_config(args.config, scope="dashboard")
    reporter = create_reporter(
        config.observability, "dashboard", [item.id for item in config.properties]
    )
    server: ThreadingHTTPServer | None = None
    stopped = threading.Event()
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_stop(signum: int, frame: object) -> None:
        stopped.set()

    try:
        signal.signal(signal.SIGTERM, request_stop)
        server = make_server(config, args.host, args.port)
        server.timeout = 0.5
        LOGGER.info("dashboard listening on http://%s:%d", args.host, args.port)
        while not stopped.is_set():
            server.handle_request()
    finally:
        try:
            if server is not None:
                try:
                    # Wait for existing request threads before closing their shared client.
                    server.server_close()
                finally:
                    server.RequestHandlerClass.repository.close()
        finally:
            try:
                close_reporter(reporter)
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
