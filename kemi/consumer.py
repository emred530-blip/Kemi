"""Consumer: rents compute from the decentralised swarm.

The consumer is itself a peer: it discovers providers through the DHT,
keeps its own ledger replica and pays per chunk with Ed25519-signed credit
transfers handed directly to providers (no escrow service, no tracker).

Job execution is BitTorrent-style: items are split into chunks, scheduled
across providers concurrently, failed chunks are retried elsewhere, and
with ``redundancy > 1`` each chunk runs on several distinct providers whose
results are cross-checked - majority wins, and losers take a reputation
hit locally.

Exposure model: a payment travels with the chunk request, so a malicious
provider can keep at most one chunk's price without delivering. Small
chunks bound the loss exactly the way small pieces bound it in BitTorrent;
the reputation store stops repeat offenders.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .discovery import find_providers
from .node import PeerNode
from .protocol import NETWORK_ERRORS, request

log = logging.getLogger("kemi.consumer")

MAX_PROVIDER_STRIKES = 3
MAX_ATTEMPTS_PER_PROVIDER = 2
MAX_EXTRA_ROUNDS = 2


class JobError(Exception):
    pass


@dataclass
class Job:
    task: str
    items: list[Any]
    params: dict[str, Any] = field(default_factory=dict)
    chunk_size: int = 8
    redundancy: int = 1
    chunk_timeout: float = 120.0


@dataclass
class JobReport:
    results: list[Any]
    chunks: int
    spent: float                    # credits paid for delivered chunks
    exposure: float                 # credits signed away to providers that failed
    providers_used: dict[str, int]


def _result_fingerprint(results: list[Any]) -> str:
    canonical = json.dumps(results, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class _ChunkState:
    index: int
    items: list[Any]
    needed: int
    results_by_provider: dict[str, list[Any]] = field(default_factory=dict)
    in_flight: set[str] = field(default_factory=set)
    attempts: dict[str, int] = field(default_factory=dict)
    accepted: list[Any] | None = None

    @property
    def done(self) -> bool:
        return self.accepted is not None

    def fingerprints(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for provider_id, results in self.results_by_provider.items():
            groups.setdefault(_result_fingerprint(results), []).append(provider_id)
        return groups


class _Scheduler:
    """Assigns chunks to providers, enforcing distinct providers per chunk
    when redundancy is requested and cross-checking results."""

    def __init__(self, chunks: list[_ChunkState], redundancy: int):
        self.chunks = chunks
        self.redundancy = redundancy

    @property
    def done(self) -> bool:
        return all(chunk.done for chunk in self.chunks)

    def acquire(self, provider_id: str) -> _ChunkState | None:
        for chunk in self.chunks:
            if chunk.done or provider_id in chunk.in_flight:
                continue
            if provider_id in chunk.results_by_provider:
                continue  # distinct providers per redundant execution
            if chunk.attempts.get(provider_id, 0) >= MAX_ATTEMPTS_PER_PROVIDER:
                continue
            if len(chunk.results_by_provider) + len(chunk.in_flight) >= chunk.needed:
                continue
            chunk.in_flight.add(provider_id)
            chunk.attempts[provider_id] = chunk.attempts.get(provider_id, 0) + 1
            return chunk
        return None

    def complete(self, chunk: _ChunkState, provider_id: str,
                 results: list[Any]) -> list[str]:
        """Record a result; returns provider ids whose results lost the vote."""
        chunk.in_flight.discard(provider_id)
        chunk.results_by_provider[provider_id] = results
        groups = chunk.fingerprints()
        majority_needed = self.redundancy // 2 + 1
        for fingerprint, providers in groups.items():
            if len(providers) >= majority_needed:
                chunk.accepted = chunk.results_by_provider[providers[0]]
                losers = [p for fp, ps in groups.items() if fp != fingerprint for p in ps]
                if losers:
                    log.warning("chunk %d: result mismatch, majority won over %s",
                                chunk.index, [p[:12] for p in losers])
                return losers
        if len(chunk.results_by_provider) >= chunk.needed:
            # All requested executions arrived but no majority: widen the vote.
            if chunk.needed < self.redundancy + MAX_EXTRA_ROUNDS:
                chunk.needed += 1
                log.warning("chunk %d: no majority yet, requesting tie-break execution",
                            chunk.index)
        return []

    def fail(self, chunk: _ChunkState, provider_id: str) -> None:
        chunk.in_flight.discard(provider_id)


async def send_to_provider(record: dict[str, Any], message: dict[str, Any],
                           timeout: float) -> dict[str, Any]:
    """Reach a provider directly, or through its relay if it sits behind NAT."""
    relay = record.get("relay")
    if relay:
        wrapped = await request(
            relay["host"], relay["port"],
            {"type": "relay.forward", "to": record["node_id"], "inner": message},
            timeout=timeout,
        )
        if not wrapped.get("ok"):
            return wrapped
        return wrapped.get("response") or {"ok": False, "error": "empty_relay_reply"}
    return await request(record["host"], record["port"], message, timeout=timeout)


class Consumer:
    """A light peer (no provider service) that submits jobs to the swarm."""

    def __init__(self, node: PeerNode):
        self.node = node

    async def balance(self) -> float:
        await self.node.sync_ledger()
        return self.node.ledger.balance(self.node.identity.node_id)

    async def list_providers(self, task: str) -> list[dict[str, Any]]:
        providers = await find_providers(self.node.dht, task)
        usable = []
        for p in providers:
            if p["node_id"] == self.node.identity.node_id:
                continue
            if self.node.reputation.is_banned(p["node_id"]):
                continue
            if self.node.ledger.is_flagged(p["node_id"]):
                continue
            usable.append(p)
        # Best reputation first, then cheapest.
        usable.sort(key=lambda p: (-self.node.reputation.score(p["node_id"]), p["price"]))
        return usable

    async def run_job(self, job: Job) -> JobReport:
        if not job.items:
            return JobReport(results=[], chunks=0, spent=0.0, exposure=0.0, providers_used={})
        if job.redundancy < 1:
            raise JobError("redundancy must be >= 1")

        providers = await self.list_providers(job.task)
        if not providers:
            raise JobError(f"no usable providers offer task {job.task!r}")
        if job.redundancy > len(providers):
            raise JobError(
                f"redundancy {job.redundancy} needs at least that many providers "
                f"(found {len(providers)})"
            )

        ledger = self.node.ledger
        my_id = self.node.identity.node_id
        min_cost = min(p["price"] for p in providers) * len(job.items) * job.redundancy
        if ledger.balance(my_id) < min_cost:
            raise JobError(
                f"insufficient credits: balance {ledger.balance(my_id):.2f}, "
                f"job needs at least {min_cost:.2f}"
            )

        chunk_size = max(1, job.chunk_size)
        chunks = [
            _ChunkState(index=i, items=job.items[start:start + chunk_size],
                        needed=job.redundancy)
            for i, start in enumerate(range(0, len(job.items), chunk_size))
        ]
        scheduler = _Scheduler(chunks, job.redundancy)
        providers_used: dict[str, int] = {}
        spent = 0.0
        exposure = 0.0

        async def provider_worker(record: dict[str, Any]) -> None:
            nonlocal spent, exposure
            provider_id = record["node_id"]
            strikes = 0
            while not scheduler.done and strikes < MAX_PROVIDER_STRIKES:
                chunk = scheduler.acquire(provider_id)
                if chunk is None:
                    await asyncio.sleep(0.05)
                    continue
                amount = round(record["price"] * len(chunk.items), 6)
                if ledger.balance(my_id) - spent_pending() < amount:
                    scheduler.fail(chunk, provider_id)
                    strikes = MAX_PROVIDER_STRIKES  # out of credits for this one
                    log.warning("skipping provider %s: not enough credits left",
                                provider_id[:12])
                    continue
                # Sign the per-chunk payment. The seq is burned even if the
                # provider fails - see GossipLedger.reserve_seq.
                tx = ledger.make_tx(self.node.identity, provider_id, amount)
                try:
                    response = await send_to_provider(
                        record,
                        {"type": "task.execute", "task": job.task,
                         "items": chunk.items, "params": job.params, "payment": tx},
                        timeout=job.chunk_timeout,
                    )
                except NETWORK_ERRORS as exc:
                    scheduler.fail(chunk, provider_id)
                    strikes += 1
                    self.node.reputation.record(provider_id, "chunk_fail")
                    log.warning("provider %s unreachable for chunk %d: %s",
                                provider_id[:12], chunk.index, exc)
                    continue
                if not response.get("ok"):
                    scheduler.fail(chunk, provider_id)
                    strikes += 1
                    exposure += amount
                    self.node.reputation.record(provider_id, "chunk_fail")
                    log.warning("provider %s failed chunk %d: %s: %s",
                                provider_id[:12], chunk.index,
                                response.get("error"), response.get("detail", ""))
                    continue
                results = response.get("results")
                if not isinstance(results, list) or len(results) != len(chunk.items):
                    scheduler.fail(chunk, provider_id)
                    strikes += 1
                    exposure += amount
                    self.node.reputation.record(provider_id, "chunk_fail")
                    continue
                strikes = 0
                # The transfer is now in force: apply it to our replica and
                # gossip it so the provider's earnings become visible swarm-wide.
                ledger.add_tx(tx)
                self.node.gossip_tx(tx)
                spent += amount
                providers_used[provider_id] = providers_used.get(provider_id, 0) + 1
                losers = scheduler.complete(chunk, provider_id, results)
                for loser in losers:
                    self.node.reputation.record(loser, "mismatch")
                if provider_id not in losers:
                    self.node.reputation.record(provider_id, "chunk_ok")

        def spent_pending() -> float:
            # make_tx applies nothing locally until success; reserve a margin
            # for chunks currently in flight.
            in_flight = sum(len(c.in_flight) for c in chunks)
            max_price = max(p["price"] for p in providers)
            return in_flight * max_price * chunk_size

        await asyncio.gather(*(provider_worker(p) for p in providers))

        incomplete = [c.index for c in chunks if not c.done]
        if incomplete:
            raise JobError(
                f"job failed: chunks {incomplete} could not be completed/verified "
                f"(spent {spent:.2f} credits, exposure {exposure:.2f})"
            )

        results: list[Any] = []
        for chunk in chunks:
            assert chunk.accepted is not None
            results.extend(chunk.accepted)
        return JobReport(
            results=results,
            chunks=len(chunks),
            spent=round(spent, 6),
            exposure=round(exposure, 6),
            providers_used=providers_used,
        )
