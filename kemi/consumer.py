"""Consumer client: rents compute from the swarm.

A job's items are split into chunks (BitTorrent-style pieces) and scheduled
across all eligible providers concurrently. Failed chunks are retried on
other providers; with ``redundancy > 1`` each chunk is executed by several
distinct providers and the results are cross-checked (majority wins), which
defends against faulty or dishonest nodes.

Payment: the consumer pre-funds an escrow at the tracker sized for the worst
case, hands the redeem key to providers with each chunk, and reclaims the
unspent remainder when the job finishes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from .identity import Identity
from .protocol import request

log = logging.getLogger("kemi.consumer")

# Give up on a provider after this many consecutive failures.
MAX_PROVIDER_STRIKES = 3
# A provider may retry the same chunk at most this many times after failing it.
MAX_ATTEMPTS_PER_PROVIDER = 2
# Cross-check mismatches trigger extra executions, up to this many beyond
# the requested redundancy.
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
    spent: float
    refunded: float
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

    def complete(self, chunk: _ChunkState, provider_id: str, results: list[Any]) -> None:
        chunk.in_flight.discard(provider_id)
        chunk.results_by_provider[provider_id] = results
        groups = chunk.fingerprints()
        majority_needed = self.redundancy // 2 + 1
        for fingerprint, providers in groups.items():
            if len(providers) >= majority_needed:
                chunk.accepted = chunk.results_by_provider[providers[0]]
                if len(groups) > 1:
                    losers = [p[:12] for fp, ps in groups.items() if fp != fingerprint for p in ps]
                    log.warning("chunk %d: result mismatch, majority won over %s", chunk.index, losers)
                return
        if len(chunk.results_by_provider) >= chunk.needed:
            # All requested executions arrived but no majority: widen the vote.
            if chunk.needed < self.redundancy + MAX_EXTRA_ROUNDS:
                chunk.needed += 1
                log.warning("chunk %d: no majority yet, requesting tie-break execution", chunk.index)

    def fail(self, chunk: _ChunkState, provider_id: str) -> None:
        chunk.in_flight.discard(provider_id)


class Consumer:
    def __init__(self, identity: Identity, tracker_host: str, tracker_port: int):
        self.identity = identity
        self.tracker_host = tracker_host
        self.tracker_port = tracker_port

    async def _tracker_request(self, message: dict[str, Any]) -> dict[str, Any]:
        message = {**message, "node_id": self.identity.node_id, "token": self.identity.token}
        response = await request(self.tracker_host, self.tracker_port, message)
        if not response.get("ok"):
            raise JobError(f"tracker error: {response.get('error')}: {response.get('detail', '')}")
        return response

    async def balance(self) -> float:
        return (await self._tracker_request({"type": "balance"}))["balance"]

    async def list_providers(self, task: str | None = None) -> list[dict[str, Any]]:
        response = await self._tracker_request({"type": "providers.list"})
        providers = response["providers"]
        if task is not None:
            providers = [p for p in providers if task in p.get("tasks", [])]
        return providers

    async def run_job(self, job: Job) -> JobReport:
        if not job.items:
            return JobReport(results=[], chunks=0, spent=0.0, refunded=0.0, providers_used={})
        if job.redundancy < 1:
            raise JobError("redundancy must be >= 1")

        providers = await self.list_providers(task=job.task)
        providers = [p for p in providers if p["node_id"] != self.identity.node_id] or providers
        if not providers:
            raise JobError(f"no active providers offer task {job.task!r}")
        if job.redundancy > len(providers):
            raise JobError(
                f"redundancy {job.redundancy} needs at least that many providers "
                f"(found {len(providers)})"
            )

        chunk_size = max(1, job.chunk_size)
        chunks = [
            _ChunkState(index=i, items=job.items[start : start + chunk_size], needed=job.redundancy)
            for i, start in enumerate(range(0, len(job.items), chunk_size))
        ]

        # Worst case every execution lands on the most expensive provider,
        # plus headroom for tie-break rounds - clamped to what we can afford.
        # If the clamped escrow runs dry mid-job, providers refuse further
        # chunks and the job fails with a payment error.
        max_price = max(p["price"] for p in providers)
        worst_runs = job.redundancy + MAX_EXTRA_ROUNDS
        worst_case = math.ceil(max_price * len(job.items) * worst_runs * 100) / 100
        balance = await self.balance()
        escrow_amount = min(worst_case, math.floor(balance * 100) / 100)
        if escrow_amount <= 0:
            raise JobError(f"insufficient credits: balance {balance:.2f}")
        escrow = await self._tracker_request({"type": "escrow.create", "amount": escrow_amount})
        escrow_id, redeem_key = escrow["escrow_id"], escrow["redeem_key"]

        scheduler = _Scheduler(chunks, job.redundancy)
        providers_used: dict[str, int] = {}
        spent = 0.0
        spent_lock = asyncio.Lock()

        async def provider_worker(provider: dict[str, Any]) -> None:
            nonlocal spent
            strikes = 0
            while not scheduler.done and strikes < MAX_PROVIDER_STRIKES:
                chunk = scheduler.acquire(provider["node_id"])
                if chunk is None:
                    await asyncio.sleep(0.05)
                    continue
                try:
                    response = await request(
                        provider["host"],
                        provider["port"],
                        {
                            "type": "task.execute",
                            "task": job.task,
                            "items": chunk.items,
                            "params": job.params,
                            "payment": {"escrow_id": escrow_id, "redeem_key": redeem_key},
                        },
                        timeout=job.chunk_timeout,
                    )
                except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                    scheduler.fail(chunk, provider["node_id"])
                    strikes += 1
                    log.warning("provider %s unreachable for chunk %d: %s",
                                provider["node_id"][:12], chunk.index, exc)
                    continue
                if not response.get("ok"):
                    scheduler.fail(chunk, provider["node_id"])
                    strikes += 1
                    log.warning("provider %s failed chunk %d: %s: %s",
                                provider["node_id"][:12], chunk.index,
                                response.get("error"), response.get("detail", ""))
                    continue
                strikes = 0
                results = response.get("results")
                if not isinstance(results, list) or len(results) != len(chunk.items):
                    scheduler.fail(chunk, provider["node_id"])
                    strikes += 1
                    continue
                async with spent_lock:
                    spent += float(response.get("charged", 0.0))
                    providers_used[provider["node_id"]] = providers_used.get(provider["node_id"], 0) + 1
                scheduler.complete(chunk, provider["node_id"], results)

        try:
            await asyncio.gather(*(provider_worker(p) for p in providers))
        finally:
            refunded = (await self._tracker_request(
                {"type": "escrow.release", "escrow_id": escrow_id}
            ))["refunded"]

        incomplete = [c.index for c in chunks if not c.done]
        if incomplete:
            raise JobError(
                f"job failed: chunks {incomplete} could not be completed/verified "
                f"(spent {spent:.2f} credits, refunded {refunded:.2f})"
            )

        results: list[Any] = []
        for chunk in chunks:
            assert chunk.accepted is not None
            results.extend(chunk.accepted)
        return JobReport(
            results=results,
            chunks=len(chunks),
            spent=round(spent, 6),
            refunded=round(refunded, 6),
            providers_used=providers_used,
        )
