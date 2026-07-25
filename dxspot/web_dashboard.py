"""Servidor HTTP y serialización del dashboard web."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .constants import APP_VERSION, dxspot_client_login
from .dashboard import clean_text


STATIC_DIRECTORY = Path(__file__).with_name("web")
MAX_REQUEST_LINE = 8_192
MAX_HEADER_BYTES = 32_768
WEB_SOURCE_LABELS = {
    "dxcluster": "SPOTS HUMANOS",
    "rbn_cw": "SKIMMER CW/RTTY",
    "rbn_digital": "SKIMMER FTx",
    "rbn_local": "SKIMMER LOCAL",
}


def age_seconds(timestamp: float | None) -> int | None:
    if timestamp is None:
        return None
    return max(0, int(time.monotonic() - timestamp))


def socket_snapshot(stats: Any, direction: str) -> dict[str, Any]:
    if direction == "rx":
        line_rate, _ = stats.rx_rate()
        last_activity = stats.last_rx
    else:
        line_rate, _ = stats.tx_rate()
        last_activity = stats.last_tx
    return {
        "state": stats.state,
        "line_rate": line_rate,
        "last_activity_seconds": age_seconds(last_activity),
        "reconnects": stats.reconnects,
        "last_error": clean_text(stats.last_error),
    }


def connection_snapshot(stats: Any) -> dict[str, Any]:
    return {
        "state": stats.state,
        "last_activity_seconds": age_seconds(stats.last_rx),
        "reconnects": stats.reconnects,
        "last_error": clean_text(stats.last_error),
    }


def stream_options(application: Any) -> list[dict[str, str]]:
    options = [
        {
            "key": f"in:{source.key}",
            "label": f"IN · {WEB_SOURCE_LABELS[source.key]}",
        }
        for source in application.config.sources
        if source.key != "dxcluster"
    ]
    for session in sorted(
        application.clients.values(),
        key=lambda item: item.identifier,
    ):
        options.extend(
            (
                {
                    "key": f"in:client:{session.identifier}",
                    "label": f"IN · {session.callsign} · SPOTS HUMANOS",
                },
                {
                    "key": f"out:client:{session.identifier}",
                    "label": f"OUT · {session.callsign}",
                },
            )
        )
    return options


def stream_snapshot(
    application: Any,
    key: str,
) -> dict[str, Any] | None:
    labels = {
        option["key"]: option["label"]
        for option in stream_options(application)
    }
    for stream_key, _, entries in application.dashboard_streams():
        if stream_key != key:
            continue
        return {
            "key": key,
            "label": labels.get(key, key),
            "entries": [
                {
                    "timestamp": timestamp,
                    "message": clean_text(message),
                }
                for timestamp, message in list(entries)[-120:]
            ],
        }
    return None


def dashboard_snapshot(
    application: Any,
    *,
    include_history: bool = True,
) -> dict[str, Any]:
    common_sources: list[dict[str, Any]] = []
    for source in application.config.sources:
        stats = socket_snapshot(
            application.source_stats[source.key],
            "rx",
        )
        if source.key != "dxcluster":
            item = {
                "key": source.key,
                "label": WEB_SOURCE_LABELS[source.key],
                "enabled": source.enabled,
                "address": f"{source.host}:{source.port}",
                **stats,
            }
            if include_history:
                item["rate_history"] = [
                    [timestamp, value]
                    for timestamp, value in application.rate_histories[
                        source.key
                    ]
                ]
            common_sources.append(item)
    clients = []
    for session in sorted(
        application.clients.values(),
        key=lambda item: item.identifier,
    ):
        connection = connection_snapshot(session.dxspot_stats)
        delivery = socket_snapshot(session.stats, "tx")
        client: dict[str, Any] = {
            "key": str(session.identifier),
            "callsign": session.callsign,
            "dxspot_login": dxspot_client_login(session.callsign),
            "queue_size": session.queue.qsize(),
            "queue_capacity": session.queue.maxsize,
            "connection": connection,
            "delivery": delivery,
        }
        if include_history:
            client["delivery"]["rate_history"] = [
                [timestamp, value]
                for timestamp, value in session.delivery_rate_history
            ]
        clients.append(client)
    return {
        "application": {
            "name": "DXSpot Agregator",
            "version": APP_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(),
            "uptime_seconds": max(
                0,
                int(time.monotonic() - application.started_at),
            ),
            "country_version": application.country_database.version,
            "country_status": application.country_file_status,
            "clients": len(application.clients),
        },
        "common_sources": common_sources,
        "events": [
            {
                "timestamp": event["timestamp"],
                "message": clean_text(event["message"]),
            }
            for event in list(application.system_events)[-80:]
        ],
        "streams": stream_options(application),
        "clients": clients,
    }


class WebDashboardServer:
    """Servidor HTTP mínimo, de solo lectura y sin dependencias externas."""

    def __init__(self, application: Any) -> None:
        self.application = application
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        config = self.application.config.web
        self.server = await asyncio.start_server(
            self._handle_connection,
            config.host,
            config.port,
        )

    async def close(self) -> None:
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()

    def listen_address(self) -> str:
        config = self.application.config.web
        if self.server is None or not self.server.sockets:
            return f"{config.host}:{config.port}"
        address = self.server.sockets[0].getsockname()
        return f"{address[0]}:{address[1]}"

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line or len(request_line) > MAX_REQUEST_LINE:
                await self._respond(writer, 400, b"Solicitud incorrecta")
                return
            header_bytes = 0
            while True:
                header = await reader.readline()
                header_bytes += len(header)
                if not header or header in (b"\r\n", b"\n"):
                    break
                if header_bytes > MAX_HEADER_BYTES:
                    await self._respond(writer, 431, b"Cabeceras demasiado grandes")
                    return

            try:
                method, target, _ = request_line.decode(
                    "ascii",
                    errors="strict",
                ).strip().split(" ", 2)
            except (UnicodeDecodeError, ValueError):
                await self._respond(writer, 400, b"Solicitud incorrecta")
                return
            if method != "GET":
                await self._respond(
                    writer,
                    405,
                    b"Metodo no permitido",
                    extra_headers={"Allow": "GET"},
                )
                return

            parsed_target = urlsplit(target)
            path = parsed_target.path
            if path == "/":
                await self._serve_file(
                    writer,
                    "index.html",
                    "text/html; charset=utf-8",
                )
            elif path == "/styles.css":
                await self._serve_file(
                    writer,
                    "styles.css",
                    "text/css; charset=utf-8",
                )
            elif path == "/app.js":
                await self._serve_file(
                    writer,
                    "app.js",
                    "text/javascript; charset=utf-8",
                )
            elif path == "/api/state":
                payload = json.dumps(
                    dashboard_snapshot(self.application),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                await self._respond(
                    writer,
                    200,
                    payload,
                    content_type="application/json; charset=utf-8",
                    extra_headers={"Cache-Control": "no-store"},
                )
            elif path == "/api/events":
                await self._stream_events(writer)
            elif path == "/api/stream":
                key = parse_qs(parsed_target.query).get("key", [""])[0]
                stream = stream_snapshot(self.application, key)
                if stream is None:
                    await self._respond(writer, 404, b"Stream no disponible")
                    return
                payload = json.dumps(
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                await self._respond(
                    writer,
                    200,
                    payload,
                    content_type="application/json; charset=utf-8",
                    extra_headers={"Cache-Control": "no-store"},
                )
            elif path == "/health":
                await self._respond(
                    writer,
                    200,
                    b'{"status":"ok"}',
                    content_type="application/json; charset=utf-8",
                    extra_headers={"Cache-Control": "no-store"},
                )
            else:
                await self._respond(writer, 404, b"No encontrado")
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError):
                pass

    async def _stream_events(
        self,
        writer: asyncio.StreamWriter,
    ) -> None:
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: keep-alive\r\n"
            "X-Accel-Buffering: no\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "X-Frame-Options: DENY\r\n"
            "Referrer-Policy: no-referrer\r\n"
            "Content-Security-Policy: default-src 'self'; "
            "connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'\r\n"
            "\r\n"
            "retry: 2000\n\n"
        )
        writer.write(headers.encode("ascii"))
        await writer.drain()

        include_history = True
        while not self.application.stop.is_set():
            payload = json.dumps(
                dashboard_snapshot(
                    self.application,
                    include_history=include_history,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            writer.write(b"event: state\ndata: " + payload + b"\n\n")
            await writer.drain()
            include_history = False
            try:
                await asyncio.wait_for(
                    self.application.stop.wait(),
                    timeout=1,
                )
            except asyncio.TimeoutError:
                pass

    async def _serve_file(
        self,
        writer: asyncio.StreamWriter,
        filename: str,
        content_type: str,
    ) -> None:
        try:
            payload = (STATIC_DIRECTORY / filename).read_bytes()
        except OSError:
            await self._respond(writer, 500, b"Recurso no disponible")
            return
        await self._respond(
            writer,
            200,
            payload,
            content_type=content_type,
            extra_headers={"Cache-Control": "no-cache"},
        )

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: int,
        payload: bytes,
        *,
        content_type: str = "text/plain; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        reasons = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            431: "Request Header Fields Too Large",
            500: "Internal Server Error",
        }
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(payload)),
            "Connection": "close",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            ),
        }
        if extra_headers:
            headers.update(extra_headers)
        header_text = (
            f"HTTP/1.1 {status} {reasons[status]}\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
            + "\r\n"
        )
        writer.write(header_text.encode("ascii") + payload)
        await writer.drain()
