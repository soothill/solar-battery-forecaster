from __future__ import annotations

import argparse
import importlib.resources
import json
import logging
import mimetypes
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlsplit

from solar_battery_forecaster.config import AppConfig, load_config
from solar_battery_forecaster.dashboard import InfluxDashboardRepository

LOGGER = logging.getLogger(__name__)
STATIC_ROOT = importlib.resources.files("solar_battery_forecaster").joinpath("static")


class DashboardHandler(BaseHTTPRequestHandler):
    config: AppConfig
    repository: InfluxDashboardRepository

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
        if path == "/api/v1/properties":
            self._json(
                HTTPStatus.OK,
                {"properties": [{"id": item.id} for item in self.config.properties]},
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
        requested = query.get("date", [datetime.now().date().isoformat()])[0]
        try:
            day = date.fromisoformat(requested)
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "date_must_be_iso_8601"})
            return
        try:
            payload = self.repository.curve(properties[property_id], day)
        except Exception:
            LOGGER.exception("dashboard query failed")
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
        LOGGER.info("%s - %s", self.address_string(), format % args)


def make_server(
    config: AppConfig,
    host: str,
    port: int,
    repository: InfluxDashboardRepository | None = None,
) -> ThreadingHTTPServer:
    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.config = config
    BoundHandler.repository = repository or InfluxDashboardRepository(config.influxdb)
    return ThreadingHTTPServer((host, port), BoundHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only solar planning dashboard")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config)
    server = make_server(config, args.host, args.port)
    try:
        LOGGER.info("dashboard listening on http://%s:%d", args.host, args.port)
        server.serve_forever()
    finally:
        server.server_close()
        server.RequestHandlerClass.repository.close()


if __name__ == "__main__":
    main()
