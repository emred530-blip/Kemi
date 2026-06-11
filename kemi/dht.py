"""Kademlia-style distributed hash table over UDP.

This replaces the tracker for peer discovery: provider records are stored
under task-derived keys on the K nodes whose ids are XOR-closest to the key
(exactly how BitTorrent's trackerless mode works, BEP 5).

Extras for real-world operation:

* every response echoes the requester's *observed* public address, which is
  how a node behind NAT learns its external endpoint without any STUN server;
* stored values must be signed envelopes with a fresh timestamp and expire
  after their TTL, so the table cannot be polluted with stale or forged
  records;
* node ids carry the identity proof-of-work, raising the cost of eclipse
  and Sybil attacks on the keyspace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Any

from .crypto import digest, open_envelope
from .identity import POW_DIFFICULTY_BITS, Identity, verify_node_id

log = logging.getLogger("kemi.dht")

K = 8                    # bucket size / replication factor
ALPHA = 3                # lookup parallelism
RPC_TIMEOUT = 2.0
MAX_DATAGRAM = 60_000
MAX_VALUES_PER_KEY = 64
DEFAULT_TTL = 90.0
MAX_RECORD_AGE = 180.0   # reject stored envelopes older than this

ID_BITS = 256


def _distance(a: str, b: str) -> int:
    return int(a, 16) ^ int(b, 16)


@dataclass(frozen=True)
class Contact:
    node_id: str
    host: str
    port: int  # nodes bind UDP (DHT) and TCP (services) to the same port

    def as_wire(self) -> list[Any]:
        return [self.node_id, self.host, self.port]

    @classmethod
    def from_wire(cls, item: Any) -> "Contact | None":
        try:
            node_id, host, port = item
            if not (isinstance(node_id, str) and len(node_id) == 64):
                return None
            return cls(node_id, str(host), int(port))
        except (TypeError, ValueError):
            return None


class RoutingTable:
    """K-buckets indexed by shared-prefix length with our own id."""

    def __init__(self, own_id: str):
        self.own_id = own_id
        self._buckets: dict[int, OrderedDict[str, Contact]] = {}

    def _bucket_index(self, node_id: str) -> int:
        d = _distance(self.own_id, node_id)
        return d.bit_length() - 1 if d else 0

    def add(self, contact: Contact) -> None:
        if contact.node_id == self.own_id:
            return
        bucket = self._buckets.setdefault(self._bucket_index(contact.node_id), OrderedDict())
        bucket.pop(contact.node_id, None)
        bucket[contact.node_id] = contact  # most recently seen at the end
        while len(bucket) > K:
            bucket.popitem(last=False)

    def remove(self, node_id: str) -> None:
        bucket = self._buckets.get(self._bucket_index(node_id))
        if bucket:
            bucket.pop(node_id, None)

    def closest(self, target: str, count: int = K) -> list[Contact]:
        contacts = [c for bucket in self._buckets.values() for c in bucket.values()]
        contacts.sort(key=lambda c: _distance(c.node_id, target))
        return contacts[:count]

    def all_contacts(self) -> list[Contact]:
        return [c for bucket in self._buckets.values() for c in bucket.values()]

    def __len__(self) -> int:
        return sum(len(b) for b in self._buckets.values())


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, node: "DHTNode"):
        self.node = node

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict):
            return
        self.node._on_datagram(message, addr)


class DHTNode:
    def __init__(self, identity: Identity, host: str = "0.0.0.0", port: int = 0,
                 difficulty: int = POW_DIFFICULTY_BITS):
        self.identity = identity
        self.host = host
        self.port = port
        self.difficulty = difficulty
        self.table = RoutingTable(identity.node_id)
        # key -> value_id -> (envelope, expires_at)
        self._storage: dict[str, dict[str, tuple[dict, float]]] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._observed: Counter[tuple[str, int]] = Counter()
        self._transport: asyncio.DatagramTransport | None = None

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self), local_addr=(self.host, self.port)
        )
        self.port = self._transport.get_extra_info("sockname")[1]
        log.debug("dht %s listening on udp %s:%s", self.identity.short_id, self.host, self.port)

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    @property
    def observed_endpoint(self) -> tuple[str, int] | None:
        """Our public address as most peers see it (NAT discovery)."""
        if not self._observed:
            return None
        return self._observed.most_common(1)[0][0]

    def nat_detected(self, local_port: int) -> bool:
        observed = self.observed_endpoint
        return observed is not None and observed[1] != local_port

    # -- datagram plumbing -------------------------------------------------------

    def _contact_header(self) -> dict[str, Any]:
        return {
            "id": self.identity.node_id,
            "pow_nonce": self.identity.pow_nonce,
            "pubkey": self.identity.public_key_hex,
            "port": self.port,
        }

    def _send(self, message: dict[str, Any], addr: tuple[str, int]) -> None:
        if self._transport is None or self._transport.is_closing():
            return
        data = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_DATAGRAM:
            log.warning("dropping oversized datagram (%d bytes)", len(data))
            return
        self._transport.sendto(data, addr)

    def _on_datagram(self, message: dict[str, Any], addr: tuple[str, int]) -> None:
        sender = message.get("from")
        if isinstance(sender, dict):
            node_id = sender.get("id", "")
            if (
                node_id != self.identity.node_id
                and verify_node_id(node_id, sender.get("pubkey", ""),
                                   sender.get("pow_nonce", -1), self.difficulty)
            ):
                # Trust the source IP we actually heard from, not a claim.
                self.table.add(Contact(node_id, addr[0], int(sender.get("port", addr[1]))))

        if "resp_to" in message:
            future = self._pending.pop(message["resp_to"], None)
            if future is not None and not future.done():
                future.set_result(message)
            observed = message.get("observed")
            if isinstance(observed, list) and len(observed) == 2:
                try:
                    self._observed[(str(observed[0]), int(observed[1]))] += 1
                except (TypeError, ValueError):
                    pass
            return

        response = self._handle_request(message, addr)
        if response is not None:
            response["resp_to"] = message.get("rpc", "")
            response["from"] = self._contact_header()
            response["observed"] = [addr[0], addr[1]]
            self._send(response, addr)

    async def _rpc(self, contact: Contact, message: dict[str, Any]) -> dict[str, Any] | None:
        rpc_id = secrets.token_hex(8)
        message = {**message, "rpc": rpc_id, "from": self._contact_header()}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = future
        self._send(message, (contact.host, contact.port))
        try:
            return await asyncio.wait_for(future, timeout=RPC_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._pending.pop(rpc_id, None)
            self.table.remove(contact.node_id)
            return None

    # -- request handlers ----------------------------------------------------------

    def _handle_request(self, message: dict[str, Any], addr: tuple[str, int]) -> dict[str, Any] | None:
        msg_type = message.get("type")
        if msg_type == "ping":
            return {"type": "pong"}
        if msg_type == "find_node":
            target = str(message.get("target", ""))
            return {"type": "nodes", "nodes": [c.as_wire() for c in self.table.closest(target)]}
        if msg_type == "find_value":
            key = str(message.get("key", ""))
            self._expire(key)
            values = [env for env, _ in self._storage.get(key, {}).values()]
            if values:
                return {"type": "values", "values": values}
            return {"type": "nodes", "nodes": [c.as_wire() for c in self.table.closest(key)]}
        if msg_type == "store":
            return self._handle_store(message)
        return None

    def _handle_store(self, message: dict[str, Any]) -> dict[str, Any]:
        key = str(message.get("key", ""))
        envelope = message.get("value")
        ttl = min(float(message.get("ttl", DEFAULT_TTL)), 15 * 60)
        if len(key) != 64 or not isinstance(envelope, dict):
            return {"type": "store_result", "stored": False}
        payload = open_envelope(envelope)
        if payload is None:
            return {"type": "store_result", "stored": False}
        ts = payload.get("ts")
        now = time.time()
        if not isinstance(ts, (int, float)) or not (now - MAX_RECORD_AGE < ts < now + 30):
            return {"type": "store_result", "stored": False}
        slot = self._storage.setdefault(key, {})
        self._expire(key)
        if len(slot) >= MAX_VALUES_PER_KEY:
            return {"type": "store_result", "stored": False}
        slot[digest(envelope)] = (envelope, now + ttl)
        return {"type": "store_result", "stored": True}

    def _expire(self, key: str) -> None:
        slot = self._storage.get(key)
        if not slot:
            return
        now = time.time()
        for value_id in [vid for vid, (_, exp) in slot.items() if exp < now]:
            del slot[value_id]

    # -- iterative operations -----------------------------------------------------

    async def bootstrap(self, peers: list[tuple[str, int]]) -> None:
        """Join the network through any existing peer(s) - no special role."""
        for host, port in peers:
            await self._rpc(Contact("0" * 64, host, port), {"type": "ping"})
        await self.lookup(self.identity.node_id)

    async def lookup(self, target: str) -> list[Contact]:
        """Iterative FIND_NODE: returns the K globally closest contacts."""
        shortlist = {c.node_id: c for c in self.table.closest(target, K)}
        queried: set[str] = set()
        while True:
            candidates = sorted(
                (c for c in shortlist.values() if c.node_id not in queried),
                key=lambda c: _distance(c.node_id, target),
            )[:ALPHA]
            if not candidates:
                break
            queried.update(c.node_id for c in candidates)
            responses = await asyncio.gather(
                *(self._rpc(c, {"type": "find_node", "target": target}) for c in candidates)
            )
            for response in responses:
                if response is None:
                    continue
                for item in response.get("nodes", []):
                    contact = Contact.from_wire(item)
                    if contact is not None and contact.node_id != self.identity.node_id:
                        shortlist.setdefault(contact.node_id, contact)
        ranked = sorted(shortlist.values(), key=lambda c: _distance(c.node_id, target))
        return ranked[:K]

    async def put(self, key: str, envelope: dict[str, Any], ttl: float = DEFAULT_TTL) -> int:
        """Store a signed envelope on the K nodes closest to the key."""
        targets = await self.lookup(key)
        # Also keep a local copy if we are one of the closest (or alone).
        own_distance = _distance(self.identity.node_id, key)
        if len(targets) < K or any(_distance(c.node_id, key) > own_distance for c in targets):
            self._handle_store({"key": key, "value": envelope, "ttl": ttl})
        responses = await asyncio.gather(
            *(self._rpc(c, {"type": "store", "key": key, "value": envelope, "ttl": ttl})
              for c in targets)
        )
        return sum(1 for r in responses if r and r.get("stored"))

    async def get(self, key: str) -> list[dict[str, Any]]:
        """Collect signed envelopes stored under a key across the swarm."""
        values: dict[str, dict[str, Any]] = {}
        self._expire(key)
        for envelope, _ in self._storage.get(key, {}).values():
            values[digest(envelope)] = envelope
        targets = await self.lookup(key)
        responses = await asyncio.gather(
            *(self._rpc(c, {"type": "find_value", "key": key}) for c in targets)
        )
        for response in responses:
            if response is None:
                continue
            for envelope in response.get("values", []):
                if isinstance(envelope, dict) and open_envelope(envelope) is not None:
                    values[digest(envelope)] = envelope
        return list(values.values())
