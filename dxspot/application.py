"""Coordinación del servidor, fuentes, clientes y metadatos."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import termios
import time
import tty
from collections import deque
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .config import AppConfig, SourceConfig
from .constants import (
    ADS_FREQUENCY_MARGIN_KHZ,
    ADS_SOURCE_KEYS,
    BEACON_COMMENT_PATTERN,
    DX_FREQUENCY_DECIMAL_COLUMN,
    DX_SPOT_PATTERN,
    FILTERED_SOURCE_KEYS,
    ONE_DECIMAL_FREQUENCY_SOURCE_KEYS,
    callsigns_match,
    frequency_band,
)
from .countries import CountryDatabase, download_latest
from .dashboard import render_dashboard
from .models import (
    AdsGroup,
    ClientMessage,
    ClientSession,
    SocketStats,
    SpotRecord,
)
from .network import client_loop, upstream_loop
from .profiles import ClientProfileStore
from .telnet import close_writer
from .web_dashboard import WebDashboardServer


RATE_HISTORY_SECONDS = 600


class DXSpotAgregator:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.stop = asyncio.Event()
        self.server: asyncio.AbstractServer | None = None
        self.web_dashboard = (
            WebDashboardServer(self) if config.web.enabled else None
        )
        self.source_stats = {
            source.key: SocketStats() for source in config.sources
        }
        self.offer_stats = SocketStats(state="ACTIVO")
        self.delivery_stats = SocketStats(state="ACTIVO")
        self.rate_histories: dict[str, deque[tuple[float, int]]] = {
            **{
                source.key: deque(maxlen=RATE_HISTORY_SECONDS)
                for source in config.sources
            },
            "aggregate": deque(maxlen=RATE_HISTORY_SECONDS),
        }
        self.country_database = CountryDatabase()
        self.country_database.version = (
            "pendiente" if config.country_file.enabled else "desactivada"
        )
        self.country_file_status = "desactivada"
        self.spot_records: deque[SpotRecord] = deque(maxlen=10_000)
        self.clients: dict[int, ClientSession] = {}
        self.source_writers: dict[str, asyncio.StreamWriter] = {}
        self.source_write_locks = {
            source.key: asyncio.Lock() for source in config.sources
        }
        self.client_tasks: set[asyncio.Task[Any]] = set()
        self.background_tasks: list[asyncio.Task[Any]] = []
        self.next_client_identifier = 1
        self.started_at = time.monotonic()
        self.system_events: deque[dict[str, str]] = deque(maxlen=500)
        self.source_input_streams: dict[
            str,
            deque[tuple[str, str]],
        ] = {
            source.key: deque(maxlen=200)
            for source in config.sources
            if source.key != "dxcluster"
        }
        self.dashboard_view_key = "activity"
        self._terminal_settings: list[Any] | None = None
        self._dashboard_input_fd: int | None = None
        self._dashboard_key_buffer = b""
        self.profile_store = ClientProfileStore(
            config.server.client_config_path,
            self.log,
        )

    def log(self, message: str) -> None:
        event_time = datetime.now().astimezone()
        self.system_events.append(
            {
                "timestamp": event_time.isoformat(),
                "message": message,
            }
        )
        if not (self.config.dashboard and sys.stdout.isatty()):
            stamp = event_time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{stamp}] {message}", flush=True)

    async def start(self) -> None:
        self.log("sistema iniciado")
        if self.config.country_file.enabled:
            await self._load_country_database()
        else:
            self.log("CTY.DAT: carga desactivada")
        self.server = await asyncio.start_server(
            self._accept_client,
            self.config.server.host,
            self.config.server.port,
        )
        addresses = ", ".join(
            str(socket.getsockname()) for socket in self.server.sockets or ()
        )
        self.log(f"servidor Telnet escuchando en {addresses}")
        if self.web_dashboard is not None:
            await self.web_dashboard.start()
            self.background_tasks.append(
                asyncio.create_task(
                    self._rate_history_loop(),
                    name="rate-history",
                )
            )
            self.log(
                "dashboard web escuchando en "
                f"http://{self.web_dashboard.listen_address()}"
            )
        if self.config.country_file.enabled:
            self.background_tasks.append(
                asyncio.create_task(
                    self._country_update_loop(),
                    name="country-file-update",
                )
            )
            self.log(
                "CTY.DAT: actualización automática cada "
                f"{self.config.country_file.update_interval_seconds:g}s"
            )

        for source in self.config.sources:
            if source.key == "dxcluster":
                self.source_stats[source.key].state = (
                    "SIN CLIENTES" if source.enabled else "DESACTIVADO"
                )
            elif source.enabled:
                self.background_tasks.append(
                    asyncio.create_task(
                        upstream_loop(self, source),
                        name=f"upstream-{source.key}",
                    )
                )
            else:
                self.source_stats[source.key].state = "DESACTIVADO"
        if self.config.dashboard and sys.stdout.isatty():
            self._enable_dashboard_input()
            self.background_tasks.append(
                asyncio.create_task(
                    self._dashboard_loop(),
                    name="dashboard",
                )
            )

    async def _load_country_database(self) -> None:
        config = self.config.country_file
        previous_database = self.country_database
        try:
            database, status, downloaded = await asyncio.to_thread(
                download_latest,
                config.url,
                config.cache_path,
                config.download_timeout_seconds,
            )
        except RuntimeError as error:
            if previous_database.entity_count:
                self.country_file_status = (
                    f"{self.country_file_status}; actualización fallida: "
                    f"{error}"
                )
                self.log(
                    f"CTY.DAT: actualización fallida; se conserva "
                    f"{previous_database.version} con "
                    f"{previous_database.entity_count} entidades: {error}"
                )
            else:
                self.country_database.version = "no disponible"
                self.country_file_status = f"no disponible: {error}"
                self.log(f"CTY.DAT: {error}")
            return
        self.country_file_status = status
        if downloaded:
            self.country_database = database
            self.log(
                f"CTY.DAT aplicado {database.version}: "
                f"{database.entity_count} entidades; {status}"
            )
        elif not previous_database.entity_count:
            self.country_database = database
            self.log(
                f"CTY.DAT cargado desde caché {database.version}: "
                f"{database.entity_count} entidades; "
                "no hay nueva versión (HTTP 304)"
            )
        elif status.startswith("no hay nueva versión"):
            self.log(
                f"CTY.DAT sin cambios {previous_database.version}: "
                f"{previous_database.entity_count} entidades; "
                "no hay nueva versión (HTTP 304)"
            )
        else:
            self.log(
                f"CTY.DAT: actualización fallida; se conserva "
                f"{previous_database.version} con "
                f"{previous_database.entity_count} entidades; {status}"
            )

    async def _country_update_loop(self) -> None:
        interval = self.config.country_file.update_interval_seconds
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(
                    self.stop.wait(),
                    timeout=interval,
                )
                return
            except asyncio.TimeoutError:
                pass
            self.log("CTY.DAT: iniciando actualización automática")
            try:
                await self._load_country_database()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.log(f"CTY.DAT: error inesperado al actualizar: {error}")

    async def run(self) -> None:
        await self.start()
        await self.stop.wait()

    async def close(self) -> None:
        self.stop.set()
        if self.server is not None:
            self.server.close()
        if self.web_dashboard is not None:
            await self.web_dashboard.close()

        sessions = tuple(self.clients.values())
        background_tasks = tuple(self.background_tasks)
        client_tasks = tuple(self.client_tasks)
        for session in sessions:
            self.discard_ads(session)
            session.writer.close()
        for task in background_tasks:
            task.cancel()
        for task in client_tasks:
            task.cancel()
        await asyncio.gather(
            *background_tasks,
            *client_tasks,
            return_exceptions=True,
        )
        await asyncio.gather(
            *(close_writer(session.writer) for session in sessions),
            return_exceptions=True,
        )
        if self.server is not None:
            try:
                await asyncio.wait_for(
                    self.server.wait_closed(),
                    timeout=1,
                )
            except asyncio.TimeoutError:
                pass
        self.clients.clear()
        if self.config.dashboard and sys.stdout.isatty():
            self._disable_dashboard_input()
            sys.stdout.write("\033[?25h\n")
            sys.stdout.flush()

    def record_source_input(self, source_key: str, line: str) -> None:
        stream = self.source_input_streams.get(source_key)
        if stream is not None:
            self._append_stream_line(stream, line)

    @staticmethod
    def _append_stream_line(
        stream: deque[tuple[str, str]],
        line: str,
    ) -> None:
        timestamp = datetime.now().astimezone().isoformat()
        stream.append((timestamp, line if line else "␤"))

    def dashboard_streams(
        self,
    ) -> list[tuple[str, str, deque[tuple[str, str]]]]:
        streams = [
            (
                f"in:{source.key}",
                f"IN · {source.name}",
                self.source_input_streams[source.key],
            )
            for source in self.config.sources
            if source.key != "dxcluster"
        ]
        for session in sorted(
            self.clients.values(),
            key=lambda item: item.identifier,
        ):
            streams.extend(
                (
                    (
                        f"in:client:{session.identifier}",
                        f"IN · {session.callsign} · SPOTS HUMANOS",
                        session.input_stream,
                    ),
                    (
                        f"out:client:{session.identifier}",
                        f"OUT · {session.callsign}",
                        session.output_stream,
                    ),
                )
            )
        return streams

    def _enable_dashboard_input(self) -> None:
        if not sys.stdin.isatty():
            return
        file_descriptor: int | None = None
        settings: list[Any] | None = None
        try:
            file_descriptor = sys.stdin.fileno()
            settings = termios.tcgetattr(file_descriptor)
            self._terminal_settings = settings
            tty.setcbreak(file_descriptor)
            asyncio.get_running_loop().add_reader(
                file_descriptor,
                self._read_dashboard_key,
            )
            self._dashboard_input_fd = file_descriptor
        except (OSError, RuntimeError, ValueError, termios.error):
            if file_descriptor is not None and settings is not None:
                try:
                    termios.tcsetattr(
                        file_descriptor,
                        termios.TCSADRAIN,
                        settings,
                    )
                except (OSError, termios.error):
                    pass
            self._terminal_settings = None
            self._dashboard_input_fd = None

    def _disable_dashboard_input(self) -> None:
        file_descriptor = self._dashboard_input_fd
        if file_descriptor is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(file_descriptor)
            if self._terminal_settings is not None:
                termios.tcsetattr(
                    file_descriptor,
                    termios.TCSADRAIN,
                    self._terminal_settings,
                )
        except (OSError, termios.error):
            pass
        self._dashboard_input_fd = None
        self._terminal_settings = None

    def _read_dashboard_key(self) -> None:
        if self._dashboard_input_fd is None:
            return
        try:
            data = os.read(self._dashboard_input_fd, 32)
        except (BlockingIOError, OSError):
            return
        self._dashboard_key_buffer = (
            self._dashboard_key_buffer + data
        )[-32:]
        buffered = self._dashboard_key_buffer
        if buffered.endswith((b"\x1b", b"\x1b[")):
            return
        self._dashboard_key_buffer = b""
        if b"a" in buffered.lower():
            self.dashboard_view_key = "activity"
            return
        if b"\x1b[D" in buffered or b"h" in buffered.lower():
            self._cycle_dashboard_view(-1)
        elif b"\x1b[C" in buffered or b"l" in buffered.lower():
            self._cycle_dashboard_view(1)

    def _cycle_dashboard_view(self, direction: int) -> None:
        keys = ["activity", *(key for key, _, _ in self.dashboard_streams())]
        try:
            index = keys.index(self.dashboard_view_key)
        except ValueError:
            index = 0
        self.dashboard_view_key = keys[(index + direction) % len(keys)]

    def _accept_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(
            client_loop(self, reader, writer),
            name=f"client-{self.next_client_identifier}",
        )
        self.client_tasks.add(task)
        task.add_done_callback(self.client_tasks.discard)

    def broadcast(self, source: SourceConfig, line: str) -> None:
        self._deliver(source, line, tuple(self.clients.values()))

    def deliver_to_client(
        self,
        session: ClientSession,
        source: SourceConfig,
        line: str,
    ) -> None:
        if self.clients.get(session.identifier) is not session:
            return
        self._deliver(source, line, (session,))

    def _deliver(
        self,
        source: SourceConfig,
        line: str,
        sessions: tuple[ClientSession, ...],
    ) -> None:
        if (
            source.key in FILTERED_SOURCE_KEYS
            and DX_SPOT_PATTERN.match(line) is None
        ):
            return
        line = self.normalize_rbn_spotter(source.key, line)
        line = self.normalize_frequency(source.key, line)
        line = self.normalize_spot_layout(line)
        spot = self.record_spot(source.key, line)
        payload = line.encode("utf-8") + b"\r\n"
        self.offer_stats.record_tx(
            len(payload),
            line=spot is not None,
        )
        stale_clients: list[ClientSession] = []
        for session in sessions:
            if source.key not in session.enabled_sources:
                continue
            own_callsign_bypass = (
                session.seeme_enabled
                and spot is not None
                and callsigns_match(session.callsign, spot.dx)
            )
            if (
                spot is not None
                and spot.beacon
                and not session.beacon_enabled
                and not own_callsign_bypass
            ):
                continue
            filters_apply = (
                spot is not None
                and (
                    session.filter_all_sources
                    or source.key in FILTERED_SOURCE_KEYS
                )
                and not own_callsign_bypass
            )
            if filters_apply and any(
                spot_filter.rejects(spot)
                for spot_filter in session.spot_filters.values()
            ):
                continue
            if (
                session.ads_enabled
                and source.key in ADS_SOURCE_KEYS
                and spot is not None
                and not own_callsign_bypass
            ):
                self._aggregate_ads(session, spot, payload)
                continue
            try:
                session.queue.put_nowait(
                    ClientMessage(
                        payload=payload,
                        count_as_delivery=spot is not None,
                    )
                )
            except asyncio.QueueFull:
                stale_clients.append(session)
        for session in stale_clients:
            self.log(
                f"cliente {session.peer} desconectado: cola de salida llena"
            )
            session.writer.close()

    def update_dxspot_state(self) -> None:
        source = next(
            item
            for item in self.config.sources
            if item.key == "dxcluster"
        )
        if not source.enabled:
            self.source_stats[source.key].state = "DESACTIVADO"
            return
        connections = sum(
            session.dxspot_writer is not None
            and not session.dxspot_writer.is_closing()
            for session in self.clients.values()
        )
        self.source_stats[source.key].state = (
            f"{connections} CONEXIÓN"
            if connections == 1
            else f"{connections} CONEXIONES"
        )

    @staticmethod
    def normalize_rbn_spotter(source_key: str, line: str) -> str:
        if source_key not in ADS_SOURCE_KEYS:
            return line
        match = DX_SPOT_PATTERN.match(line)
        if match is None:
            return line
        start, end = match.span("spotter")
        if line[end:end + 2] != "-#":
            return line
        normalized = re.sub(r"-\d+$", "", match.group("spotter"))
        if normalized == match.group("spotter"):
            return line
        frequency_start = match.start("frequency")
        header = line[:start] + normalized + line[end:end + 2] + ":"
        if len(header) > frequency_start:
            return line
        return (
            header
            + (" " * (frequency_start - len(header)))
            + line[frequency_start:]
        )

    @staticmethod
    def normalize_frequency(source_key: str, line: str) -> str:
        if source_key not in ONE_DECIMAL_FREQUENCY_SOURCE_KEYS:
            return line
        match = DX_SPOT_PATTERN.match(line)
        if match is None:
            return line
        raw_frequency = match.group("frequency")
        try:
            frequency = Decimal(raw_frequency).quantize(
                Decimal("0.1"),
                rounding=ROUND_HALF_UP,
            )
        except InvalidOperation:
            return line
        normalized = f"{frequency:.1f}"
        field_width = len(raw_frequency)
        if len(normalized) > field_width:
            return line
        start, end = match.span("frequency")
        return (
            line[:start]
            + normalized.rjust(field_width)
            + line[end:]
        )

    @staticmethod
    def normalize_spot_layout(line: str) -> str:
        match = DX_SPOT_PATTERN.match(line)
        if match is None:
            return line
        frequency = match.group("frequency")
        decimal_offset = frequency.find(".")
        if decimal_offset < 0:
            return line
        frequency_start = match.start("frequency")
        colon = line.rfind(":", 0, frequency_start)
        if colon < 0:
            return line
        header = line[:colon].rstrip() + ":"
        target_start = (
            DX_FREQUENCY_DECIMAL_COLUMN - 1 - decimal_offset
        )
        if len(header) >= target_start:
            return line
        return (
            header
            + (" " * (target_start - len(header)))
            + line[frequency_start:]
        )

    def _aggregate_ads(
        self,
        session: ClientSession,
        spot: SpotRecord,
        payload: bytes,
    ) -> None:
        normalized_dx = spot.dx.upper()
        candidates = (
            group
            for group in session.ads_groups.values()
            if group.dx == normalized_dx
            and abs(group.frequency_khz - spot.frequency_khz)
            <= ADS_FREQUENCY_MARGIN_KHZ + 1e-9
        )
        group = min(
            candidates,
            key=lambda candidate: abs(
                candidate.frequency_khz - spot.frequency_khz
            ),
            default=None,
        )
        loop = asyncio.get_running_loop()
        if group is None:
            identifier = session.next_ads_group_identifier
            session.next_ads_group_identifier += 1
            group = AdsGroup(
                identifier=identifier,
                dx=normalized_dx,
                frequency_khz=spot.frequency_khz,
                payload=payload,
                spotters={spot.spotter.upper()},
            )
            session.ads_groups[identifier] = group
        elif group.timer is not None:
            group.timer.cancel()
        group.spotters.add(spot.spotter.upper())
        group.timer = loop.call_later(
            session.ads_window_seconds,
            self._deliver_ads_group,
            session.identifier,
            group.identifier,
        )

    def _deliver_ads_group(
        self,
        session_identifier: int,
        group_identifier: int,
    ) -> None:
        session = self.clients.get(session_identifier)
        if session is None:
            return
        group = session.ads_groups.pop(group_identifier, None)
        if group is None:
            return
        group.timer = None
        if len(group.spotters) < session.ads_min_spotters:
            return
        try:
            session.queue.put_nowait(
                ClientMessage(
                    payload=self._ads_group_payload(group),
                    count_as_delivery=True,
                )
            )
        except asyncio.QueueFull:
            self.log(
                f"cliente {session.peer} desconectado: cola de salida llena"
            )
            session.writer.close()

    def flush_ads(self, session: ClientSession) -> None:
        groups = tuple(
            session.ads_groups[identifier]
            for identifier in sorted(session.ads_groups)
        )
        session.ads_groups.clear()
        for group in groups:
            if group.timer is not None:
                group.timer.cancel()
                group.timer = None
            if len(group.spotters) < session.ads_min_spotters:
                continue
            session.queue.put_nowait(
                ClientMessage(
                    payload=self._ads_group_payload(group),
                    count_as_delivery=True,
                )
            )

    @staticmethod
    def _ads_group_payload(group: AdsGroup) -> bytes:
        additional_spotters = len(group.spotters) - 1
        if additional_spotters < 1:
            return group.payload
        line = group.payload.rstrip(b"\r\n")
        suffix = f" +{additional_spotters}".encode("ascii")
        counter = f"+{additional_spotters}".encode("ascii")
        fields_start = line.find(b":") + 1
        beacon_markers = tuple(
            re.finditer(
                rb"\b(?:NCDXF|BEACON)\b",
                line[fields_start:],
                flags=re.IGNORECASE,
            )
        )
        if beacon_markers:
            beacon = beacon_markers[-1]
            beacon_start = fields_start + beacon.start()
            beacon_end = fields_start + beacon.end()
            beacon_width = beacon_end - beacon_start
            beacon_counter = b"BCN" + counter
            beacon_timestamp_spacing = tuple(
                re.finditer(
                    rb"\s+(?=\d{4}Z\b)",
                    line[beacon_end:],
                )
            )
            if beacon_timestamp_spacing:
                timestamp_padding = beacon_timestamp_spacing[-1]
                beacon_end += timestamp_padding.end()
                beacon_width = beacon_end - beacon_start
            if len(beacon_counter) > beacon_width:
                beacon_counter = b"B" + counter
            if len(beacon_counter) <= beacon_width:
                return (
                    line[:beacon_start]
                    + beacon_counter.ljust(beacon_width)
                    + line[beacon_end:]
                    + b"\r\n"
                )
        cq_markers = tuple(
            re.finditer(
                rb"\bCQ\b",
                line[fields_start:],
                flags=re.IGNORECASE,
            )
        )
        if cq_markers:
            cq_start = fields_start + cq_markers[-1].start()
            cq_end = fields_start + cq_markers[-1].end()
            cq_padding = re.match(rb"\s+", line[cq_end:])
            if cq_padding is not None:
                segment_end = cq_end + len(cq_padding.group())
                segment_width = segment_end - cq_start
                if len(counter) <= segment_width:
                    remaining_padding = b" " * (
                        segment_width - len(counter)
                    )
                    return (
                        line[:cq_start]
                        + counter
                        + remaining_padding
                        + line[segment_end:]
                        + b"\r\n"
                    )

        timestamp_spacing = tuple(
            re.finditer(rb"\s+(?=\d{4}Z\b)", line[fields_start:])
        )
        if timestamp_spacing:
            padding = timestamp_spacing[-1]
            padding_start = fields_start + padding.start()
            padding_end = fields_start + padding.end()
            if len(padding.group()) >= len(suffix):
                remaining_padding = line[
                    padding_start + len(suffix):padding_end
                ]
                return (
                    line[:padding_start]
                    + suffix
                    + remaining_padding
                    + line[padding_end:]
                    + b"\r\n"
                )
        return line + suffix + b"\r\n"

    @staticmethod
    def discard_ads(session: ClientSession) -> None:
        groups = tuple(session.ads_groups.values())
        session.ads_groups.clear()
        for group in groups:
            if group.timer is not None:
                group.timer.cancel()
                group.timer = None

    def record_spot(
        self,
        source_key: str,
        line: str,
    ) -> SpotRecord | None:
        match = DX_SPOT_PATTERN.match(line)
        if match is None:
            return None
        spotter = match.group("spotter")
        dx = match.group("dx")
        frequency_khz = float(match.group("frequency"))
        record = SpotRecord(
            received_at=datetime.now().astimezone(),
            source_key=source_key,
            line=line,
            spotter=spotter,
            dx=dx,
            frequency_khz=frequency_khz,
            band=frequency_band(frequency_khz),
            spotter_country=self.country_database.resolve(spotter),
            dx_country=self.country_database.resolve(dx),
            beacon=BEACON_COMMENT_PATTERN.search(
                line[match.end():]
            ) is not None,
        )
        self.spot_records.append(record)
        return record

    def listen_address(self) -> str:
        if self.server is None or not self.server.sockets:
            return f"{self.config.server.host}:{self.config.server.port}"
        address = self.server.sockets[0].getsockname()
        return f"{address[0]}:{address[1]}"

    def render_dashboard(self, columns: int, rows: int) -> str:
        return render_dashboard(self, columns, rows)

    async def _rate_history_loop(self) -> None:
        while not self.stop.is_set():
            timestamp = time.time()
            for source in self.config.sources:
                line_rate, _ = self.source_stats[source.key].rx_rate()
                self.rate_histories[source.key].append(
                    (timestamp, line_rate)
                )
            aggregate_rate, _ = self.offer_stats.tx_rate()
            self.rate_histories["aggregate"].append(
                (timestamp, aggregate_rate)
            )
            for session in tuple(self.clients.values()):
                delivery_rate, _ = session.stats.tx_rate()
                session.delivery_rate_history.append(
                    (timestamp, delivery_rate)
                )
            try:
                await asyncio.wait_for(
                    self.stop.wait(),
                    timeout=1,
                )
            except asyncio.TimeoutError:
                pass

    async def _dashboard_loop(self) -> None:
        while not self.stop.is_set():
            terminal = shutil.get_terminal_size(fallback=(160, 50))
            screen = self.render_dashboard(
                terminal.columns,
                terminal.lines,
            )
            sys.stdout.write("\033[2J\033[H\033[?25l" + screen)
            sys.stdout.flush()
            try:
                await asyncio.wait_for(
                    self.stop.wait(),
                    timeout=self.config.refresh_seconds,
                )
            except asyncio.TimeoutError:
                pass
