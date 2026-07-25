"""Primitivas del protocolo Telnet usadas en ambos sentidos."""

from __future__ import annotations

import asyncio


IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240


class TelnetDecoder:
    """Separa datos de usuario y negociación Telnet."""

    def __init__(self) -> None:
        self.state = "data"
        self.command = 0

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        text = bytearray()
        reply = bytearray()
        for byte in data:
            if self.state == "data":
                if byte == IAC:
                    self.state = "iac"
                else:
                    text.append(byte)
            elif self.state == "iac":
                if byte == IAC:
                    text.append(IAC)
                    self.state = "data"
                elif byte in (DO, DONT, WILL, WONT):
                    self.command = byte
                    self.state = "option"
                elif byte == SB:
                    self.state = "subnegotiation"
                else:
                    self.state = "data"
            elif self.state == "option":
                if self.command == WILL:
                    reply.extend((IAC, DONT, byte))
                elif self.command == DO:
                    reply.extend((IAC, WONT, byte))
                self.state = "data"
            elif self.state == "subnegotiation":
                if byte == IAC:
                    self.state = "subnegotiation_iac"
            elif self.state == "subnegotiation_iac":
                self.state = "data" if byte == SE else "subnegotiation"
        return bytes(text), bytes(reply)


async def send_line(writer: asyncio.StreamWriter, value: str) -> None:
    writer.write(value.encode("utf-8") + b"\r\n")
    await writer.drain()


async def close_writer(
    writer: asyncio.StreamWriter,
    *,
    timeout: float = 1,
) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=timeout)
    except (ConnectionError, OSError, asyncio.TimeoutError):
        transport = writer.transport
        if transport is not None:
            transport.abort()
