"""Built-in task types.

A *job* is a named task applied to a list of items. The consumer splits the
items into chunks (BitTorrent-style pieces) and providers execute each chunk
independently:

    run(items, params, context) -> list of results, one per item

Tasks are allowlisted by name: providers never execute arbitrary code sent
over the network. New capabilities are added by registering a task here (or
in a provider-side plugin) so the security boundary stays explicit.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, Callable

TaskFn = Callable[[list[Any], dict[str, Any], dict[str, Any]], list[Any]]

TASKS: dict[str, TaskFn] = {}


class TaskError(Exception):
    pass


def register_task(name: str) -> Callable[[TaskFn], TaskFn]:
    def decorator(fn: TaskFn) -> TaskFn:
        TASKS[name] = fn
        return fn

    return decorator


def run_task(
    name: str,
    items: list[Any],
    params: dict[str, Any],
    context: dict[str, Any],
) -> list[Any]:
    fn = TASKS.get(name)
    if fn is None:
        raise TaskError(f"unsupported task: {name!r}")
    results = fn(items, params, context)
    if len(results) != len(items):
        raise TaskError(f"task {name!r} returned {len(results)} results for {len(items)} items")
    return results


@register_task("hash.sha256")
def _hash_sha256(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
    """items: list of strings -> list of hex digests."""
    rounds = int(params.get("rounds", 1))
    out = []
    for item in items:
        digest = str(item).encode("utf-8")
        for _ in range(max(1, rounds)):
            digest = hashlib.sha256(digest).digest()
        out.append(digest.hex())
    return out


@register_task("text.wordcount")
def _wordcount(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
    """items: list of strings -> list of {words, chars, lines}."""
    return [
        {
            "words": len(str(item).split()),
            "chars": len(str(item)),
            "lines": str(item).count("\n") + 1,
        }
        for item in items
    ]


@register_task("math.matmul")
def _matmul(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
    """CPU benchmark: items are matrix sizes; multiply two seeded random
    matrices of that size and return a checksum of the product."""
    out = []
    for n in items:
        n = int(n)
        if n <= 0 or n > 512:
            raise TaskError(f"matrix size out of range: {n}")
        rng = random.Random(n)
        a = [[rng.random() for _ in range(n)] for _ in range(n)]
        b = [[rng.random() for _ in range(n)] for _ in range(n)]
        checksum = 0.0
        for i in range(n):
            row = a[i]
            for j in range(n):
                checksum += sum(row[k] * b[k][j] for k in range(n))
        out.append(round(checksum, 6))
    return out


@register_task("ai.generate")
def _ai_generate(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
    """items: list of prompts -> list of completions via the provider's backend."""
    backend = context.get("ai_backend")
    if backend is None:
        raise TaskError("this provider has no AI backend configured")
    max_tokens = int(params.get("max_tokens", 64))
    return [backend.generate(str(prompt), max_tokens=max_tokens) for prompt in items]
