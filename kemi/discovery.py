"""Provider discovery on top of the DHT.

Providers periodically publish a signed *provider record* under one
DHT key per task they offer (``sha256("kemi:task:<name>")``). Consumers
fetch and verify those records - no tracker, no central registry. Records
are short-lived and re-announced, so departed providers fade out on
their own.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from .crypto import open_envelope, sign_envelope
from .dht import DHTNode
from .identity import Identity, verify_node_id

ANNOUNCE_TTL = 90.0
ANNOUNCE_INTERVAL = 30.0
MAX_RECORD_AGE = 120.0


def task_key(task: str) -> str:
    return hashlib.sha256(f"kemi:task:{task}".encode("utf-8")).hexdigest()


def make_provider_record(
    identity: Identity,
    host: str,
    port: int,
    price: float,
    tasks: list[str],
    resources: dict[str, Any],
    relay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "kind": "provider",
        "node_id": identity.node_id,
        "pow_nonce": identity.pow_nonce,
        "host": host,
        "port": port,
        "price": round(float(price), 6),
        "tasks": tasks,
        "resources": resources,
        "relay": relay,
        "e2e": True,  # this provider accepts end-to-end encrypted payloads
        "ts": round(time.time(), 3),
    }
    return sign_envelope(identity.key, payload)


async def announce_provider(dht: DHTNode, record: dict[str, Any]) -> None:
    payload = record["payload"]
    for task in payload["tasks"]:
        await dht.put(task_key(task), record, ttl=ANNOUNCE_TTL)


async def find_providers(dht: DHTNode, task: str) -> list[dict[str, Any]]:
    """Return verified, fresh provider payloads for a task (latest per node)."""
    now = time.time()
    fresh: dict[str, dict[str, Any]] = {}
    for envelope in await dht.get(task_key(task)):
        payload = open_envelope(envelope)
        if payload is None or payload.get("kind") != "provider":
            continue
        node_id = payload.get("node_id", "")
        if not verify_node_id(node_id, envelope["pubkey"], payload.get("pow_nonce", -1),
                              dht.difficulty):
            continue
        ts = payload.get("ts", 0)
        if not isinstance(ts, (int, float)) or now - ts > MAX_RECORD_AGE:
            continue
        if task not in payload.get("tasks", []):
            continue
        current = fresh.get(node_id)
        if current is None or ts > current["ts"]:
            # The signing key is bound to the node id (checked above), so the
            # consumer can use it to derive an end-to-end encryption key.
            fresh[node_id] = {**payload, "pubkey": envelope["pubkey"]}
    return list(fresh.values())
