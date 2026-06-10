"""Wire protocol: length-prefixed JSON messages over TCP (asyncio streams).

Every message is a JSON object with a ``type`` field. Requests are sent on a
fresh connection and answered with a single response message, after which the
connection is closed. This keeps the protocol trivially stateless and easy to
implement in other languages.
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

# Hard cap so a malicious peer cannot make us allocate unbounded memory.
MAX_MESSAGE_BYTES = 32 * 1024 * 1024

_HEADER = struct.Struct(">I")


class ProtocolError(Exception):
    """Raised when a peer violates the wire protocol."""


async def send_message(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    data = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message too large: {len(data)} bytes")
    writer.write(_HEADER.pack(len(data)) + data)
    await writer.drain()


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    header = await reader.readexactly(_HEADER.size)
    (length,) = _HEADER.unpack(header)
    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"declared message size too large: {length} bytes")
    data = await reader.readexactly(length)
    message = json.loads(data.decode("utf-8"))
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    return message


async def request(
    host: str,
    port: int,
    message: dict[str, Any],
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Open a connection, send one request and return the single response."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout
    )
    try:
        await send_message(writer, message)
        return await asyncio.wait_for(read_message(reader), timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def error(code: str, detail: str = "") -> dict[str, Any]:
    return {"ok": False, "error": code, "detail": detail}
