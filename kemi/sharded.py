"""Sharded big-model inference: run one transformer across many ships.

The flagship capability. A model's layers are split into contiguous groups
and each group is assigned to a *different* provider; generating a token
pipes the hidden state through that chain of ships. Decisive properties:

* **No single machine holds the whole model.** Each provider runs (and only
  loads) its layer slice via the ``ai.shard`` task. Give a model enough
  layers and it cannot fit on any one participant, yet the fleet runs it.
* **Verifiable.** ``ai.shard`` is deterministic, so each stage can be run on
  two providers and cross-checked (``redundancy=2``) — a sharded forward
  pass nobody can silently corrupt.
* **Private.** Hidden states travel end-to-end encrypted; a provider sees
  only its slice's activations, never the prompt or the output.

The consumer keeps only the small embedding and output head; the heavy
layers live on the fleet.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any

from .consumer import Consumer, JobError
from .model import (ModelSpec, decode, embed, encode, logits_for_last,
                    next_token)
from .names import ship_name

log = logging.getLogger("kemi.sharded")


@dataclass
class ShardPlan:
    """Which ship runs which contiguous layer range."""
    spec: ModelSpec
    assignments: list[tuple[dict[str, Any], int, int]]  # (record, start, end)

    @property
    def ships(self) -> int:
        return len({rec["node_id"] for rec, _, _ in self.assignments})

    def describe(self) -> str:
        return "  ".join(f"{ship_name(rec['node_id'])}:L{a}-{b - 1}"
                         for rec, a, b in self.assignments)


@dataclass
class ShardReport:
    text: str
    tokens: list[int]
    spent: float
    plan: ShardPlan
    redundancy: int
    verified_stages: int = 0  # stages cross-checked across distinct ships


def plan_shards(spec: ModelSpec, providers: list[dict[str, Any]],
                max_stages: int | None = None) -> list[tuple[dict[str, Any], int, int]]:
    """Split the model's layers across as many distinct providers as possible,
    contiguously and balanced. Fewer providers than ideal is fine — some run
    more than one slice — but we spread as widely as we can."""
    if not providers:
        raise JobError("no providers offer the ai.shard task")
    stages = min(spec.n_layers, len(providers))
    if max_stages:
        stages = min(stages, max_stages)
    base, extra = divmod(spec.n_layers, stages)
    assignments, layer = [], 0
    for i in range(stages):
        width = base + (1 if i < extra else 0)
        assignments.append((providers[i % len(providers)], layer, layer + width))
        layer += width
    return assignments


class ShardedLLM:
    """Drives autoregressive generation of a layer-sharded model over the fleet."""

    def __init__(self, consumer: Consumer, spec: ModelSpec | None = None):
        self.consumer = consumer
        self.spec = spec or ModelSpec()

    async def make_plan(self, redundancy: int = 1,
                        max_stages: int | None = None) -> ShardPlan:
        providers = await self.consumer.list_providers("ai.shard")
        if redundancy > 1 and len(providers) < 2:
            raise JobError("redundancy>1 needs at least two ai.shard providers")
        assignments = plan_shards(self.spec, providers, max_stages)
        if len(assignments) < 2:
            log.warning("only %d distinct ship(s) available; the model is not "
                        "meaningfully distributed", len({a[0]['node_id'] for a in assignments}))
        return ShardPlan(spec=self.spec, assignments=assignments)

    async def _run_stage(self, plan: ShardPlan, start: int, end: int,
                         primary: dict[str, Any], hidden: list, redundancy: int,
                         providers: list[dict[str, Any]]) -> tuple[list, float, bool]:
        """Execute one layer range, optionally on >1 distinct ship and
        cross-check the (deterministic) result. Returns (hidden, spent, verified)."""
        params = {"spec": self.spec.to_dict(), "start": start, "end": end}
        candidates = [primary] + [p for p in providers
                                  if p["node_id"] != primary["node_id"]]
        results: dict[str, list] = {}
        spent = 0.0
        needed = max(1, redundancy)
        from .crypto import digest
        votes: dict[str, int] = {}
        winner = None
        for record in candidates:
            if len(results) >= needed and winner is not None:
                break
            try:
                out = await self.consumer.execute_on(record, "ai.shard", [hidden], params,
                                                     timeout=120.0)
            except JobError as exc:
                log.warning("shard L%d-%d on %s failed: %s", start, end - 1,
                            ship_name(record["node_id"]), exc)
                continue
            spent += round(record["price"], 6)
            fingerprint = digest(out[0])
            votes[fingerprint] = votes.get(fingerprint, 0) + 1
            results[fingerprint] = out[0]
            if votes[fingerprint] >= (needed // 2 + 1):
                winner = fingerprint
        if winner is None:
            if not results:
                raise JobError(f"layer range L{start}-{end - 1} could not be run")
            winner = max(votes, key=votes.get)
        verified = redundancy > 1 and votes[winner] >= 2
        return results[winner], spent, verified

    async def generate(self, prompt: str, max_tokens: int = 16, *,
                       redundancy: int = 1, temperature: float = 0.0,
                       plan: ShardPlan | None = None) -> ShardReport:
        plan = plan or await self.make_plan(redundancy=redundancy)
        providers = await self.consumer.list_providers("ai.shard")
        rng = random.Random(0xC0FFEE)
        tokens = encode(prompt)
        spent = 0.0
        verified_stages = 0
        for _ in range(max_tokens):
            hidden = embed(self.spec, tokens[-self.spec.max_seq:])
            for record, start, end in plan.assignments:
                hidden, stage_spent, verified = await self._run_stage(
                    plan, start, end, record, hidden, redundancy, providers)
                spent += stage_spent
                verified_stages += int(verified)
            token = next_token(logits_for_last(self.spec, hidden),
                               temperature=temperature, rng=rng)
            tokens.append(token)
        generated = tokens[len(encode(prompt)):]
        return ShardReport(text=decode(generated), tokens=generated,
                           spent=round(spent, 6), plan=plan, redundancy=redundancy,
                           verified_stages=verified_stages)
