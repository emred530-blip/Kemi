"""T18 — protocol fuzzing pass.

Throws a matrix of malformed and hostile inputs at every TCP handler and the
DHT datagram path, asserting the node never crashes: after each barrage a
fresh, valid request must still succeed. Garbage in must mean an error
response or a closed connection, never a dead peer.
"""

import asyncio
import json
import random
import socket
import struct
import unittest

from kemi.crypto import sign_envelope
from kemi.identity import Identity
from kemi.node import PeerNode
from kemi.protocol import request

DIFF = 4
_HEADER = struct.Struct(">I")


async def _raw_send(port: int, payload: bytes, read_reply: bool = True) -> bytes:
    """Send raw bytes to a node's TCP port; optionally read what comes back."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        if read_reply:
            try:
                return await asyncio.wait_for(reader.read(4096), timeout=3.0)
            except asyncio.TimeoutError:
                return b""
        return b""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def _framed(blob: bytes) -> bytes:
    return _HEADER.pack(len(blob)) + blob


class TcpFuzzTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1",
                             port=0, difficulty=DIFF, sandbox=False,
                             provide=True, price=1.0)
        await self.node.start()

    async def asyncTearDown(self):
        await self.node.stop()

    async def _assert_alive(self):
        response = await request("127.0.0.1", self.node.port,
                                 {"type": "node.info"}, timeout=5.0)
        self.assertTrue(response.get("ok"), "node stopped answering after fuzzing")

    async def test_malformed_frames(self):
        rng = random.Random(1234)
        payloads = [
            b"",                                   # nothing
            b"\x00",                               # truncated header
            b"\x00\x00\x00\x05ab",                 # length exceeds body
            _framed(b"not json at all"),           # bad JSON
            _framed(b"[1,2,3]"),                   # JSON but not an object
            _framed(b'"a string"'),
            _framed(b"12345"),
            _framed(b"null"),
            _framed(b"{"),                          # truncated JSON
            _framed(b"\xff\xfe\xfd"),               # invalid utf-8
            _framed(json.dumps({"no_type": 1}).encode()),
            _framed(json.dumps({"type": 12345}).encode()),   # non-string type
            _framed(json.dumps({"type": "no.such.method"}).encode()),
            b"\x7f\xff\xff\xff" + b"x" * 100,       # huge declared length, short body
            _framed(b"\x00" * 1000),                # framed NULs
        ]
        for payload in payloads:
            await _raw_send(self.node.port, payload)
            await _raw_send(self.node.port, payload, read_reply=False)
        # random fuzz on top
        for _ in range(60):
            length = rng.randint(0, 200)
            await _raw_send(self.node.port, bytes(rng.getrandbits(8)
                                                  for _ in range(length)))
        await self._assert_alive()

    async def test_structurally_valid_but_hostile_payloads(self):
        """Every dispatch type, fed fields of the wrong type/shape."""
        victim = Identity.create(difficulty=DIFF)
        garbage_tx = sign_envelope(victim.key, {"kind": "tx", "from": "x",
                                                "to": "y", "amount": "lots"})
        messages = [
            {"type": "task.execute"},                       # no task/items
            {"type": "task.execute", "task": "hash.sha256", "items": "notalist"},
            {"type": "task.execute", "task": "../../etc/passwd", "items": ["a"]},
            {"type": "task.execute", "task": "hash.sha256", "items": [], "payment": 5},
            {"type": "task.execute", "task": "hash.sha256", "items": ["a"],
             "enc": {"n": "zz", "c": "zz"}, "payment": garbage_tx},
            {"type": "ledger.push", "txs": "notalist"},
            {"type": "ledger.push", "txs": [1, 2, 3]},
            {"type": "ledger.push", "txs": [{"garbage": True}]},
            {"type": "ledger.pull", "cursor": "notanint"},
            {"type": "ledger.pull", "cursor": -999999999},
            {"type": "tx.witness", "tx": "notadict"},
            {"type": "tx.witness", "tx": {"bad": "envelope"}},
            {"type": "tx.witness", "tx": garbage_tx},
            {"type": "relay.forward", "to": 12345, "inner": "x"},
            {"type": "relay.forward"},
            {"type": "relay.register", "envelope": "notadict"},
            {"type": "relay.register", "envelope": {"bad": 1}},
            {"type": "relay.stream", "to": None, "inner": None},
            {"type": "task.stream", "task": "hash.sha256"},   # stream needs ai.generate
            {"type": "task.stream"},
            {"type": "ledger.snapshot", "extra": [1, 2, 3]},
            {"type": "node.info", "junk": "\x00" * 50},
        ]
        for message in messages:
            try:
                await asyncio.wait_for(
                    _raw_send(self.node.port, _framed(json.dumps(message).encode())),
                    timeout=5.0)
            except asyncio.TimeoutError:
                pass  # a stream type may hold the socket; that's fine
        await self._assert_alive()

    async def test_oversized_declared_length_is_refused(self):
        # declare a body far over MAX_MESSAGE_BYTES; node must not allocate it
        await _raw_send(self.node.port, b"\xff\xff\xff\xff" + b"x" * 10)
        await self._assert_alive()


class DhtFuzzTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1",
                             port=0, difficulty=DIFF, sandbox=False)
        await self.node.start()

    async def asyncTearDown(self):
        await self.node.stop()

    async def test_malformed_datagrams(self):
        rng = random.Random(99)
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            datagrams = [
                b"", b"{", b"not json", b"\xff\xfe\xfd", b"[1,2,3]", b"null",
                b'"str"', json.dumps({"type": "store"}).encode(),
                json.dumps({"type": "store", "key": 123, "value": "x"}).encode(),
                json.dumps({"type": "find_value"}).encode(),
                json.dumps({"type": "find_node", "target": None}).encode(),
                json.dumps({"from": {"id": "short"}, "type": "ping"}).encode(),
                json.dumps({"resp_to": "nope"}).encode(),
                b"\x00" * 70000,  # over MAX_DATAGRAM
            ]
            async def shove(blob: bytes) -> None:
                # An over-MTU datagram may be unsendable locally (OSError); that
                # trivially cannot reach the node, so skipping it is correct.
                try:
                    await loop.sock_sendto(sock, blob, ("127.0.0.1", self.node.port))
                except OSError:
                    pass

            for datagram in datagrams:
                await shove(datagram)
            for _ in range(60):
                await shove(bytes(rng.getrandbits(8)
                                  for _ in range(rng.randint(0, 300))))
            await asyncio.sleep(0.2)
        finally:
            sock.close()

        # the DHT must still answer a well-formed ping via a peer lookup
        other = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1",
                         port=0, difficulty=DIFF, sandbox=False,
                         bootstrap=[("127.0.0.1", self.node.port)])
        await other.start()
        try:
            contacts = await other.dht.lookup(other.identity.node_id)
            self.assertTrue(any(c.port == self.node.port for c in contacts),
                            "DHT stopped responding after datagram fuzzing")
        finally:
            await other.stop()


if __name__ == "__main__":
    unittest.main()
