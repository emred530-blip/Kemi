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

from .crypto import canonical
from .discovery import find_providers
from .e2e import E2EError, derive_box_key, open_sealed, seal
from .node import PeerNode
from .protocol import NETWORK_ERRORS, request, stream_request

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
    encrypt: bool = True  # end-to-end encrypt payloads when the provider supports it
    model: str | None = None  # require providers advertising this AI model


@dataclass
class JobReport:
    results: list[Any]
    chunks: int
    spent: float                    # credits paid for delivered chunks
    exposure: float                 # credits signed away to providers that failed
    providers_used: dict[str, int]
    encrypted_chunks: int = 0       # chunks that travelled end-to-end encrypted


@dataclass
class PipelineStage:
    """One stage of a pipeline job: the previous stage's results become
    this stage's items (the basis for layer-sharded model execution)."""

    task: str
    params: dict[str, Any] = field(default_factory=dict)
    chunk_size: int = 8
    redundancy: int = 1


@dataclass
class PipelineReport:
    results: list[Any]
    stages: list[JobReport]

    @property
    def spent(self) -> float:
        return round(sum(stage.spent for stage in self.stages), 6)


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

    async def list_providers(self, task: str,
                             model: str | None = None) -> list[dict[str, Any]]:
        providers = await find_providers(self.node.dht, task)
        usable = []
        for p in providers:
            if p["node_id"] == self.node.identity.node_id:
                continue
            if self.node.reputation.is_banned(p["node_id"]):
                continue
            if self.node.ledger.is_flagged(p["node_id"]):
                continue
            if model is not None and p.get("model") != model:
                continue  # multi-model marketplace: consumer pins a model
            usable.append(p)
        # Best reputation first, then cheapest.
        usable.sort(key=lambda p: (-self.node.reputation.score(p["node_id"]), p["price"]))
        return usable

    async def models(self, task: str = "ai.generate") -> list[str]:
        """Distinct AI model names currently advertised for a task."""
        seen = {p.get("model") for p in await self.list_providers(task)}
        return sorted(m for m in seen if m)

    async def run_job_embeddings(self, texts: list[str],
                                 model: str | None = None) -> list[list[float]]:
        """Embed a batch of texts over the fleet (used by the OpenAI gateway)."""
        report = await self.run_job(Job(task="ai.embed", items=list(texts),
                                        chunk_size=8, model=model))
        return report.results

    async def execute_on(self, record: dict[str, Any], task: str, items: list[Any],
                         params: dict[str, Any] | None = None, *,
                         encrypt: bool = True, timeout: float = 120.0) -> list[Any]:
        """Run one chunk on one *specific* provider — payment, e2e encryption
        and ledger settlement included. The building block for sharded
        inference, where each stage must land on a chosen ship rather than
        whoever the scheduler prefers. Returns the results or raises JobError.
        """
        ledger, my_id = self.node.ledger, self.node.identity.node_id
        provider_id = record["node_id"]
        box_key = None
        if encrypt and record.get("e2e") and isinstance(record.get("pubkey"), str):
            try:
                box_key = derive_box_key(self.node.identity.key.seed,
                                         bytes.fromhex(record["pubkey"]))
            except (E2EError, ValueError):
                box_key = None
        amount = round(record["price"] * len(items), 6)
        if ledger.balance(my_id) < amount:
            raise JobError(f"insufficient credits: need {amount:.2f}")
        tx = ledger.make_tx(self.node.identity, provider_id, amount)
        message: dict[str, Any] = {"type": "task.execute", "task": task, "payment": tx}
        if box_key is not None:
            message["enc"] = seal(box_key, canonical({"items": items, "params": params or {}}))
        else:
            message["items"], message["params"] = items, params or {}
        try:
            response = await send_to_provider(record, message, timeout=timeout)
        except NETWORK_ERRORS as exc:
            self.node.reputation.record(provider_id, "chunk_fail")
            raise JobError(f"provider unreachable: {exc}")
        if not response.get("ok"):
            self.node.reputation.record(provider_id, "chunk_fail")
            raise JobError(f"{response.get('error')}: {response.get('detail', '')}")
        if box_key is not None:
            try:
                results = json.loads(open_sealed(box_key, response.get("enc") or {})).get("results")
            except (E2EError, ValueError, json.JSONDecodeError):
                results = None
        else:
            results = response.get("results")
        if not isinstance(results, list) or len(results) != len(items):
            self.node.reputation.record(provider_id, "chunk_fail")
            raise JobError("provider returned a malformed result")
        ledger.add_tx(tx)
        self.node.gossip_tx(tx)
        self.node.reputation.record(provider_id, "chunk_ok")
        return results

    async def run_job(self, job: Job) -> JobReport:
        if not job.items:
            return JobReport(results=[], chunks=0, spent=0.0, exposure=0.0, providers_used={})
        if job.redundancy < 1:
            raise JobError("redundancy must be >= 1")

        providers = await self.list_providers(job.task, model=job.model)
        if not providers:
            suffix = f" with model {job.model!r}" if job.model else ""
            raise JobError(f"no usable providers offer task {job.task!r}{suffix}")
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

        encrypted_chunks = 0

        async def provider_worker(record: dict[str, Any]) -> None:
            nonlocal spent, exposure, encrypted_chunks
            provider_id = record["node_id"]
            # One shared box key per provider covers both directions.
            box_key: bytes | None = None
            if job.encrypt and record.get("e2e") and isinstance(record.get("pubkey"), str):
                try:
                    box_key = derive_box_key(self.node.identity.key.seed,
                                             bytes.fromhex(record["pubkey"]))
                except (E2EError, ValueError):
                    box_key = None
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
                message: dict[str, Any] = {"type": "task.execute", "task": job.task,
                                           "payment": tx}
                if box_key is not None:
                    message["enc"] = seal(box_key, canonical(
                        {"items": chunk.items, "params": job.params}))
                else:
                    message["items"] = chunk.items
                    message["params"] = job.params
                try:
                    response = await send_to_provider(record, message,
                                                      timeout=job.chunk_timeout)
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
                if box_key is not None:
                    try:
                        inner = json.loads(open_sealed(box_key, response.get("enc") or {}))
                        results = inner.get("results")
                    except (E2EError, ValueError, json.JSONDecodeError):
                        results = None
                else:
                    results = response.get("results")
                if not isinstance(results, list) or len(results) != len(chunk.items):
                    scheduler.fail(chunk, provider_id)
                    strikes += 1
                    exposure += amount
                    self.node.reputation.record(provider_id, "chunk_fail")
                    continue
                strikes = 0
                if box_key is not None:
                    encrypted_chunks += 1
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
            encrypted_chunks=encrypted_chunks,
        )

    async def stream_generate(self, prompts: list[Any],
                              params: dict[str, Any] | None = None,
                              chunk_timeout: float = 300.0,
                              encrypt: bool = True, model: str | None = None):
        """Live LLM output over the swarm: an async generator that yields
        ``{"item", "token"}`` events as the provider produces them, then one
        final ``{"done": True, "results", "spent", "provider"}`` summary.

        Works with relayed (NATed) providers too: the stream is multiplexed
        through their relay peer, which - like everything else on the path -
        sees only sealed ciphertext. Failover to another provider happens
        only before the first token; once tokens have flowed, an
        interruption is surfaced as a JobError rather than silently
        regenerating (and re-paying).
        """
        if not prompts:
            raise JobError("no prompts to stream")
        params = dict(params or {})
        providers = [p for p in await self.list_providers("ai.generate", model=model)
                     if p.get("stream")]
        if not providers:
            raise JobError("no streaming-capable providers for ai.generate")

        ledger = self.node.ledger
        my_id = self.node.identity.node_id
        last_error = "no providers tried"
        for record in providers:
            amount = round(record["price"] * len(prompts), 6)
            if ledger.balance(my_id) < amount:
                raise JobError(f"insufficient credits: balance "
                               f"{ledger.balance(my_id):.2f} < {amount:.2f}")
            box_key: bytes | None = None
            if encrypt and record.get("e2e") and isinstance(record.get("pubkey"), str):
                try:
                    box_key = derive_box_key(self.node.identity.key.seed,
                                             bytes.fromhex(record["pubkey"]))
                except (E2EError, ValueError):
                    box_key = None
            tx = ledger.make_tx(self.node.identity, record["node_id"], amount)
            message: dict[str, Any] = {"type": "task.stream", "task": "ai.generate",
                                       "payment": tx}
            if box_key is not None:
                message["enc"] = seal(box_key, canonical(
                    {"items": prompts, "params": params}))
            else:
                message["items"] = prompts
                message["params"] = params

            relay = record.get("relay")
            if relay:
                target_host, target_port = relay["host"], relay["port"]
                wire_message: dict[str, Any] = {"type": "relay.stream",
                                                "to": record["node_id"],
                                                "inner": message}
            else:
                target_host, target_port = record["host"], record["port"]
                wire_message = message

            streamed_any = False
            failed = False
            try:
                async for raw in stream_request(target_host, target_port,
                                                wire_message, timeout=chunk_timeout):
                    event = raw
                    if box_key is not None and "enc" in raw:
                        try:
                            event = json.loads(open_sealed(box_key, raw["enc"]))
                        except (E2EError, ValueError, json.JSONDecodeError):
                            last_error = "undecryptable stream data"
                            failed = True
                            break
                    if event.get("evt") == "token":
                        streamed_any = True
                        yield {"item": int(event.get("item", 0)),
                               "token": str(event.get("t", ""))}
                        continue
                    if raw.get("end"):
                        if event.get("evt") == "end" and event.get("ok"):
                            # The provider applied our transfer before
                            # streaming; mirror it locally and gossip.
                            ledger.add_tx(tx)
                            self.node.gossip_tx(tx)
                            self.node.reputation.record(record["node_id"], "chunk_ok")
                            yield {"done": True, "results": event.get("results"),
                                   "spent": amount, "provider": record["node_id"]}
                            return
                        last_error = (f"{event.get('error', raw.get('error'))}: "
                                      f"{event.get('detail', raw.get('detail', ''))}")
                        failed = True
                        break
            except NETWORK_ERRORS as exc:
                last_error = str(exc)
                failed = True
            if failed:
                self.node.reputation.record(record["node_id"], "chunk_fail")
                log.warning("streaming via %s failed: %s",
                            record["node_id"][:12], last_error)
                if streamed_any:
                    raise JobError(f"stream interrupted mid-output: {last_error}")
        raise JobError(f"streaming failed on all providers: {last_error}")

    async def run_pipeline(self, stages: list[PipelineStage], items: list[Any],
                           chunk_timeout: float = 120.0,
                           encrypt: bool = True) -> PipelineReport:
        """Run items through a chain of stages, each distributed over the swarm.

        Stage ``k+1`` consumes stage ``k``'s outputs, so a model sharded into
        layer groups can be executed across providers none of which could
        host the whole model (pipeline parallelism). Every stage gets the
        full scheduler treatment: chunking, retries, redundancy voting,
        per-chunk signed payments and end-to-end encryption.
        """
        if not stages:
            raise JobError("pipeline needs at least one stage")
        reports: list[JobReport] = []
        current = items
        for index, stage in enumerate(stages):
            report = await self.run_job(Job(
                task=stage.task,
                items=current,
                params=stage.params,
                chunk_size=stage.chunk_size,
                redundancy=stage.redundancy,
                chunk_timeout=chunk_timeout,
                encrypt=encrypt,
            ))
            log.info("pipeline stage %d/%d (%s) done: %d items, %.2f credits",
                     index + 1, len(stages), stage.task, len(report.results),
                     report.spent)
            reports.append(report)
            current = report.results
        return PipelineReport(results=current, stages=reports)
