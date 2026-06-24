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
import hashlib
import json
import logging
import os
import platform
import random
import shutil
import subprocess
import time
from collections import OrderedDict
from typing import Any

from .ai_backends import (AIBackend, AIBackendError, backend_model, load_backend,
                          supports_embedding, supports_streaming)
from .crypto import canonical, digest, open_envelope, sign_envelope
from .dht import DHTNode
from .e2e import E2EError, derive_box_key, open_sealed, seal
from .discovery import ANNOUNCE_INTERVAL, announce_provider, make_provider_record
from .gossip_ledger import GossipLedger
from .identity import POW_DIFFICULTY_BITS, Identity, verify_node_id
from .protocol import (NETWORK_ERRORS, ProtocolError, error, ok, read_message,
                       request, send_message)
from .ratelimit import IPGuard, Limits
from .reputation import ReputationStore
from .sandbox import UNSANDBOXED_TASKS, SandboxError, run_sandboxed
from .tasks import BACKEND_REQUIRED, TASKS, TaskError, run_task

log = logging.getLogger("kemi.node")

GOSSIP_FANOUT = 3
GOSSIP_INTERVAL = 3.0
RELAY_FORWARD_TIMEOUT = 150.0
RELAY_RECONNECT_DELAY = 2.0

# Tasks whose output may legitimately differ between runs must not be cached.
NON_CACHEABLE = frozenset({"ai.generate"})

# How often (seconds) transient reputation evidence decays toward neutral.
REP_DECAY_INTERVAL = 3600.0

# Witness committee: before accepting a payment, a provider asks the nodes
# DHT-closest to sha256("kemi:witness:" + sender) to lock and co-sign the
# transfer. Two transfers racing the same seq hit the SAME committee, so at
# most one wins - double-spending is prevented, not merely detected later.
WITNESS_COUNT = 5          # committee members consulted
WITNESS_QUORUM = 2         # receipts required (adaptive: capped by reachable)
WITNESS_TIMEOUT = 3.0
WITNESS_CACHE_TTL = 30.0


def witness_key(sender: str) -> str:
    return hashlib.sha256(f"kemi:witness:{sender}".encode("utf-8")).hexdigest()


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
        self.streams: dict[str, asyncio.Queue] = {}
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"{self._counter}"

    async def forward(self, inner: dict[str, Any], timeout: float) -> dict[str, Any]:
        msg_id = self._next_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[msg_id] = future
        try:
            async with self.write_lock:
                await send_message(self.writer, {"id": msg_id, "inner": inner})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending.pop(msg_id, None)

    async def stream(self, inner: dict[str, Any], timeout: float):
        """Multiplexed streaming over the persistent connection: yields the
        NATed peer's event messages for this request until one ends it."""
        msg_id = self._next_id()
        queue: asyncio.Queue = asyncio.Queue()
        self.streams[msg_id] = queue
        try:
            async with self.write_lock:
                await send_message(self.writer,
                                   {"id": msg_id, "inner": inner, "stream": True})
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
                if event is None:
                    raise ConnectionError("relay session closed mid-stream")
                yield event
                if event.get("end"):
                    return
        finally:
            self.streams.pop(msg_id, None)

    def route_reply(self, reply: dict[str, Any]) -> None:
        msg_id = str(reply.get("id"))
        payload = reply.get("response")
        if not isinstance(payload, dict):
            payload = {"ok": False, "error": "empty_relay_reply", "end": True}
        queue = self.streams.get(msg_id)
        if queue is not None:
            queue.put_nowait(payload)
            return
        future = self.pending.get(msg_id)
        if future is not None and not future.done():
            future.set_result(payload)

    def fail_all(self) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("relay session closed"))
        for queue in self.streams.values():
            queue.put_nowait(None)


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
        witness_enabled: bool = True,
        lan: bool = False,
        limits: Limits | None = None,
        prune_above: int | None = None,
        dynamic_price: bool = False,
    ):
        self.identity = identity
        self.host = host
        self.port = port
        self.bootstrap_peers = bootstrap or []
        self.advertise_host = advertise_host
        self.difficulty = difficulty
        self.limits = limits or Limits()
        self.prune_above = prune_above
        self._tcp_guard = IPGuard(self.limits.per_ip_rate, self.limits.per_ip_burst,
                                  self.limits.per_ip_connections)
        self._udp_guard = IPGuard(self.limits.udp_rate, self.limits.udp_burst,
                                  max_concurrent=1_000_000)
        self._open_connections = 0
        self.ledger = GossipLedger(ledger_path, difficulty=difficulty)
        self.reputation = ReputationStore(reputation_path)
        self.dht = DHTNode(identity, host=host, port=port, difficulty=difficulty,
                           guard=self._udp_guard, max_keys=self.limits.dht_max_keys)

        self.provide = provide
        self.price = price
        self.dynamic_price = dynamic_price
        self._active_chunks = 0
        self.max_workers = max_workers or (os.cpu_count() or 1)
        self.task_timeout = task_timeout
        self.sandbox = sandbox
        self.sandbox_mem_mb = sandbox_mem_mb
        self.force_relay = force_relay
        self.lan = lan
        self._lan_beacon = None
        self.witness_enabled = witness_enabled
        self._witness_cache: dict[str, tuple[float, list[Any]]] = {}
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
        self._last_rep_decay = time.monotonic()
        # -- operational metrics (T13) and a content-addressed result cache (T4)
        self.metrics: dict[str, float] = {
            "chunks_served": 0, "chunks_failed": 0, "credits_earned": 0.0,
            "cache_hits": 0, "cache_misses": 0, "payments_rejected": 0,
            "bytes_in": 0, "bytes_out": 0,
        }
        self._result_cache: "OrderedDict[str, list]" = OrderedDict()
        self._result_cache_max = 512

    # ------------------------------------------------------------------ basics

    def effective_price(self) -> float:
        """Advertised price. With dynamic pricing on, it surges with load
        (up to 2x at full occupancy) so the market sheds work to idle ships;
        the base price stays the floor the provider will accept."""
        if not self.dynamic_price or self.max_workers <= 0:
            return self.price
        load = min(1.0, self._active_chunks / self.max_workers)
        return round(self.price * (1.0 + load), 6)

    @property
    def supported_tasks(self) -> list[str]:
        names = list(TASKS)
        if self._task_context.get("ai_backend") is None:
            names = [n for n in names if n not in BACKEND_REQUIRED]
        return names

    async def start(self) -> None:
        await self._bind()
        if self.lan:
            from . import lan as _lan

            for peer in await _lan.discover(self.identity.node_id, timeout=1.2):
                if peer not in self.bootstrap_peers:
                    self.bootstrap_peers.append(peer)
            self._lan_beacon = _lan.beacon_payload_of(self)
            await self._lan_beacon.start()
        await self.dht.bootstrap(self.bootstrap_peers)
        if (self.bootstrap_peers and self.ledger.tx_count() == 0
                and not self.ledger.has_base()):
            await self._bootstrap_from_snapshot()
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
        if self._lan_beacon is not None:
            await self._lan_beacon.stop()
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
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = str(peer[0])
        if self._open_connections >= self.limits.max_connections \
                or not self._tcp_guard.try_connect(ip):
            writer.close()  # over capacity: shed load without ceremony
            return
        self._open_connections += 1
        try:
            await self._guarded_client(ip, reader, writer)
        finally:
            self._open_connections -= 1
            self._tcp_guard.disconnect(ip)

    async def _guarded_client(self, ip: str, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        if not self._tcp_guard.allow_request(ip):
            try:
                await send_message(writer, error("rate_limited",
                                                 "slow down, sailor"))
            except (ConnectionError, OSError):
                pass
            writer.close()
            return
        try:
            message = await asyncio.wait_for(read_message(reader), timeout=60.0)
        except (ProtocolError, asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, ValueError):
            writer.close()
            return
        if message.get("type") == "relay.register":
            await self._serve_relay_session(message, reader, writer)
            return
        if message.get("type") == "task.stream":
            await self._serve_task_stream(message, writer)
            return
        if message.get("type") == "relay.stream":
            await self._serve_relay_stream(message, writer)
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
                price=self.effective_price(),
                tasks=self.supported_tasks if self.provide else [],
                resources=self.resources,
                model=backend_model(self._task_context.get("ai_backend")),
                ledger_txs=self.ledger.tx_count(),
                dht_contacts=len(self.dht.table),
                relayed=self._relay_endpoint is not None,
                metrics=dict(self.metrics),
            )
        if msg_type == "ledger.pull":
            try:
                cursor = max(0, int(message.get("cursor", 0)))
            except (TypeError, ValueError):
                return error("bad_request", "cursor must be an integer")
            txs, new_cursor = self.ledger.txs_after(cursor)
            return ok(txs=txs, cursor=new_cursor)
        if msg_type == "ledger.push":
            return self._on_ledger_push(message)
        if msg_type == "ledger.snapshot":
            snap = self.ledger.snapshot()
            return ok(snapshot=snap, hash=digest(snap))
        if msg_type == "tx.witness":
            return self._on_tx_witness(message)
        if msg_type == "relay.forward":
            return await self._on_relay_forward(message)
        if msg_type == "task.execute":
            if not self.provide:
                return error("not_a_provider")
            return await self._on_task_execute(message)
        if msg_type == "task.stream":
            # reaches here only via relay; streaming needs a direct connection
            return error("no_stream_via_relay",
                         "token streaming requires a direct connection")
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

    # --------------------------------------------------------------- witnessing

    def _on_tx_witness(self, message: dict[str, Any]) -> dict[str, Any]:
        """Act as a witness: lock the first transfer seen for a (sender, seq)
        and co-sign it. A conflicting transfer gets the stored evidence back
        instead of a receipt."""
        envelope = message.get("tx")
        if not isinstance(envelope, dict):
            return error("bad_request")
        status = self.ledger.add_tx(envelope)
        if status == "invalid":
            return error("invalid_tx", "transfer failed validation")
        payload = open_envelope(envelope) or {}
        sender = str(payload.get("from", ""))
        seq = int(payload.get("seq", 0))
        if status == "conflict":
            self.reputation.record(sender, "double_spend")
            self.gossip_tx(envelope)  # spread the evidence
            tx_id = digest(payload)
            evidence = [env for env in self.ledger.envelopes_for_seq(sender, seq)
                        if digest(env.get("payload", {})) != tx_id]
            return error("conflict", "a different transfer already holds this seq",
                         ) | {"evidence": evidence[:1]}
        if status == "new":
            self.gossip_tx(envelope)
        receipt = sign_envelope(self.identity.key, {
            "kind": "receipt",
            "tx_id": digest(payload),
            "from": sender,
            "seq": seq,
            "witness": self.identity.node_id,
            "pow_nonce": self.identity.pow_nonce,
            "ts": round(time.time(), 3),
        })
        return ok(receipt=receipt)

    def _rank_witnesses(self, candidates: list) -> list:
        """Stake-weighted committee membership (T1): among candidates near the
        sender's witness key, prefer established (higher-earned) ships. An
        attacker must not only mint identities near the victim's key (PoW) but
        also out-earn real ships to capture the committee."""
        ranked = sorted(candidates,
                        key=lambda c: self.ledger.total_earned(c.node_id),
                        reverse=True)
        return ranked[:WITNESS_COUNT]

    async def _witness_check(self, payment: dict[str, Any],
                             sender: str) -> tuple[bool, str]:
        """Ask the sender's witness committee to lock this transfer.

        Adaptive quorum: in small swarms with few reachable witnesses the
        requirement shrinks (down to optimistic acceptance when alone), so
        liveness is never lost - but any reachable witness that has seen a
        conflicting transfer vetoes the payment outright.
        """
        if not self.witness_enabled:
            return True, ""
        cached = self._witness_cache.get(sender)
        if cached and cached[0] > time.monotonic():
            committee = cached[1]
        else:
            contacts = await self.dht.lookup(witness_key(sender))
            candidates = [c for c in contacts
                          if c.node_id not in (sender, self.identity.node_id)]
            committee = self._rank_witnesses(candidates)
            self._witness_cache[sender] = (time.monotonic() + WITNESS_CACHE_TTL,
                                           committee)
        if not committee:
            return True, ""  # alone in the swarm: optimistic fallback

        async def ask(contact) -> dict[str, Any] | None:
            try:
                return await request(contact.host, contact.port,
                                     {"type": "tx.witness", "tx": payment},
                                     timeout=WITNESS_TIMEOUT)
            except NETWORK_ERRORS:
                return None

        responses = await asyncio.gather(*(ask(c) for c in committee))
        tx_id = digest(open_envelope(payment) or {})
        receipts = 0
        for contact, response in zip(committee, responses):
            if response is None:
                continue
            if response.get("ok"):
                receipt = open_envelope(response.get("receipt") or {})
                if (
                    receipt is not None
                    and receipt.get("kind") == "receipt"
                    and receipt.get("tx_id") == tx_id
                    and receipt.get("witness") == contact.node_id
                    and verify_node_id(contact.node_id,
                                       response["receipt"].get("pubkey", ""),
                                       receipt.get("pow_nonce", -1), self.difficulty)
                ):
                    receipts += 1
            elif response.get("error") == "conflict":
                # Objective evidence: record it, flag the sender, refuse.
                for evidence in response.get("evidence") or []:
                    if isinstance(evidence, dict):
                        self.ledger.add_tx(evidence)
                        self.gossip_tx(evidence)
                self.reputation.record(sender, "double_spend")
                return False, "witness vetoed payment: conflicting transfer exists"
        reachable = sum(1 for r in responses if r is not None)
        required = min(WITNESS_QUORUM, reachable) if reachable else 0
        if receipts >= required:
            return True, ""
        return False, f"witness quorum not reached ({receipts}/{required})"

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
            if self.prune_above and self.ledger.tx_count() > self.prune_above:
                pruned = self.ledger.prune()
                self._gossip_cursors.clear()  # peers re-sync via snapshots
                log.info("ledger pruned: %d transactions folded into the baseline",
                         pruned)
            now = time.monotonic()
            if now - self._last_rep_decay > REP_DECAY_INTERVAL:
                self._last_rep_decay = now
                self.reputation.decay()  # transient marks fade; condemnation stays

    async def _bootstrap_from_snapshot(self) -> None:
        """Fast bootstrap: adopt a checkpoint instead of replaying history.

        Snapshots are fetched from several bootstrap peers and adopted only
        when independently fetched copies agree on the content hash; with a
        single reachable source we still adopt (there is nobody else to
        ask) but say so out loud.
        """
        responses: dict[str, tuple[dict[str, Any], int]] = {}
        for peer in self.bootstrap_peers[:3]:
            try:
                reply = await request(peer[0], peer[1],
                                      {"type": "ledger.snapshot"}, timeout=10.0)
            except NETWORK_ERRORS:
                continue
            if not reply.get("ok") or not isinstance(reply.get("snapshot"), dict):
                continue
            snap = reply["snapshot"]
            snap_hash = digest(snap)
            if snap_hash != reply.get("hash"):
                continue
            seen = responses.get(snap_hash)
            responses[snap_hash] = (snap, (seen[1] if seen else 0) + 1)
        if not responses:
            return
        snap, agreement = max(responses.values(), key=lambda pair: pair[1])
        if not snap.get("accounts"):
            return  # an empty fleet: nothing worth adopting
        if agreement < 2 and len(responses) + agreement > 2:
            log.warning("snapshot sources disagree; adopting the majority hash")
        if agreement < 2:
            log.warning("adopting a single-source ledger snapshot "
                        "(only one bootstrap peer reachable)")
        try:
            self.ledger.adopt_snapshot(snap)
            log.info("fast bootstrap: adopted snapshot of %d accounts",
                     len(snap["accounts"]))
        except ValueError as exc:
            log.warning("snapshot adoption failed: %s", exc)

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
        backend = self._task_context.get("ai_backend")
        return make_provider_record(
            self.identity,
            host=self._advertised_host(),
            port=self.port,
            price=self.effective_price(),
            tasks=self.supported_tasks,
            resources=self.resources,
            relay=relay,
            stream=supports_streaming(backend),
            model=backend_model(backend),
            embed=supports_embedding(backend),
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

    def _decode_task_payload(
        self, message: dict[str, Any]
    ) -> tuple[list[Any] | None, dict[str, Any], bytes | None, dict[str, Any] | None]:
        """Extract (items, params, box_key) from a possibly encrypted request.

        The box key is derived from the payment's signing key, which
        _validate_payment later proves is bound to the paying identity.
        Relays only ever see ciphertext.
        """
        payment = message.get("payment")
        box_key: bytes | None = None
        if "enc" in message:
            if not isinstance(payment, dict) or not isinstance(payment.get("pubkey"), str):
                return None, {}, None, error("bad_request",
                                             "encrypted requests need a payment envelope")
            try:
                box_key = derive_box_key(self.identity.key.seed,
                                         bytes.fromhex(payment["pubkey"]))
                inner = json.loads(open_sealed(box_key, message["enc"]))
                items = inner.get("items")
                params = dict(inner.get("params") or {})
            except (E2EError, ValueError, json.JSONDecodeError) as exc:
                return None, {}, None, error("decrypt_failed", str(exc))
        else:
            items = message.get("items")
            params = dict(message.get("params") or {})
        if not isinstance(items, list) or not items:
            return None, {}, box_key, error("bad_request", "items must be a non-empty list")
        return items, params, box_key, None

    async def _on_task_execute(self, message: dict[str, Any]) -> dict[str, Any]:
        task = str(message.get("task", ""))
        payment = message.get("payment")
        if task not in self.supported_tasks:
            return error("unsupported_task", f"task {task!r} not offered by this provider")
        items, params, box_key, problem = self._decode_task_payload(message)
        if problem is not None:
            return problem

        expected = round(self.price * len(items), 6)
        sender, problem = self._validate_payment(payment, expected)
        if problem is not None:
            self.metrics["payments_rejected"] += 1
            return error("payment_rejected", problem)
        witnessed, veto = await self._witness_check(payment, sender)
        if not witnessed:
            self.metrics["payments_rejected"] += 1
            return error("payment_rejected", veto)

        async with self._work_semaphore:
            self._active_chunks += 1
            try:
                results = await self._execute_chunk(task, items, params)
            except (TaskError, SandboxError) as exc:
                self.metrics["chunks_failed"] += 1
                return error("task_failed", str(exc))
            except asyncio.TimeoutError:
                self.metrics["chunks_failed"] += 1
                return error("task_timeout", f"chunk exceeded {self.task_timeout}s")
            finally:
                self._active_chunks -= 1

        # Payment before result: apply the signed transfer to our replica and
        # gossip it. Only then does the consumer get the result.
        status = self.ledger.add_tx(payment)
        if status == "invalid":
            self.metrics["payments_rejected"] += 1
            return error("payment_rejected", "transfer failed validation")
        if status == "conflict":
            self.reputation.record(sender, "double_spend")
            self.gossip_tx(payment)  # spread the evidence
            self.metrics["payments_rejected"] += 1
            return error("payment_rejected", "double-spend detected; account flagged")
        self.gossip_tx(payment)
        self.reputation.record(sender, "chunk_ok")
        self.metrics["chunks_served"] += 1
        self.metrics["credits_earned"] += expected
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
        # Content-addressed result cache (T4): identical deterministic work is
        # answered from memory. The consumer still pays — the saving is the
        # provider's CPU, which lets popular work get cheaper over time.
        cache_key = None
        if task not in NON_CACHEABLE:
            cache_key = digest({"t": task, "i": items, "p": params})
            cached = self._result_cache.get(cache_key)
            if cached is not None:
                self._result_cache.move_to_end(cache_key)
                self.metrics["cache_hits"] += 1
                return cached
            self.metrics["cache_misses"] += 1
        if self.sandbox and task not in UNSANDBOXED_TASKS:
            results = await run_sandboxed(task, items, params,
                                          timeout=self.task_timeout,
                                          mem_mb=self.sandbox_mem_mb)
        else:
            results = await asyncio.wait_for(
                asyncio.to_thread(run_task, task, items, params, self._task_context),
                timeout=self.task_timeout,
            )
        if cache_key is not None:
            self._result_cache[cache_key] = results
            while len(self._result_cache) > self._result_cache_max:
                self._result_cache.popitem(last=False)
        return results

    # --------------------------------------------------------------- streaming

    async def _serve_task_stream(self, message: dict[str, Any],
                                 writer: asyncio.StreamWriter) -> None:
        async def emit(payload: dict[str, Any]) -> None:
            await send_message(writer, payload)

        await self._stream_task(message, emit)

    async def _stream_task(self, message: dict[str, Any], emit) -> None:
        """Live token streaming for ai.generate (direct or via relay).

        Streaming reverses the usual payment-before-result order: the
        consumer's transfer is applied *before* tokens flow (otherwise it
        could disconnect after the last token and never pay). Exposure stays
        bounded by one chunk's price, the same bound as everywhere else.
        """
        async def finish(payload: dict[str, Any]) -> None:
            payload = {**payload, "end": True}
            try:
                await emit(payload)
            except (ConnectionError, OSError):
                pass

        backend = self._task_context.get("ai_backend")
        task = str(message.get("task", ""))
        if self._closed or not self.provide:
            await finish(error("not_a_provider"))
            return
        if task != "ai.generate" or task not in self.supported_tasks:
            await finish(error("unsupported_task", "streaming is only for ai.generate"))
            return
        if not supports_streaming(backend):
            await finish(error("no_stream", "this provider's backend cannot stream"))
            return
        items, params, box_key, problem = self._decode_task_payload(message)
        if problem is not None:
            await finish(problem)
            return
        expected = round(self.price * len(items), 6)
        sender, pay_problem = self._validate_payment(message.get("payment"), expected)
        if pay_problem is not None:
            await finish(error("payment_rejected", pay_problem))
            return
        witnessed, veto = await self._witness_check(message["payment"], sender)
        if not witnessed:
            await finish(error("payment_rejected", veto))
            return
        status = self.ledger.add_tx(message["payment"])
        if status == "invalid":
            await finish(error("payment_rejected", "transfer failed validation"))
            return
        if status == "conflict":
            self.reputation.record(sender, "double_spend")
            self.gossip_tx(message["payment"])
            await finish(error("payment_rejected", "double-spend detected; account flagged"))
            return
        self.gossip_tx(message["payment"])

        max_tokens = int(params.get("max_tokens", 64))
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def produce() -> None:
            try:
                results = []
                for index, prompt in enumerate(items):
                    parts: list[str] = []
                    for token in backend.stream(str(prompt), max_tokens=max_tokens):
                        parts.append(token)
                        loop.call_soon_threadsafe(queue.put_nowait, ("token", index, token))
                    results.append("".join(parts))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", results, None))
            except AIBackendError as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc), None))
            except Exception as exc:  # backend bug: report, don't kill the node
                loop.call_soon_threadsafe(
                    queue.put_nowait, ("error", f"{type(exc).__name__}: {exc}", None))

        async with self._work_semaphore:
            producer = asyncio.create_task(asyncio.to_thread(produce))
            producer.add_done_callback(lambda t: t.exception())
            deadline = loop.time() + self.task_timeout
            try:
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    kind, a, b = await asyncio.wait_for(queue.get(), timeout=remaining)
                    if kind == "token":
                        event = {"evt": "token", "item": a, "t": b}
                        await emit(
                            {"enc": seal(box_key, canonical(event))} if box_key else event)
                    elif kind == "done":
                        self.reputation.record(sender, "chunk_ok")
                        final = {"evt": "end", "ok": True, "results": a, "charged": expected}
                        if box_key:
                            await finish({"ok": True, "enc": seal(box_key, canonical(final))})
                        else:
                            await finish(final)
                        return
                    else:
                        await finish(error("task_failed", a))
                        return
            except asyncio.TimeoutError:
                await finish(error("task_timeout", f"stream exceeded {self.task_timeout}s"))
            except (ConnectionError, OSError):
                pass  # consumer went away; producer drains harmlessly

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
        ip = str((writer.get_extra_info("peername") or ("?",))[0])
        if (len(self._relay_sessions) >= self.limits.max_relay_sessions
                or sum(1 for s in self._relay_sessions.values()
                       if getattr(s, "ip", None) == ip)
                >= self.limits.per_ip_relay_sessions):
            try:
                await send_message(writer, error("relay_full",
                                                 "no relay capacity for you here"))
            except (ConnectionError, OSError):
                pass
            writer.close()
            return
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
        session.ip = ip  # for per-IP relay caps
        self._relay_sessions[node_id] = session
        log.info("relaying for NATed peer %s", node_id[:12])
        try:
            await send_message(writer, ok())
            while True:
                session.route_reply(await read_message(reader))
        except (ProtocolError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            if self._relay_sessions.get(node_id) is session:
                del self._relay_sessions[node_id]
            session.fail_all()
            writer.close()

    async def _serve_relay_stream(self, message: dict[str, Any],
                                  writer: asyncio.StreamWriter) -> None:
        """Pass a NATed provider's stream through to the consumer verbatim -
        events are end-to-end sealed, so this relay learns nothing."""
        session = self._relay_sessions.get(str(message.get("to", "")))
        inner = message.get("inner")
        try:
            if session is None or not isinstance(inner, dict):
                await send_message(writer, {**error(
                    "no_relay_session", "target peer is not relaying through us"),
                    "end": True})
                return
            async for event in session.stream(inner, timeout=RELAY_FORWARD_TIMEOUT):
                await send_message(writer, event)
        except asyncio.TimeoutError:
            try:
                await send_message(writer, {**error("relay_failed", "stream timed out"),
                                            "end": True})
            except (ConnectionError, OSError):
                pass
        except (ConnectionError, OSError):
            pass

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
        msg_id = incoming.get("id")
        if incoming.get("stream") and inner.get("type") == "task.stream":
            async def emit(payload: dict[str, Any]) -> None:
                async with lock:
                    await send_message(writer, {"id": msg_id, "response": payload})

            try:
                await self._stream_task(inner, emit)
            except Exception:
                log.exception("unhandled error in relayed stream")
            return
        try:
            response = await self._dispatch(inner)
        except Exception:
            log.exception("unhandled error in relayed request")
            response = error("internal_error")
        try:
            async with lock:
                await send_message(writer, {"id": msg_id, "response": response})
        except (ConnectionError, OSError):
            pass
