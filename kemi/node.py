"""The unified Kemi peer.

Every participant runs the same node - there are no special roles and no
tracker. A peer always carries:

* a Kademlia DHT endpoint (UDP) for discovery and NAT address detection,
* a gossip-replicated ledger replica with anti-entropy sync (TCP),
* a local reputation store.

Optionally it also *provides* compute: announcing signed provider records
to the DHT, executing chunks in a sandbox, and accepting signed credit
transfers as payment. Peers behind NAT keep a persistent connection to any
public peer, which then relays task traffic to them (TURN-style).

TCP and UDP share the same port number, so one ``host:port`` is all you
need to address a peer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import random
import shutil
import subprocess
import time
from typing import Any

from .ai_backends import AIBackend, load_backend
from .crypto import canonical, open_envelope, sign_envelope
from .dht import DHTNode
from .e2e import E2EError, derive_box_key, open_sealed, seal
from .discovery import ANNOUNCE_INTERVAL, announce_provider, make_provider_record
from .gossip_ledger import GossipLedger
from .identity import POW_DIFFICULTY_BITS, Identity, verify_node_id
from .protocol import (NETWORK_ERRORS, ProtocolError, error, ok, read_message,
                       request, send_message)
from .reputation import ReputationStore
from .sandbox import UNSANDBOXED_TASKS, SandboxError, run_sandboxed
from .tasks import BACKEND_REQUIRED, TASKS, TaskError, run_task

log = logging.getLogger("kemi.node")

GOSSIP_FANOUT = 3
GOSSIP_INTERVAL = 3.0
RELAY_FORWARD_TIMEOUT = 150.0
RELAY_RECONNECT_DELAY = 2.0


def detect_resources() -> dict[str, Any]:
    resources: dict[str, Any] = {
        "cpu_count": os.cpu_count() or 1,
        "machine": platform.machine(),
        "system": platform.system(),
    }
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    resources["mem_total_mb"] = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    resources["gpus"] = detect_gpus()
    return resources


def detect_gpus() -> list[dict[str, Any]]:
    """Probe NVIDIA GPUs via nvidia-smi (no driver bindings needed)."""
    if shutil.which("nvidia-smi") is None:
        return []
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[1].isdigit():
            gpus.append({"name": parts[0], "mem_mb": int(parts[1])})
    return gpus


class _RelaySession:
    """Server side of one NATed peer's persistent relay connection."""

    def __init__(self, node_id: str, writer: asyncio.StreamWriter):
        self.node_id = node_id
        self.writer = writer
        self.write_lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future] = {}
        self._counter = 0

    async def forward(self, inner: dict[str, Any], timeout: float) -> dict[str, Any]:
        self._counter += 1
        msg_id = f"{self._counter}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[msg_id] = future
        try:
            async with self.write_lock:
                await send_message(self.writer, {"id": msg_id, "inner": inner})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending.pop(msg_id, None)

    def fail_all(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("relay session closed"))


class PeerNode:
    def __init__(
        self,
        identity: Identity,
        host: str = "0.0.0.0",
        port: int = 0,
        bootstrap: list[tuple[str, int]] | None = None,
        advertise_host: str | None = None,
        ledger_path: str = ":memory:",
        reputation_path: str = ":memory:",
        difficulty: int = POW_DIFFICULTY_BITS,
        # provider options
        provide: bool = False,
        price: float = 1.0,
        max_workers: int | None = None,
        task_timeout: float = 300.0,
        sandbox: bool = True,
        sandbox_mem_mb: int = 512,
        ai_backend: str | AIBackend | None = "mock",
        force_relay: bool = False,
    ):
        self.identity = identity
        self.host = host
        self.port = port
        self.bootstrap_peers = bootstrap or []
        self.advertise_host = advertise_host
        self.difficulty = difficulty
        self.ledger = GossipLedger(ledger_path, difficulty=difficulty)
        self.reputation = ReputationStore(reputation_path)
        self.dht = DHTNode(identity, host=host, port=port, difficulty=difficulty)

        self.provide = provide
        self.price = price
        self.max_workers = max_workers or (os.cpu_count() or 1)
        self.task_timeout = task_timeout
        self.sandbox = sandbox
        self.sandbox_mem_mb = sandbox_mem_mb
        self.force_relay = force_relay
        self.resources = detect_resources()
        if isinstance(ai_backend, str):
            ai_backend = load_backend(ai_backend)
        self._task_context: dict[str, Any] = {"ai_backend": ai_backend}
        self._work_semaphore = asyncio.Semaphore(self.max_workers)

        self._server: asyncio.Server | None = None
        self._loops: list[asyncio.Task] = []
        self._gossip_cursors: dict[tuple[str, int], int] = {}
        self._relay_sessions: dict[str, _RelaySession] = {}
        self._relay_endpoint: tuple[str, int] | None = None  # our relay, if NATed
        self._running = False
        self._closed = False

    # ------------------------------------------------------------------ basics

    @property
    def supported_tasks(self) -> list[str]:
        names = list(TASKS)
        if self._task_context.get("ai_backend") is None:
            names = [n for n in names if n not in BACKEND_REQUIRED]
        return names

    async def start(self) -> None:
        await self._bind()
        await self.dht.bootstrap(self.bootstrap_peers)
        await self.sync_ledger()
        self._running = True
        self._loops.append(asyncio.create_task(self._gossip_loop()))
        if self.provide:
            if self.force_relay or self._behind_nat():
                self._relay_endpoint = await self._pick_relay()
                if self._relay_endpoint:
                    self._loops.append(asyncio.create_task(self._relay_client_loop()))
            await self._announce_once()
            self._loops.append(asyncio.create_task(self._announce_loop()))
        log.info("peer %s up on port %s (provide=%s, relay=%s)",
                 self.identity.short_id, self.port, self.provide,
                 self._relay_endpoint is not None)

    async def _bind(self) -> None:
        """Bind TCP and UDP to the same port number."""
        for attempt in range(10):
            server = await asyncio.start_server(self._handle_client, self.host, self.port)
            port = server.sockets[0].getsockname()[1]
            try:
                self.dht.host, self.dht.port = self.host, port
                await self.dht.start()
                self._server = server
                self.port = port
                return
            except OSError:
                server.close()
                await server.wait_closed()
                if self.port != 0:
                    raise
        raise RuntimeError("could not bind matching TCP/UDP ports")

    async def stop(self) -> None:
        self._running = False
        self._closed = True
        for loop_task in self._loops:
            loop_task.cancel()
        for loop_task in self._loops:
            try:
                await loop_task
            except (asyncio.CancelledError, Exception):
                pass
        self._loops.clear()
        for session in self._relay_sessions.values():
            session.fail_all()
        self.dht.stop()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self.ledger.close()
        self.reputation.close()

    async def serve_forever(self) -> None:
        assert self._server is not None, "call start() first"
        async with self._server:
            await self._server.serve_forever()

    def _behind_nat(self) -> bool:
        observed = self.dht.observed_endpoint
        return observed is not None and observed[1] != self.port

    # -------------------------------------------------------------- TCP server

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            message = await asyncio.wait_for(read_message(reader), timeout=60.0)
        except (ProtocolError, asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, ValueError):
            writer.close()
            return
        if message.get("type") == "relay.register":
            await self._serve_relay_session(message, reader, writer)
            return
        try:
            response = await self._dispatch(message)
        except Exception:
            log.exception("unhandled error in peer request")
            response = error("internal_error")
        try:
            await send_message(writer, response)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            return error("shutting_down")
        msg_type = message.get("type")
        if msg_type == "node.info":
            return ok(
                node_id=self.identity.node_id,
                provide=self.provide,
                price=self.price,
                tasks=self.supported_tasks if self.provide else [],
                resources=self.resources,
                ledger_txs=self.ledger.tx_count(),
                dht_contacts=len(self.dht.table),
                relayed=self._relay_endpoint is not None,
            )
        if msg_type == "ledger.pull":
            cursor = int(message.get("cursor", 0))
            txs, new_cursor = self.ledger.txs_after(cursor)
            return ok(txs=txs, cursor=new_cursor)
        if msg_type == "ledger.push":
            return self._on_ledger_push(message)
        if msg_type == "relay.forward":
            return await self._on_relay_forward(message)
        if msg_type == "task.execute":
            if not self.provide:
                return error("not_a_provider")
            return await self._on_task_execute(message)
        return error("unknown_type", f"unknown message type: {message.get('type')!r}")

    # ------------------------------------------------------------------ ledger

    def _on_ledger_push(self, message: dict[str, Any]) -> dict[str, Any]:
        envelopes = message.get("txs")
        if not isinstance(envelopes, list) or len(envelopes) > 500:
            return error("bad_request")
        accepted = 0
        for envelope in envelopes:
            if not isinstance(envelope, dict):
                continue
            status = self.ledger.add_tx(envelope)
            if status in ("new", "conflict"):
                accepted += 1
                self.gossip_tx(envelope)
            if status == "conflict":
                payload = open_envelope(envelope) or {}
                self.reputation.record(payload.get("from", ""), "double_spend")
                log.warning("double-spend evidence recorded against %s",
                            payload.get("from", "")[:12])
        return ok(accepted=accepted)

    def gossip_tx(self, envelope: dict[str, Any]) -> None:
        """Fire-and-forget push of a transaction to a few random peers."""
        contacts = self.dht.table.all_contacts()
        random.shuffle(contacts)
        for contact in contacts[:GOSSIP_FANOUT]:
            asyncio.ensure_future(self._push_to(contact.host, contact.port, [envelope]))

    async def _push_to(self, host: str, port: int, envelopes: list[dict[str, Any]]) -> None:
        try:
            await request(host, port, {"type": "ledger.push", "txs": envelopes}, timeout=10.0)
        except NETWORK_ERRORS:
            pass

    async def sync_ledger(self) -> int:
        """Anti-entropy pull from every known peer until caught up."""
        peers = list(self.bootstrap_peers)
        peers += [(c.host, c.port) for c in self.dht.table.all_contacts()]
        pulled = 0
        for peer in dict.fromkeys(peers):
            pulled += await self._pull_from(peer)
        return pulled

    async def _pull_from(self, peer: tuple[str, int]) -> int:
        pulled = 0
        cursor = self._gossip_cursors.get(peer, 0)
        for _ in range(20):  # at most 10k txs per round
            try:
                response = await request(peer[0], peer[1],
                                         {"type": "ledger.pull", "cursor": cursor}, timeout=10.0)
            except NETWORK_ERRORS:
                return pulled
            if not response.get("ok"):
                return pulled
            txs = response.get("txs", [])
            for envelope in txs:
                if isinstance(envelope, dict):
                    status = self.ledger.add_tx(envelope)
                    if status in ("new", "conflict"):
                        pulled += 1
            cursor = int(response.get("cursor", cursor))
            self._gossip_cursors[peer] = cursor
            if not txs:
                break
        return pulled

    async def _gossip_loop(self) -> None:
        while self._running:
            await asyncio.sleep(GOSSIP_INTERVAL)
            contacts = self.dht.table.all_contacts()
            if contacts:
                peer = random.choice(contacts)
                await self._pull_from((peer.host, peer.port))

    # ---------------------------------------------------------------- provider

    def _advertised_host(self) -> str:
        if self.advertise_host:
            return self.advertise_host
        observed = self.dht.observed_endpoint
        if observed is not None:
            return observed[0]
        return "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host

    def _build_record(self) -> dict[str, Any]:
        relay = None
        if self._relay_endpoint is not None:
            relay = {"host": self._relay_endpoint[0], "port": self._relay_endpoint[1]}
        return make_provider_record(
            self.identity,
            host=self._advertised_host(),
            port=self.port,
            price=self.price,
            tasks=self.supported_tasks,
            resources=self.resources,
            relay=relay,
        )

    async def _announce_once(self) -> None:
        try:
            await announce_provider(self.dht, self._build_record())
        except Exception:
            log.exception("provider announce failed")

    async def _announce_loop(self) -> None:
        while self._running:
            await asyncio.sleep(ANNOUNCE_INTERVAL)
            await self._announce_once()

    async def _on_task_execute(self, message: dict[str, Any]) -> dict[str, Any]:
        task = str(message.get("task", ""))
        payment = message.get("payment")
        if task not in self.supported_tasks:
            return error("unsupported_task", f"task {task!r} not offered by this provider")

        # End-to-end encrypted payload: the box key is derived from the
        # payment's signing key, which _validate_payment later proves is
        # bound to the paying identity. Relays only ever see ciphertext.
        box_key: bytes | None = None
        if "enc" in message:
            if not isinstance(payment, dict) or not isinstance(payment.get("pubkey"), str):
                return error("bad_request", "encrypted requests need a payment envelope")
            try:
                box_key = derive_box_key(self.identity.key.seed,
                                         bytes.fromhex(payment["pubkey"]))
                inner = json.loads(open_sealed(box_key, message["enc"]))
                items = inner.get("items")
                params = dict(inner.get("params") or {})
            except (E2EError, ValueError, json.JSONDecodeError) as exc:
                return error("decrypt_failed", str(exc))
        else:
            items = message.get("items")
            params = dict(message.get("params") or {})
        if not isinstance(items, list) or not items:
            return error("bad_request", "items must be a non-empty list")

        expected = round(self.price * len(items), 6)
        sender, problem = self._validate_payment(payment, expected)
        if problem is not None:
            return error("payment_rejected", problem)

        async with self._work_semaphore:
            try:
                results = await self._execute_chunk(task, items, params)
            except (TaskError, SandboxError) as exc:
                return error("task_failed", str(exc))
            except asyncio.TimeoutError:
                return error("task_timeout", f"chunk exceeded {self.task_timeout}s")

        # Payment before result: apply the signed transfer to our replica and
        # gossip it. Only then does the consumer get the result.
        status = self.ledger.add_tx(payment)
        if status == "invalid":
            return error("payment_rejected", "transfer failed validation")
        if status == "conflict":
            self.reputation.record(sender, "double_spend")
            self.gossip_tx(payment)  # spread the evidence
            return error("payment_rejected", "double-spend detected; account flagged")
        self.gossip_tx(payment)
        self.reputation.record(sender, "chunk_ok")
        if box_key is not None:
            return ok(enc=seal(box_key, canonical({"results": results})), charged=expected)
        return ok(results=results, charged=expected)

    def _validate_payment(self, envelope: Any, expected: float) -> tuple[str, str | None]:
        if not isinstance(envelope, dict):
            return "", "missing payment"
        payload = open_envelope(envelope)
        if payload is None or payload.get("kind") != "tx":
            return "", "bad payment signature"
        sender = str(payload.get("from", ""))
        if not verify_node_id(sender, envelope.get("pubkey", ""),
                              payload.get("pow_nonce", -1), self.difficulty):
            return sender, "payment sender lacks valid identity proof-of-work"
        if payload.get("to") != self.identity.node_id:
            return sender, "payment is not addressed to this provider"
        amount = payload.get("amount")
        if not isinstance(amount, (int, float)) or amount + 1e-9 < expected:
            return sender, f"payment {amount} below price {expected}"
        if self.ledger.is_flagged(sender):
            return sender, "sender account is flagged (double-spend or overdrawn)"
        if self.reputation.is_banned(sender):
            return sender, "sender is banned by this provider"
        if self.ledger.balance(sender) < amount:
            return sender, "insufficient balance on our ledger replica"
        return sender, None

    async def _execute_chunk(self, task: str, items: list[Any],
                             params: dict[str, Any]) -> list[Any]:
        if self.sandbox and task not in UNSANDBOXED_TASKS:
            return await run_sandboxed(task, items, params,
                                       timeout=self.task_timeout,
                                       mem_mb=self.sandbox_mem_mb)
        return await asyncio.wait_for(
            asyncio.to_thread(run_task, task, items, params, self._task_context),
            timeout=self.task_timeout,
        )

    # ------------------------------------------------------------------- relay

    async def _pick_relay(self) -> tuple[str, int] | None:
        """A NATed provider relays through any reachable public peer."""
        for peer in self.bootstrap_peers:
            try:
                response = await request(peer[0], peer[1], {"type": "node.info"}, timeout=5.0)
                if response.get("ok"):
                    return peer
            except NETWORK_ERRORS:
                continue
        log.warning("NAT detected but no reachable relay peer found")
        return None

    async def _serve_relay_session(self, message: dict[str, Any],
                                   reader: asyncio.StreamReader,
                                   writer: asyncio.StreamWriter) -> None:
        payload = open_envelope(message.get("envelope") or {})
        now = time.time()
        if (
            payload is None
            or payload.get("kind") != "relay"
            or not verify_node_id(payload.get("node_id", ""),
                                  message["envelope"].get("pubkey", ""),
                                  payload.get("pow_nonce", -1), self.difficulty)
            or not isinstance(payload.get("ts"), (int, float))
            or abs(now - payload["ts"]) > 60
        ):
            try:
                await send_message(writer, error("relay_auth_failed"))
            except (ConnectionError, OSError):
                pass
            writer.close()
            return
        node_id = payload["node_id"]
        session = _RelaySession(node_id, writer)
        self._relay_sessions[node_id] = session
        log.info("relaying for NATed peer %s", node_id[:12])
        try:
            await send_message(writer, ok())
            while True:
                reply = await read_message(reader)
                future = session.pending.get(str(reply.get("id")))
                if future is not None and not future.done():
                    future.set_result(reply.get("response") or error("empty_relay_reply"))
        except (ProtocolError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            if self._relay_sessions.get(node_id) is session:
                del self._relay_sessions[node_id]
            session.fail_all()
            writer.close()

    async def _on_relay_forward(self, message: dict[str, Any]) -> dict[str, Any]:
        session = self._relay_sessions.get(str(message.get("to", "")))
        inner = message.get("inner")
        if session is None:
            return error("no_relay_session", "target peer is not relaying through us")
        if not isinstance(inner, dict):
            return error("bad_request")
        try:
            response = await session.forward(inner, timeout=RELAY_FORWARD_TIMEOUT)
        except (ConnectionError, asyncio.TimeoutError) as exc:
            return error("relay_failed", str(exc))
        return ok(response=response)

    async def _relay_client_loop(self) -> None:
        """Provider side: hold a persistent connection to our relay peer."""
        assert self._relay_endpoint is not None
        host, port = self._relay_endpoint
        while self._running:
            try:
                reader, writer = await asyncio.open_connection(host, port)
                envelope = sign_envelope(self.identity.key, {
                    "kind": "relay",
                    "node_id": self.identity.node_id,
                    "pow_nonce": self.identity.pow_nonce,
                    "ts": round(time.time(), 3),
                })
                await send_message(writer, {"type": "relay.register", "envelope": envelope})
                response = await read_message(reader)
                if not response.get("ok"):
                    raise ConnectionError(f"relay refused us: {response}")
                write_lock = asyncio.Lock()
                while True:
                    incoming = await read_message(reader)
                    asyncio.ensure_future(
                        self._answer_relayed(incoming, writer, write_lock)
                    )
            except asyncio.CancelledError:
                raise
            except (ProtocolError, asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                log.warning("relay connection lost (%s), reconnecting", exc)
                await asyncio.sleep(RELAY_RECONNECT_DELAY)

    async def _answer_relayed(self, incoming: dict[str, Any],
                              writer: asyncio.StreamWriter, lock: asyncio.Lock) -> None:
        inner = incoming.get("inner")
        if not isinstance(inner, dict):
            return
        try:
            response = await self._dispatch(inner)
        except Exception:
            log.exception("unhandled error in relayed request")
            response = error("internal_error")
        try:
            async with lock:
                await send_message(writer, {"id": incoming.get("id"), "response": response})
        except (ConnectionError, OSError):
            pass
