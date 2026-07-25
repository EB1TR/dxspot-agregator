"""Carga y validación de la configuración JSON."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .constants import DEFAULT_COUNTRY_FILE_URL, SOURCE_KEYS


@dataclass(frozen=True)
class SourceConfig:
    key: str
    name: str
    host: str
    port: int
    commands: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    client_timeout_seconds: float
    client_queue_lines: int
    default_sources: frozenset[str]
    client_config_path: Path
    welcome: str


@dataclass(frozen=True)
class CountryFileConfig:
    enabled: bool
    url: str
    cache_path: Path
    download_timeout_seconds: float
    update_interval_seconds: float


@dataclass(frozen=True)
class WebConfig:
    enabled: bool
    host: str
    port: int


@dataclass(frozen=True)
class AppConfig:
    login: str
    dashboard: bool
    refresh_seconds: float
    web: WebConfig
    upstream_keepalive_seconds: float
    upstream_keepalive_command: str
    reconnect_initial_seconds: float
    reconnect_max_seconds: float
    country_file: CountryFileConfig
    server: ServerConfig
    sources: tuple[SourceConfig, ...]


def number(value: object, path: str, *, minimum: float = 0) -> float:
    if type(value) not in (int, float) or float(value) <= minimum:
        raise ValueError(f"{path} debe ser numérico y mayor que {minimum}")
    return float(value)


def port(value: object, path: str, *, allow_zero: bool = False) -> int:
    lower = 0 if allow_zero else 1
    if type(value) is not int or not lower <= value <= 65535:
        suffix = "0 y 65535" if allow_zero else "1 y 65535"
        raise ValueError(f"{path} debe estar entre {suffix}")
    return value


def positive_int(value: object, path: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{path} debe ser un entero mayor que cero")
    return value


def string(value: object, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "una cadena" if allow_empty else "una cadena no vacía"
        raise ValueError(f"{path} debe ser {qualifier}")
    return value


def callsign(value: object, path: str = "login") -> str:
    configured_callsign = string(value, path)
    pattern = re.compile(
        r"(?=[A-Za-z0-9-]*[A-Za-z])"
        r"(?=[A-Za-z0-9-]*\d)"
        r"[A-Za-z0-9]+(?:-(?:[0-9]|1[0-5]))?"
    )
    if pattern.fullmatch(configured_callsign) is None:
        raise ValueError(
            f"{path} debe ser un único indicativo, opcionalmente seguido "
            "de un SSID entre 0 y 15"
        )
    return configured_callsign


def load_config(path: Path | None = None) -> AppConfig:
    path = path or Path(__file__).resolve().parent.parent / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SystemExit(
            f"No existe {path}. Copia config.example.json como config.json."
        ) from error
    except json.JSONDecodeError as error:
        raise SystemExit(f"JSON no válido en {path}: {error}") from error

    try:
        if not isinstance(raw, dict):
            raise ValueError("la raíz debe ser un objeto")
        login = callsign(raw.get("login"), "login")

        general = raw.get("general", {})
        if not isinstance(general, dict):
            raise ValueError("general debe ser un objeto")
        dashboard = general.get("dashboard", True)
        if not isinstance(dashboard, bool):
            raise ValueError("general.dashboard debe ser true o false")
        refresh_seconds = number(
            general.get("refresh_seconds", 1),
            "general.refresh_seconds",
        )

        web_raw = raw.get("web", {})
        if not isinstance(web_raw, dict):
            raise ValueError("web debe ser un objeto")
        web_enabled = web_raw.get("enabled", True)
        if not isinstance(web_enabled, bool):
            raise ValueError("web.enabled debe ser true o false")
        web = WebConfig(
            enabled=web_enabled,
            host=string(
                os.environ.get(
                    "DXA_WEB_HOST",
                    web_raw.get("host", "127.0.0.1"),
                ),
                "web.host",
            ),
            port=port(
                (
                    int(os.environ["DXA_WEB_PORT"])
                    if "DXA_WEB_PORT" in os.environ
                    else web_raw.get("port", 8080)
                ),
                "web.port",
                allow_zero=True,
            ),
        )

        country_file_raw = raw.get("country_file", {})
        if not isinstance(country_file_raw, dict):
            raise ValueError("country_file debe ser un objeto")
        country_file_enabled = country_file_raw.get("enabled", True)
        if not isinstance(country_file_enabled, bool):
            raise ValueError("country_file.enabled debe ser true o false")
        configured_cache_path = Path(
            string(
                country_file_raw.get("cache_path", "data/cty.dat"),
                "country_file.cache_path",
            )
        )
        if not configured_cache_path.is_absolute():
            configured_cache_path = path.parent / configured_cache_path
        country_file = CountryFileConfig(
            enabled=country_file_enabled,
            url=string(
                country_file_raw.get("url", DEFAULT_COUNTRY_FILE_URL),
                "country_file.url",
            ),
            cache_path=configured_cache_path,
            download_timeout_seconds=number(
                country_file_raw.get("download_timeout_seconds", 15),
                "country_file.download_timeout_seconds",
            ),
            update_interval_seconds=number(
                country_file_raw.get("update_interval_seconds", 86_400),
                "country_file.update_interval_seconds",
            ),
        )

        upstream = raw.get("upstream", {})
        if not isinstance(upstream, dict):
            raise ValueError("upstream debe ser un objeto")
        keepalive_seconds = number(
            upstream.get("keepalive_seconds", 180),
            "upstream.keepalive_seconds",
        )
        keepalive_command = string(
            upstream.get("keepalive_command", ""),
            "upstream.keepalive_command",
            allow_empty=True,
        )
        reconnect_initial = number(
            upstream.get("reconnect_initial_seconds", 1),
            "upstream.reconnect_initial_seconds",
        )
        reconnect_max = number(
            upstream.get("reconnect_max_seconds", 30),
            "upstream.reconnect_max_seconds",
        )
        if reconnect_max < reconnect_initial:
            raise ValueError(
                "upstream.reconnect_max_seconds no puede ser menor que "
                "reconnect_initial_seconds"
            )

        server_raw = raw.get("server")
        if not isinstance(server_raw, dict):
            raise ValueError("server debe ser un objeto")
        default_sources_raw = server_raw.get(
            "default_sources",
            ["dxcluster"],
        )
        if not isinstance(default_sources_raw, list) or not all(
            isinstance(source, str) for source in default_sources_raw
        ):
            raise ValueError("server.default_sources debe ser una lista")
        default_sources = frozenset(default_sources_raw)
        unknown_default_sources = default_sources - set(SOURCE_KEYS)
        if unknown_default_sources:
            raise ValueError(
                "server.default_sources contiene fuentes desconocidas: "
                + ", ".join(sorted(unknown_default_sources))
            )
        client_config_path = Path(
            string(
                server_raw.get("client_config_path", "data/clients.json"),
                "server.client_config_path",
            )
        )
        if not client_config_path.is_absolute():
            client_config_path = path.parent / client_config_path
        server = ServerConfig(
            host=string(
                os.environ.get(
                    "DXA_SERVER_HOST",
                    server_raw.get("host", "127.0.0.1"),
                ),
                "server.host",
            ),
            port=port(
                (
                    int(os.environ["DXA_SERVER_PORT"])
                    if "DXA_SERVER_PORT" in os.environ
                    else server_raw.get("port", 7300)
                ),
                "server.port",
                allow_zero=True,
            ),
            client_timeout_seconds=number(
                server_raw.get("client_timeout_seconds", 300),
                "server.client_timeout_seconds",
            ),
            client_queue_lines=positive_int(
                server_raw.get("client_queue_lines", 1000),
                "server.client_queue_lines",
            ),
            default_sources=default_sources,
            client_config_path=client_config_path,
            welcome=string(
                server_raw.get(
                    "welcome",
                    "DXSpot Agregator",
                ),
                "server.welcome",
                allow_empty=True,
            ),
        )

        source_raw = raw.get("sources")
        if not isinstance(source_raw, dict):
            raise ValueError("sources debe ser un objeto")
        unknown = set(source_raw) - set(SOURCE_KEYS)
        if unknown:
            raise ValueError("fuentes desconocidas: " + ", ".join(sorted(unknown)))
        sources: list[SourceConfig] = []
        for key in SOURCE_KEYS:
            item = source_raw.get(key)
            if not isinstance(item, dict):
                raise ValueError(f"sources.{key} debe ser un objeto")
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError(f"sources.{key}.enabled debe ser true o false")
            commands = item.get("commands", [])
            if not isinstance(commands, list) or not all(
                isinstance(command, str) for command in commands
            ):
                raise ValueError(f"sources.{key}.commands debe ser una lista")
            configured_host = item.get("host")
            host = (
                string(configured_host, f"sources.{key}.host")
                if enabled
                else (
                    configured_host
                    if isinstance(configured_host, str) and configured_host
                    else "—"
                )
            )
            sources.append(
                SourceConfig(
                    key=key,
                    name=string(item.get("name", key), f"sources.{key}.name"),
                    host=host,
                    port=port(item.get("port", 23), f"sources.{key}.port"),
                    commands=tuple(commands),
                    enabled=enabled,
                )
            )
        if not any(source.enabled for source in sources):
            raise ValueError("debe haber al menos una fuente habilitada")
    except (TypeError, ValueError) as error:
        raise SystemExit(f"Configuración incorrecta: {error}") from error

    return AppConfig(
        login=login,
        dashboard=dashboard,
        refresh_seconds=refresh_seconds,
        web=web,
        upstream_keepalive_seconds=keepalive_seconds,
        upstream_keepalive_command=keepalive_command,
        reconnect_initial_seconds=reconnect_initial,
        reconnect_max_seconds=reconnect_max,
        country_file=country_file,
        server=server,
        sources=tuple(sources),
    )
