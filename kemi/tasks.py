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

import base64
import gzip
import hashlib
import math
import random
from typing import Any, Callable

TaskFn = Callable[[list[Any], dict[str, Any], dict[str, Any]], list[Any]]

TASKS: dict[str, TaskFn] = {}

# Tasks that only work when the provider has an AI backend configured.
BACKEND_REQUIRED = frozenset({"ai.generate", "ai.embed"})


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


@register_task("data.aggregate")
def _data_aggregate(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
    """Map-reduce over JSON records: each item is a list of objects; group by
    a key and aggregate a field (sum/avg/min/max/count). The consumer merges
    per-chunk partials, so terabyte-scale datasets shard naturally."""
    group_by = str(params.get("group_by", ""))
    field = str(params.get("field", ""))
    op = str(params.get("op", "sum"))
    if op not in ("sum", "avg", "min", "max", "count"):
        raise TaskError(f"unknown aggregate op: {op!r}")
    out = []
    for records in items:
        if not isinstance(records, list):
            raise TaskError("data.aggregate items must be lists of records")
        groups: dict[str, list[float]] = {}
        for record in records:
            if not isinstance(record, dict):
                raise TaskError("records must be JSON objects")
            key = str(record.get(group_by, "∅"))
            if op == "count":
                groups.setdefault(key, []).append(1.0)
            else:
                value = record.get(field)
                if isinstance(value, (int, float)):
                    groups.setdefault(key, []).append(float(value))
        reduced = {}
        for key, values in groups.items():
            if not values:
                continue
            if op in ("sum", "count"):
                reduced[key] = round(sum(values), 9)
            elif op == "avg":
                reduced[key] = round(sum(values) / len(values), 9)
            elif op == "min":
                reduced[key] = min(values)
            else:
                reduced[key] = max(values)
        out.append(reduced)
    return out


@register_task("crypto.pbkdf2")
def _crypto_pbkdf2(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
    """Real CPU-bound key hardening: PBKDF2-HMAC-SHA256 over each item.
    Useful for batch credential strengthening or honest benchmarking."""
    iterations = min(int(params.get("iterations", 100_000)), 5_000_000)
    salt = str(params.get("salt", "kemi")).encode("utf-8")
    return [
        hashlib.pbkdf2_hmac("sha256", str(item).encode("utf-8"), salt, iterations).hex()
        for item in items
    ]


@register_task("compress.gzip")
def _compress_gzip(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
    """Batch compression: items are utf-8 strings (or base64 blobs with
    encoding="base64"); returns base64 gzip payloads with ratios."""
    level = max(1, min(9, int(params.get("level", 6))))
    is_base64 = params.get("encoding") == "base64"
    out = []
    for item in items:
        try:
            raw = base64.b64decode(str(item)) if is_base64 else str(item).encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise TaskError(f"bad base64 input: {exc}")
        packed = gzip.compress(raw, compresslevel=level)
        out.append({
            "data": base64.b64encode(packed).decode("ascii"),
            "original": len(raw),
            "compressed": len(packed),
            "ratio": round(len(packed) / len(raw), 4) if raw else 1.0,
        })
    return out


try:  # registered only where numpy exists; the provider record advertises it
    import numpy as _np

    @register_task("sci.matmul")
    def _sci_matmul(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
        """Real BLAS-backed matrix multiplication: items are [A, B] pairs of
        nested lists; returns the products. Orders of magnitude faster than
        math.matmul - providers with numpy advertise this automatically."""
        out = []
        for pair in items:
            if not (isinstance(pair, list) and len(pair) == 2):
                raise TaskError("sci.matmul items must be [A, B] matrix pairs")
            a, b = _np.asarray(pair[0], dtype=float), _np.asarray(pair[1], dtype=float)
            if a.size > 1_000_000 or b.size > 1_000_000:
                raise TaskError("matrix too large")
            out.append((a @ b).round(9).tolist())
        return out
except ImportError:  # pragma: no cover
    pass


@register_task("ai.layer")
def _ai_layer(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
    """One shard of a layer-split model for pipeline parallelism.

    Items are hidden-state vectors (lists of floats); the output feeds the
    next stage. This reference implementation applies a deterministic
    pseudo-layer (fixed pseudo-random affine map + tanh) so pipelines are
    fully testable without model weights; a real backend would swap in
    actual transformer layers here.
    """
    layer = int(params.get("layer", 0))
    out = []
    for item in items:
        if not isinstance(item, list) or not all(isinstance(v, (int, float)) for v in item):
            raise TaskError("ai.layer items must be vectors of numbers")
        if len(item) > 4096:
            raise TaskError("hidden state too wide")
        rng = random.Random(f"kemi-layer-{layer}")
        weights = [rng.uniform(-1.0, 1.0) for _ in item]
        bias = rng.uniform(-0.1, 0.1)
        mixed = []
        for i, value in enumerate(item):
            # cheap deterministic mixing of neighbours, then a nonlinearity
            neighbour = item[(i + 1) % len(item)]
            mixed.append(round(math.tanh(value * weights[i] + neighbour * 0.5 + bias), 9))
        out.append(mixed)
    return out


@register_task("ai.shard")
def _ai_shard(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
    """Run a contiguous transformer layer range over hidden states.

    The heart of distributed big-model inference: a provider executes only
    the layers it was assigned (and only loads those layers' weights), so a
    model too large for any single machine runs across the fleet. Pure
    compute — no AI backend needed, fully sandboxable, and deterministic so
    redundancy can cross-check each shard.

    params: {spec, start, end}; items: list of hidden-state matrices.
    """
    from .model import ModelSpec, forward_layers

    spec = ModelSpec.from_dict(params["spec"])
    start, end = int(params["start"]), int(params["end"])
    out = []
    for hidden in items:
        if not isinstance(hidden, list) or not hidden:
            raise TaskError("ai.shard items must be non-empty hidden-state matrices")
        if len(hidden) > spec.max_seq:
            raise TaskError("sequence longer than the model's max_seq")
        if any(len(row) != spec.d_model for row in hidden):
            raise TaskError("hidden-state width does not match d_model")
        out.append(forward_layers(spec, start, end, hidden))
    return out


@register_task("ai.generate")
def _ai_generate(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
    """items: list of prompts -> list of completions via the provider's backend."""
    backend = context.get("ai_backend")
    if backend is None:
        raise TaskError("this provider has no AI backend configured")
    max_tokens = int(params.get("max_tokens", 64))
    return [backend.generate(str(prompt), max_tokens=max_tokens) for prompt in items]


@register_task("ai.embed")
def _ai_embed(items: list[Any], params: dict[str, Any], context: dict[str, Any]) -> list[Any]:
    """items: list of texts -> list of embedding vectors (unlocks RAG)."""
    backend = context.get("ai_backend")
    if backend is None:
        raise TaskError("this provider has no AI backend configured")
    embed = getattr(backend, "embed", None)
    if not callable(embed):
        raise TaskError("this provider's backend does not support embeddings")
    return [embed(str(text)) for text in items]
