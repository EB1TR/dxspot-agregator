"""Interfaz de línea de comandos y gestión de señales."""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

from .application import DXSpotAgregator
from .config import AppConfig, load_config


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.json"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Agregador de fuentes DX con clientes salientes "
            "y servidor Telnet."
        )
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=default_config_path(),
        help="ruta del archivo JSON de configuración",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="valida la configuración y termina",
    )
    return parser.parse_args()


async def async_main(config: AppConfig) -> None:
    application = DXSpotAgregator(config)
    loop = asyncio.get_running_loop()
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                handled_signal,
                application.stop.set,
            )
        except NotImplementedError:
            pass
    try:
        await application.run()
    finally:
        await application.close()


def main() -> None:
    arguments = parse_arguments()
    config = load_config(arguments.config)
    if arguments.check_config:
        print("Configuración correcta")
        return
    try:
        asyncio.run(async_main(config))
    except KeyboardInterrupt:
        pass
