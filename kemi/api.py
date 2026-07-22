"""The high-level Python API: use the fleet from three lines of code.

    import asyncio, kemi

    async def main():
        async with kemi.connect(peer="203.0.113.7:7700") as fleet:
            hashes = await fleet.run("hash.sha256", ["a", "b", "c"])
            async for token in fleet.stream("Why do P2P networks matter?"):
                print(token, end="", flush=True)

    asyncio.run(main())

``connect()`` joins as a light peer (no compute shared), with an ephemeral
identity and in-memory wallet by default - pass ``identity_path`` /
``ledger_path`` to keep a persistent wallet and earn toward ranks.
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

from .consumer import Consumer, Job, JobError, JobReport, PipelineReport, PipelineStage
from .identity import Identity
from .names import rank_for, ship_name
from .node import PeerNode

__all__ = ["Fleet", "connect", "Job", "JobError", "JobReport",
           "PipelineStage", "PipelineReport"]


def _normalise_peers(peer: Any) -> list[tuple[str, int]]:
    if peer is None:
        return []
    if isinstance(peer, str):
        host, _, port = peer.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"expected 'HOST:PORT', got {peer!r}")
        return [(host, int(port))]
    if isinstance(peer, tuple):
        return [(str(peer[0]), int(peer[1]))]
    return [p for item in peer for p in _normalise_peers(item)]


class Fleet:
    """A connected handle on the swarm. Create via :func:`connect`."""

    def __init__(self, node: PeerNode):
        self.node = node
        self._consumer = Consumer(node)

    # -- identity ------------------------------------------------------------

    @property
    def ship(self) -> str:
        return ship_name(self.node.identity.node_id)

    @property
    def node_id(self) -> str:
        return self.node.identity.node_id

    def rank(self) -> str:
        title, insignia, _ = rank_for(
            self.node.ledger.total_earned(self.node.identity.node_id))
        return f"{insignia} {title}"

    # -- money ----------------------------------------------------------------

    async def balance(self) -> float:
        return await self._consumer.balance()

    # -- discovery --------------------------------------------------------------

    async def providers(self, task: str = "hash.sha256") -> list[dict[str, Any]]:
        return await self._consumer.list_providers(task)

    # -- work --------------------------------------------------------------------

    async def models(self, task: str = "ai.generate") -> list[str]:
        """AI model names currently on offer in the fleet."""
        return await self._consumer.models(task)

    async def run(self, task: str, items: list[Any], *,
                  params: dict[str, Any] | None = None, chunk_size: int = 8,
                  redundancy: int = 1, encrypt: bool = True,
                  chunk_timeout: float = 120.0, model: str | None = None,
                  full_report: bool = False) -> list[Any] | JobReport:
        """Run a job across the fleet; returns the results (or the full
        :class:`JobReport` with ``full_report=True``)."""
        report = await self._consumer.run_job(Job(
            task=task, items=items, params=dict(params or {}),
            chunk_size=chunk_size, redundancy=redundancy,
            encrypt=encrypt, chunk_timeout=chunk_timeout, model=model,
        ))
        return report if full_report else report.results

    async def generate(self, prompt: str, *, max_tokens: int = 128,
                       redundancy: int = 1, model: str | None = None) -> str:
        """One prompt in, one completion out."""
        results = await self.run("ai.generate", [prompt],
                                 params={"max_tokens": max_tokens},
                                 chunk_size=1, redundancy=redundancy, model=model)
        return str(results[0])

    async def embed(self, texts: list[str], *, redundancy: int = 1,
                    model: str | None = None) -> list[list[float]]:
        """Embed a batch of texts across the fleet (RAG building block)."""
        return await self.run("ai.embed", texts, chunk_size=8,
                              redundancy=redundancy, model=model)

    async def rag_search(self, query: str, documents: list[str], *, top_k: int = 3,
                         model: str | None = None) -> list[dict[str, Any]]:
        """Retrieval-augmented search over the fleet: embed the query and
        documents (ai.embed), then rank by cosine similarity (vector.search).
        Returns ``[{"document", "index", "score"}, ...]`` best-first."""
        if not documents:
            return []
        vectors = await self.embed([query] + list(documents), model=model)
        ranked = await self.run("vector.search",
                                [{"q": vectors[0], "docs": vectors[1:]}],
                                chunk_size=1)
        return [{"document": documents[hit["index"]], **hit}
                for hit in ranked[0][:top_k]]

    async def stream(self, prompt: str, *, max_tokens: int = 128,
                     chunk_timeout: float = 300.0,
                     model: str | None = None,
                     first_token_timeout: float = 20.0,
                     first_token_max: float = 120.0) -> AsyncIterator[str]:
        """Yield completion tokens live as a provider generates them."""
        async for event in self._consumer.stream_generate(
                [prompt], {"max_tokens": max_tokens},
                chunk_timeout=chunk_timeout, model=model,
                first_token_timeout=first_token_timeout,
                first_token_max=first_token_max):
            if event.get("done"):
                return
            yield event["token"]

    async def stream_many(self, prompts: list[str], *, max_tokens: int = 128,
                          chunk_timeout: float = 300.0) -> AsyncIterator[dict[str, Any]]:
        """Multi-prompt streaming: yields the consumer's raw events
        (``{"item", "token"}`` then a final ``{"done": True, ...}``)."""
        async for event in self._consumer.stream_generate(
                prompts, {"max_tokens": max_tokens}, chunk_timeout=chunk_timeout):
            yield event

    async def pipeline(self, stages: list[PipelineStage | dict[str, Any]],
                       items: list[Any]) -> PipelineReport:
        normalised = [stage if isinstance(stage, PipelineStage)
                      else PipelineStage(**stage) for stage in stages]
        return await self._consumer.run_pipeline(normalised, items)

    async def shard_generate(self, prompt: str, *, max_tokens: int = 16,
                             redundancy: int = 1, temperature: float = 0.0,
                             spec: "ModelSpec | None" = None):
        """Run a transformer sharded across the fleet — its layers split over
        many ships, none holding the whole model. Returns a ShardReport."""
        from .model import ModelSpec
        from .sharded import ShardedLLM

        llm = ShardedLLM(self._consumer, spec or ModelSpec())
        return await llm.generate(prompt, max_tokens=max_tokens,
                                  redundancy=redundancy, temperature=temperature)

    # -- lifecycle --------------------------------------------------------------

    async def close(self) -> None:
        await self.node.stop()


@contextlib.asynccontextmanager
async def connect(peer: Any = None, *, invite: str | None = None,
                  lan: bool = False, identity_path: str | None = None,
                  ledger_path: str = ":memory:", share: bool = False,
                  price: float = 1.0, **node_kwargs: Any):
    """Join the fleet and yield a :class:`Fleet` handle.

    ``peer`` accepts ``"host:port"``, a ``(host, port)`` tuple or a list of
    either. Alternatively pass a friend's ``invite`` code, or ``lan=True``
    to auto-discover a fleet on the local network. ``share=True`` also
    offers this machine's compute while connected.
    """
    peers = _normalise_peers(peer)
    if invite:
        from .invite import parse_invite

        peers.extend(parse_invite(invite)["peers"])
    identity = (Identity.load_or_create(identity_path) if identity_path
                else Identity.create())
    node = PeerNode(identity, bootstrap=peers, lan=lan or not peers,
                    ledger_path=ledger_path, provide=share, price=price,
                    **node_kwargs)
    await node.start()
    fleet = Fleet(node)
    try:
        yield fleet
    finally:
        await fleet.close()
