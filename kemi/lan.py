"""Zero-config LAN discovery: ships on the same network find each other.

Nodes multicast a small signed-free beacon (it only claims connectivity;
everything that matters is verified at the protocol layer anyway). A new
node listens for a moment before bootstrapping - on a home, office or
classroom network this makes joining literally zero-config: run
``kemi katil`` on two machines and they form a fleet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
from typing import Any

log = logging.getLogger("kemi.lan")

GROUP = "239.255.77.73"   # 77/73 = "MI" - kemi's multicast street address
PORT = 53530
BEACON_INTERVAL = 5.0


def _make_socket(bind: bool) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    if bind:
        sock.bind(("", PORT))
        membership = struct.pack("4s4s", socket.inet_aton(GROUP),
                                 socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.setblocking(False)
    return sock


class _Listener(asyncio.DatagramProtocol):
    def __init__(self, found: dict[str, tuple[str, int]], own_id: str):
        self.found = found
        self.own_id = own_id

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            beacon = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if beacon.get("kemi") != 1:
            return
        node_id = str(beacon.get("id", ""))
        port = beacon.get("port")
        if node_id and node_id != self.own_id and isinstance(port, int):
            self.found[node_id] = (addr[0], port)


async def discover(own_id: str = "", timeout: float = 1.5) -> list[tuple[str, int]]:
    """Listen briefly for fleet beacons on the local network."""
    found: dict[str, tuple[str, int]] = {}
    loop = asyncio.get_running_loop()
    try:
        sock = _make_socket(bind=True)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Listener(found, own_id), sock=sock)
    except OSError as exc:
        log.debug("LAN discovery unavailable: %s", exc)
        return []
    try:
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return list(found.values())


class LanBeacon:
    """Periodically announces this node to the local network."""

    def __init__(self, node_id: str, tcp_port: int, name: str = ""):
        self.payload = json.dumps(
            {"kemi": 1, "id": node_id, "port": tcp_port, "name": name},
            separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._sock: socket.socket | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        try:
            self._sock = _make_socket(bind=False)
        except OSError as exc:
            log.debug("LAN beacon unavailable: %s", exc)
            return
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        assert self._sock is not None
        while True:
            try:
                self._sock.sendto(self.payload, (GROUP, PORT))
            except OSError:
                pass
            await asyncio.sleep(BEACON_INTERVAL)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._sock is not None:
            self._sock.close()


def beacon_payload_of(node: Any) -> "LanBeacon":
    from .names import ship_name

    return LanBeacon(node.identity.node_id, node.port,
                     ship_name(node.identity.node_id))
