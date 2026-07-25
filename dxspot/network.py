"""Bucles de red para fuentes upstream y clientes Telnet."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .commands import (
    client_text,
    handle_aggregator_command,
    prompt_message,
    queue_connection_summary,
    queue_unknown_command,
)
from .config import SourceConfig, callsign
from .constants import (
    ADS_DEFAULT_QUALITY,
    ADS_WINDOW_SECONDS,
    DX_SPOT_PATTERN,
    dxspot_client_login,
    source_login,
)
from .dashboard import clean_text
from .models import ClientMessage, ClientSession, SpotFilter
from .telnet import TelnetDecoder, close_writer, send_line


WELCOME_LINE_WIDTH = 80
WELCOME_DESCRIPTION_LINES = (
    "Software EXPERIMENTAL que combina varias fuentes de spots DX.",
    "Cada cliente puede seleccionar fuentes y aplicar filtros propios.",
    "Un algoritmo agrupa spots repetidos y reduce el ruido.",
    "",
    "EXPERIMENTAL software combining multiple real-time DX spot sources.",
    "Each client can select sources and apply individual filters.",
    "A reduction algorithm groups duplicate spots and reduces noise.",
)


def welcome_block(title: str) -> bytes:
    normalized_title = " ".join(clean_text(title).split())
    centered_title = normalized_title[:WELCOME_LINE_WIDTH].center(
        WELCOME_LINE_WIDTH
    )
    centered_description = tuple(
        line.center(WELCOME_LINE_WIDTH)
        for line in WELCOME_DESCRIPTION_LINES
    )
    separator = "-" * WELCOME_LINE_WIDTH
    return (
        "\r\n".join(
            (
                "",
                separator,
                centered_title,
                separator,
                *centered_description,
                separator,
                "",
            )
        )
        + "\r\n"
    ).encode("utf-8")


async def upstream_loop(application: Any, source: SourceConfig) -> None:
    stats = application.source_stats[source.key]
    reconnect_delay = application.config.reconnect_initial_seconds
    while not application.stop.is_set():
        writer: asyncio.StreamWriter | None = None
        stats.state = "CONECTANDO"
        stats.last_error = ""
        application.log(
            f"{source.name}: conectando a {source.host}:{source.port}"
        )
        try:
            reader, writer = await asyncio.open_connection(
                source.host,
                source.port,
            )
            stats.state = "CONECTADO"
            stats.connected_at = time.monotonic()
            application.log(f"{source.name}: conectado")
            login = source_login(application.config.login, source.key)
            await send_line(writer, login)
            stats.record_tx(len(login.encode("utf-8")) + 2, line=False)
            for command in source.commands:
                await send_line(writer, command)
                stats.record_tx(
                    len(command.encode("utf-8")) + 2,
                    line=False,
                )
            application.source_writers[source.key] = writer

            reconnect_delay = application.config.reconnect_initial_seconds
            decoder = TelnetDecoder()
            pending = bytearray()
            next_keepalive = (
                time.monotonic()
                + application.config.upstream_keepalive_seconds
            )
            while not application.stop.is_set():
                timeout = max(0.0, next_keepalive - time.monotonic())
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(4096),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    command = application.config.upstream_keepalive_command
                    await send_line(writer, command)
                    stats.record_tx(
                        len(command.encode("utf-8")) + 2,
                        line=False,
                    )
                    next_keepalive = (
                        time.monotonic()
                        + application.config.upstream_keepalive_seconds
                    )
                    continue
                if not chunk:
                    raise ConnectionError("el servidor cerró la conexión")

                stats.record_rx(len(chunk), line=False)
                text, reply = decoder.feed(chunk)
                if reply:
                    writer.write(reply)
                    await writer.drain()
                    stats.record_tx(len(reply), line=False)
                pending.extend(text)
                if len(pending) > 65_536:
                    raise ConnectionError("línea recibida mayor de 64 KiB")

                while b"\n" in pending:
                    raw_line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    line = raw_line.rstrip(b"\r").decode(
                        "utf-8",
                        errors="replace",
                    )
                    line = clean_text(line)
                    application.record_source_input(source.key, line)
                    if not line:
                        continue
                    stats.record_rx(
                        0,
                        line=DX_SPOT_PATTERN.match(line) is not None,
                    )
                    application.broadcast(source, line)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as error:
            stats.state = "DESCONECTADO"
            stats.last_error = str(error)
            stats.reconnects += 1
            application.log(
                f"{source.name}: {error}; reconexión en "
                f"{reconnect_delay:g}s"
            )
            try:
                await asyncio.wait_for(
                    application.stop.wait(),
                    timeout=reconnect_delay,
                )
            except asyncio.TimeoutError:
                pass
            reconnect_delay = min(
                reconnect_delay * 2,
                application.config.reconnect_max_seconds,
            )
        finally:
            if writer is not None:
                if application.source_writers.get(source.key) is writer:
                    application.source_writers.pop(source.key, None)
                await close_writer(writer)


async def client_dxspot_loop(
    application: Any,
    session: ClientSession,
    source: SourceConfig,
) -> None:
    """Mantiene una conexión DXSPOT exclusiva para una sesión cliente."""
    aggregate_stats = application.source_stats[source.key]
    stats = session.dxspot_stats
    reconnect_delay = application.config.reconnect_initial_seconds
    login = dxspot_client_login(session.callsign)
    while (
        not application.stop.is_set()
        and not session.writer.is_closing()
    ):
        writer: asyncio.StreamWriter | None = None
        stats.state = "CONECTANDO"
        stats.last_error = ""
        application.update_dxspot_state()
        application.log(
            f"DXSPOT [{session.callsign}]: conectando a "
            f"{source.host}:{source.port} como {login}"
        )
        try:
            reader, writer = await asyncio.open_connection(
                source.host,
                source.port,
            )
            stats.state = "CONECTADO"
            stats.connected_at = time.monotonic()
            stats.last_error = ""
            await send_line(writer, login)
            login_bytes = len(login.encode("utf-8")) + 2
            stats.record_tx(login_bytes, line=False)
            aggregate_stats.record_tx(login_bytes, line=False)
            for command in source.commands:
                await send_line(writer, command)
                command_bytes = len(command.encode("utf-8")) + 2
                stats.record_tx(command_bytes, line=False)
                aggregate_stats.record_tx(command_bytes, line=False)
            async with session.dxspot_write_lock:
                session.dxspot_writer = writer
            reconnect_delay = application.config.reconnect_initial_seconds
            application.update_dxspot_state()
            application.log(
                f"DXSPOT [{session.callsign}]: conectado como {login}"
            )

            decoder = TelnetDecoder()
            pending = bytearray()
            next_keepalive = (
                time.monotonic()
                + application.config.upstream_keepalive_seconds
            )
            while (
                not application.stop.is_set()
                and not session.writer.is_closing()
            ):
                timeout = max(0.0, next_keepalive - time.monotonic())
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(4096),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    command = application.config.upstream_keepalive_command
                    async with session.dxspot_write_lock:
                        if session.dxspot_writer is not writer:
                            raise ConnectionError(
                                "la conexión dejó de estar disponible"
                            )
                        await send_line(writer, command)
                    command_bytes = len(command.encode("utf-8")) + 2
                    stats.record_tx(command_bytes, line=False)
                    aggregate_stats.record_tx(command_bytes, line=False)
                    next_keepalive = (
                        time.monotonic()
                        + application.config.upstream_keepalive_seconds
                    )
                    continue
                if not chunk:
                    raise ConnectionError("el servidor cerró la conexión")

                stats.record_rx(len(chunk), line=False)
                aggregate_stats.record_rx(len(chunk), line=False)
                text, reply = decoder.feed(chunk)
                if reply:
                    async with session.dxspot_write_lock:
                        if session.dxspot_writer is not writer:
                            raise ConnectionError(
                                "la conexión dejó de estar disponible"
                            )
                        writer.write(reply)
                        await writer.drain()
                    stats.record_tx(len(reply), line=False)
                    aggregate_stats.record_tx(len(reply), line=False)
                pending.extend(text)
                if len(pending) > 65_536:
                    raise ConnectionError("línea recibida mayor de 64 KiB")

                while b"\n" in pending:
                    raw_line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    line = clean_text(
                        raw_line.rstrip(b"\r").decode(
                            "utf-8",
                            errors="replace",
                        )
                    )
                    application._append_stream_line(
                        session.input_stream,
                        line,
                    )
                    if not line:
                        continue
                    is_dx_line = DX_SPOT_PATTERN.match(line) is not None
                    stats.record_rx(0, line=is_dx_line)
                    aggregate_stats.record_rx(0, line=is_dx_line)
                    application.deliver_to_client(session, source, line)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as error:
            stats.state = "DESCONECTADO"
            stats.last_error = str(error)
            stats.reconnects += 1
            aggregate_stats.last_error = str(error)
            aggregate_stats.reconnects += 1
            application.update_dxspot_state()
            application.log(
                f"DXSPOT [{session.callsign}]: {error}; reconexión en "
                f"{reconnect_delay:g}s"
            )
            try:
                await asyncio.wait_for(
                    application.stop.wait(),
                    timeout=reconnect_delay,
                )
            except asyncio.TimeoutError:
                pass
            reconnect_delay = min(
                reconnect_delay * 2,
                application.config.reconnect_max_seconds,
            )
        finally:
            async with session.dxspot_write_lock:
                if session.dxspot_writer is writer:
                    session.dxspot_writer = None
            if writer is not None:
                await close_writer(writer)
            application.update_dxspot_state()


async def client_writer(application: Any, session: ClientSession) -> None:
    try:
        while True:
            queued = await session.queue.get()
            if queued is None:
                return
            if isinstance(queued, ClientMessage):
                payload = queued.payload
                pause_after_seconds = queued.pause_after_seconds
                count_as_delivery = queued.count_as_delivery
            else:
                payload = queued
                pause_after_seconds = 0
                count_as_delivery = False
            session.writer.write(payload)
            await session.writer.drain()
            text = payload.decode("utf-8", errors="replace")
            for raw_line in text.splitlines():
                application._append_stream_line(
                    session.output_stream,
                    clean_text(raw_line),
                )
            session.stats.record_tx(
                len(payload),
                line=count_as_delivery,
            )
            application.delivery_stats.record_tx(
                len(payload),
                line=count_as_delivery,
            )
            if pause_after_seconds:
                await asyncio.sleep(pause_after_seconds)
    except (ConnectionError, OSError):
        session.writer.close()


async def forward_to_dxspot(
    application: Any,
    session: ClientSession,
    payload: bytes,
) -> None:
    source_key = "dxcluster"
    async with session.dxspot_write_lock:
        writer = session.dxspot_writer
        if writer is None or writer.is_closing():
            application.log(
                f"DXSPOT [{session.callsign}]: comando descartado; "
                "su conexión no está disponible"
            )
            return
        try:
            writer.write(payload)
            await writer.drain()
            session.dxspot_stats.record_tx(len(payload), line=False)
            application.source_stats[source_key].record_tx(
                len(payload),
                line=False,
            )
        except (ConnectionError, OSError) as error:
            application.log(
                f"DXSPOT [{session.callsign}]: "
                f"no se pudo reenviar el comando: {error}"
            )
            writer.close()


def blocked_dxspot_command(command: str) -> bool:
    parts = command.casefold().split()
    return bool(parts) and parts[0] in {
        "set/skimmer",
        "set/seeme",
    }


async def authenticate_client(
    application: Any,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    peer: str,
) -> tuple[str, TelnetDecoder, bytearray] | None:
    decoder = TelnetDecoder()
    pending = bytearray()
    try:
        writer.write(
            welcome_block(application.config.server.welcome)
        )
        writer.write(prompt_message("Login: ").encode("utf-8"))
        await writer.drain()
        while b"\n" not in pending:
            chunk = await asyncio.wait_for(
                reader.read(4096),
                timeout=application.config.server.client_timeout_seconds,
            )
            if not chunk:
                return None
            text, reply = decoder.feed(chunk)
            if reply:
                writer.write(reply)
                await writer.drain()
            pending.extend(text)
            if b"\n" not in pending and len(pending) > 256:
                raise ValueError("login demasiado largo")

        raw_login, _, remainder = pending.partition(b"\n")
        configured_login = raw_login.rstrip(b"\r").decode(
            "utf-8",
            errors="replace",
        ).strip()
        client_callsign = callsign(configured_login, "login").upper()
        writer.write(
            (
                prompt_message("Login OK", client_callsign)
                + "\r\n"
            ).encode("utf-8")
        )
        await writer.drain()
        return client_callsign, decoder, bytearray(remainder)
    except asyncio.TimeoutError:
        application.log(
            f"cliente {peer} desconectado: no envió login durante "
            f"{application.config.server.client_timeout_seconds:g}s"
        )
    except ValueError as error:
        writer.write(
            (
                prompt_message("Login rejected / Login no aceptado")
                + "\r\n"
            ).encode("utf-8")
        )
        await asyncio.gather(writer.drain(), return_exceptions=True)
        application.log(f"cliente {peer} rechazado: {error}")
    except (ConnectionError, OSError) as error:
        application.log(f"cliente {peer} durante login: {error}")
    return None


async def select_client_language(
    application: Any,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    decoder: TelnetDecoder,
    pending: bytearray,
    peer: str,
    client_callsign: str,
) -> tuple[str, bytearray] | None:
    prompt = prompt_message(
        "Idioma / Language [ES/EN]: ",
        client_callsign,
    )
    invalid = prompt_message(
        "Opción no válida / Invalid option",
        client_callsign,
    )
    try:
        while not application.stop.is_set():
            writer.write(prompt.encode("utf-8"))
            await writer.drain()
            while b"\n" not in pending:
                chunk = await asyncio.wait_for(
                    reader.read(4096),
                    timeout=(
                        application.config.server.client_timeout_seconds
                    ),
                )
                if not chunk:
                    return None
                text, reply = decoder.feed(chunk)
                if reply:
                    writer.write(reply)
                    await writer.drain()
                pending.extend(text)
                if len(pending) > 32:
                    raise ValueError("selección de idioma demasiado larga")

            raw_language, _, remainder = pending.partition(b"\n")
            pending = bytearray(remainder)
            language = raw_language.rstrip(b"\r").decode(
                "utf-8",
                errors="replace",
            ).strip().casefold()
            if language in ("es", "en"):
                return language, pending
            writer.write((invalid + "\r\n").encode("utf-8"))
            await writer.drain()
    except asyncio.TimeoutError:
        application.log(
            f"cliente {peer} desconectado: no seleccionó idioma"
        )
    except (ConnectionError, OSError, ValueError) as error:
        application.log(
            f"cliente {peer} durante selección de idioma: {error}"
        )
    return None


async def client_loop(
    application: Any,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    identifier = application.next_client_identifier
    application.next_client_identifier += 1
    peer_info = writer.get_extra_info("peername")
    peer = (
        f"{peer_info[0]}:{peer_info[1]}"
        if isinstance(peer_info, tuple) and len(peer_info) >= 2
        else str(peer_info)
    )
    application.log(f"cliente entrante {peer}: conexión aceptada")
    try:
        authentication = await authenticate_client(
            application,
            reader,
            writer,
            peer,
        )
    except asyncio.CancelledError:
        await close_writer(writer)
        raise
    if authentication is None:
        await close_writer(writer)
        return
    client_callsign, decoder, pending = authentication
    saved_profile = application.profile_store.get(client_callsign)
    if saved_profile is None:
        saved_language = None
        enabled_sources = set(application.config.server.default_sources)
        spot_filters: dict[int, SpotFilter] = {}
        filter_all_sources = False
        ads_enabled = False
        ads_window_seconds = ADS_WINDOW_SECONDS
        beacon_enabled = True
        ads_min_spotters = ADS_DEFAULT_QUALITY
        seeme_enabled = False
    else:
        (
            saved_sources,
            saved_filters,
            filter_all_sources,
            ads_enabled,
            ads_window_seconds,
            beacon_enabled,
            ads_min_spotters,
            seeme_enabled,
            saved_language,
        ) = saved_profile
        enabled_sources = set(saved_sources)
        spot_filters = dict(saved_filters)
    if saved_language is None:
        language_selection = await select_client_language(
            application,
            reader,
            writer,
            decoder,
            pending,
            peer,
            client_callsign,
        )
        if language_selection is None:
            await close_writer(writer)
            return
        client_language, pending = language_selection
    else:
        client_language = saved_language
    session = ClientSession(
        identifier=identifier,
        peer=peer,
        writer=writer,
        queue=asyncio.Queue(
            maxsize=application.config.server.client_queue_lines
        ),
        callsign=client_callsign,
        language=client_language,
        enabled_sources=enabled_sources,
        spot_filters=spot_filters,
        filter_all_sources=filter_all_sources,
        ads_enabled=ads_enabled,
        ads_window_seconds=ads_window_seconds,
        beacon_enabled=beacon_enabled,
        ads_min_spotters=ads_min_spotters,
        seeme_enabled=seeme_enabled,
    )
    session.stats.state = "CONECTADO"
    session.stats.connected_at = session.connected_at
    queue_connection_summary(session)
    application.clients[identifier] = session
    application.profile_store.save(session)
    application.log(f"cliente {client_callsign} ({peer}) conectado")
    writer_task = asyncio.create_task(
        client_writer(application, session),
        name=f"client-writer-{identifier}",
    )
    dxspot_source = next(
        source
        for source in application.config.sources
        if source.key == "dxcluster"
    )
    dxspot_task = (
        asyncio.create_task(
            client_dxspot_loop(application, session, dxspot_source),
            name=f"client-dxspot-{identifier}",
        )
        if dxspot_source.enabled
        else None
    )
    requested_disconnect = False
    try:
        while not application.stop.is_set():
            if b"\n" not in pending:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                session.last_rx = time.monotonic()
                session.stats.record_rx(len(chunk), line=False)
                text, reply = decoder.feed(chunk)
                if reply:
                    writer.write(reply)
                    await writer.drain()
                    session.stats.record_tx(len(reply), line=False)
                pending.extend(text)
                if len(pending) > 65_536:
                    application.log(
                        f"cliente {client_callsign} desconectado: "
                        "línea demasiado larga"
                    )
                    break
            should_quit = False
            while b"\n" in pending:
                raw_line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                command = raw_line.rstrip(b"\r").decode(
                    "utf-8",
                    errors="replace",
                ).strip()
                command_result = handle_aggregator_command(
                    session,
                    command,
                )
                if command_result == "disconnect":
                    requested_disconnect = True
                    should_quit = True
                    break
                if command_result == "changed":
                    application.flush_ads(session)
                    application.profile_store.save(session)
                    continue
                if command_result == "handled":
                    continue
                if command:
                    if blocked_dxspot_command(command):
                        queue_unknown_command(session)
                        application.log(
                            f"DXSPOT [{session.callsign}]: "
                            f"comando bloqueado: {command}"
                        )
                        continue
                    await forward_to_dxspot(
                        application,
                        session,
                        raw_line + b"\n",
                    )
            if should_quit:
                break
    except (ConnectionError, OSError, asyncio.QueueFull) as error:
        application.log(f"cliente {peer}: {error}")
    finally:
        application.clients.pop(identifier, None)
        application.discard_ads(session)
        if dxspot_task is not None:
            dxspot_task.cancel()
        writer_task.cancel()
        await asyncio.gather(
            *(task for task in (dxspot_task,) if task is not None),
            writer_task,
            return_exceptions=True,
        )
        if requested_disconnect and not writer.is_closing():
            disconnect_message = prompt_message(
                client_text(session.language, "goodbye"),
                session.callsign,
            )
            try:
                await asyncio.wait_for(
                    send_line(writer, disconnect_message),
                    timeout=1,
                )
                session.stats.record_tx(
                    len(disconnect_message.encode("utf-8")) + 2,
                    line=False,
                )
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass
        await close_writer(writer)
        application.log(f"cliente {client_callsign} ({peer}) desconectado")
