"""A real (if small) transformer, built so its layers can be sharded across
the fleet — no single machine ever holds the whole model.

This is a genuine GPT-style forward pass: byte-level tokens → embedding +
sinusoidal positions → N causal multi-head self-attention + MLP blocks →
final layernorm → vocab logits. The arithmetic is real (attention,
layernorm, GELU); only the *weights* are generated deterministically from a
small spec so the whole thing runs with zero dependencies and every node
computes byte-identical results (which is what makes sharded inference
verifiable by redundancy).

The decisive property: a provider asked to run "layers 8-11" generates and
holds **only those four layers' weights**. Give a model enough layers and
no participant can hold them all — yet the fleet runs it end to end. Point a
provider at real trained weights (GGUF/safetensors) and the same sharding
machinery serves an actual model; the reference weights here keep it
testable and dependency-free.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

Vector = list[float]
Matrix = list[list[float]]   # row-major: M[out][in]


@dataclass(frozen=True)
class ModelSpec:
    name: str = "kemi-ref-small"
    n_layers: int = 6
    d_model: int = 32
    n_heads: int = 4
    d_ff: int = 64
    vocab: int = 256          # byte-level tokenizer
    max_seq: int = 512
    seed: int = 1337

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "n_layers": self.n_layers, "d_model": self.d_model,
            "n_heads": self.n_heads, "d_ff": self.d_ff, "vocab": self.vocab,
            "max_seq": self.max_seq, "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelSpec":
        spec = cls(
            name=str(data.get("name", "kemi-ref-small")),
            n_layers=int(data["n_layers"]), d_model=int(data["d_model"]),
            n_heads=int(data["n_heads"]), d_ff=int(data["d_ff"]),
            vocab=int(data["vocab"]), max_seq=int(data.get("max_seq", 512)),
            seed=int(data.get("seed", 1337)),
        )
        if spec.d_model % spec.n_heads != 0:
            raise ValueError("d_model must be divisible by n_heads")
        if not (1 <= spec.n_layers <= 256 and 1 <= spec.d_model <= 1024):
            raise ValueError("model spec out of supported range")
        return spec

    @property
    def key(self) -> tuple:
        return (self.name, self.n_layers, self.d_model, self.n_heads,
                self.d_ff, self.vocab, self.seed)


# ---------------------------------------------------------------------------
# Deterministic weights (generated per layer, cached, so a shard only ever
# materialises the slice it was asked for).
# ---------------------------------------------------------------------------

def _rng(*parts: Any) -> random.Random:
    return random.Random("|".join(str(p) for p in parts))


def _matrix(rng: random.Random, out_dim: int, in_dim: int) -> Matrix:
    scale = 1.0 / math.sqrt(in_dim)
    return [[rng.gauss(0.0, scale) for _ in range(in_dim)] for _ in range(out_dim)]


@lru_cache(maxsize=4096)
def _layer_weights(spec_key: tuple, layer: int) -> dict[str, Any]:
    name, n_layers, d, h, d_ff, vocab, seed = spec_key
    rng = _rng("layer", seed, name, layer)
    return {
        "ln1_g": [1.0] * d, "ln1_b": [0.0] * d,
        "wq": _matrix(rng, d, d), "wk": _matrix(rng, d, d),
        "wv": _matrix(rng, d, d), "wo": _matrix(rng, d, d),
        "ln2_g": [1.0] * d, "ln2_b": [0.0] * d,
        "w1": _matrix(rng, d_ff, d), "b1": [0.0] * d_ff,
        "w2": _matrix(rng, d, d_ff), "b2": [0.0] * d,
    }


@lru_cache(maxsize=64)
def _embedding(spec_key: tuple) -> Matrix:
    name, n_layers, d, h, d_ff, vocab, seed = spec_key
    rng = _rng("embed", seed, name)
    return _matrix(rng, vocab, d)


@lru_cache(maxsize=64)
def _head(spec_key: tuple) -> Matrix:
    name, n_layers, d, h, d_ff, vocab, seed = spec_key
    rng = _rng("head", seed, name)
    return _matrix(rng, vocab, d)


# ---------------------------------------------------------------------------
# Math primitives (pure Python; small dims keep it fast and dependency-free)
# ---------------------------------------------------------------------------

def _linear(x: Vector, w: Matrix, b: list[float] | None = None) -> Vector:
    out = [sum(wi * xi for wi, xi in zip(row, x)) for row in w]
    if b is not None:
        out = [o + bi for o, bi in zip(out, b)]
    return out


def _layernorm(x: Vector, g: list[float], b: list[float]) -> Vector:
    mean = sum(x) / len(x)
    var = sum((xi - mean) ** 2 for xi in x) / len(x)
    inv = 1.0 / math.sqrt(var + 1e-5)
    return [gi * ((xi - mean) * inv) + bi for xi, gi, bi in zip(x, g, b)]


def _gelu(x: Vector) -> Vector:
    return [0.5 * xi * (1.0 + math.tanh(0.7978845608 * (xi + 0.044715 * xi ** 3)))
            for xi in x]


def _softmax(scores: Vector) -> Vector:
    m = max(scores)
    exps = [math.exp(s - m) for s in scores]
    total = sum(exps)
    return [e / total for e in exps]


def _positional(d_model: int, position: int) -> Vector:
    vec = []
    for i in range(d_model):
        angle = position / (10000 ** ((i - (i % 2)) / d_model))
        vec.append(math.sin(angle) if i % 2 == 0 else math.cos(angle))
    return vec


# ---------------------------------------------------------------------------
# Forward pass — operating on a single sequence: hidden is (seq_len, d_model)
# ---------------------------------------------------------------------------

def embed(spec: ModelSpec, token_ids: list[int]) -> Matrix:
    table = _embedding(spec.key)
    return [[v + p for v, p in zip(table[tid % spec.vocab], _positional(spec.d_model, pos))]
            for pos, tid in enumerate(token_ids)]


def _attention(spec: ModelSpec, w: dict[str, Any], normed: Matrix) -> Matrix:
    seq_len, d, heads = len(normed), spec.d_model, spec.n_heads
    head_dim = d // heads
    q = [_linear(x, w["wq"]) for x in normed]
    k = [_linear(x, w["wk"]) for x in normed]
    v = [_linear(x, w["wv"]) for x in normed]
    scale = 1.0 / math.sqrt(head_dim)
    context = [[0.0] * d for _ in range(seq_len)]
    for head in range(heads):
        lo, hi = head * head_dim, (head + 1) * head_dim
        for i in range(seq_len):  # causal: attend to 0..i
            scores = [sum(q[i][t] * k[j][t] for t in range(lo, hi)) * scale
                      for j in range(i + 1)]
            weights = _softmax(scores)
            for t in range(lo, hi):
                context[i][t] = sum(weights[j] * v[j][t] for j in range(i + 1))
    return [_linear(c, w["wo"]) for c in context]


def forward_layer(spec: ModelSpec, layer: int, hidden: Matrix) -> Matrix:
    w = _layer_weights(spec.key, layer)
    normed = [_layernorm(x, w["ln1_g"], w["ln1_b"]) for x in hidden]
    attn = _attention(spec, w, normed)
    hidden = [[a + b for a, b in zip(h, at)] for h, at in zip(hidden, attn)]
    out = []
    for h in hidden:
        n = _layernorm(h, w["ln2_g"], w["ln2_b"])
        ff = _linear(_gelu(_linear(n, w["w1"], w["b1"])), w["w2"], w["b2"])
        out.append([a + b for a, b in zip(h, ff)])
    return out


def forward_layers(spec: ModelSpec, start: int, end: int, hidden: Matrix) -> Matrix:
    """Run the contiguous half-open layer range [start, end). A provider only
    ever touches — and only ever generates weights for — these layers."""
    if not (0 <= start <= end <= spec.n_layers):
        raise ValueError(f"layer range [{start},{end}) outside 0..{spec.n_layers}")
    for layer in range(start, end):
        hidden = forward_layer(spec, layer, hidden)
    return hidden


def final_norm(spec: ModelSpec, hidden: Matrix) -> Matrix:
    g, b = [1.0] * spec.d_model, [0.0] * spec.d_model
    return [_layernorm(x, g, b) for x in hidden]


def logits_for_last(spec: ModelSpec, hidden: Matrix) -> Vector:
    return _linear(final_norm(spec, hidden)[-1], _head(spec.key))


def next_token(logits: Vector, temperature: float = 0.0,
               rng: random.Random | None = None) -> int:
    if temperature <= 0.0:
        return max(range(len(logits)), key=lambda i: logits[i])
    scaled = [l / temperature for l in logits]
    probs = _softmax(scaled)
    r = (rng or random).random()
    cumulative = 0.0
    for i, p in enumerate(probs):
        cumulative += p
        if r <= cumulative:
            return i
    return len(probs) - 1


# ---------------------------------------------------------------------------
# Byte-level tokenizer (vocab == 256)
# ---------------------------------------------------------------------------

def encode(text: str) -> list[int]:
    return list(text.encode("utf-8"))


def decode(token_ids: list[int]) -> str:
    return bytes(t % 256 for t in token_ids).decode("utf-8", "replace")
