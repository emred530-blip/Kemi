"""Self-training synapse network: the fleet brain.

A pure-stdlib neural network that trains itself online — one example at a
time, forever — from the node's own experience. No dataset, no labels, no
human in the loop: every chunk a provider serves (or botches) becomes a
training example the moment it happens, and the consumer blends the brain's
predictions into provider selection. The longer a ship sails, the smarter
it gets about whom to hire.

The network is deliberately small (default 6-8-4-1): it must train in
microseconds on a Raspberry Pi and its full synapse state must stream to
the dashboard every 2 seconds for live visualisation.
"""

from __future__ import annotations

import json
import math
import os
import random
from collections import deque
from typing import Any, Sequence

# How many outcomes the brain wants to see before its opinion is trusted
# in provider ranking (before that it is still learning in the background).
WARMUP_STEPS = 20
# Blend: rank providers by 70% reputation + 30% brain prediction.
BRAIN_WEIGHT = 0.3
AUTOSAVE_EVERY = 25
LOSS_HISTORY = 120


def _sigmoid(x: float) -> float:
    if x < -60.0:
        return 0.0
    if x > 60.0:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


class SynapseNet:
    """A tiny multi-layer perceptron trained by online gradient descent.

    ``train_step`` takes a single example and immediately adjusts every
    synapse — the network never stops learning.
    """

    def __init__(self, sizes: Sequence[int], lr: float = 0.1,
                 seed: int | None = None):
        if len(sizes) < 2:
            raise ValueError("need at least an input and an output layer")
        rng = random.Random(seed)
        self.sizes = [int(s) for s in sizes]
        self.lr = float(lr)
        self.weights = [
            [[rng.gauss(0.0, 1.0) / math.sqrt(self.sizes[i]) for _ in range(self.sizes[i])]
             for _ in range(self.sizes[i + 1])]
            for i in range(len(self.sizes) - 1)
        ]
        self.biases = [[0.0] * self.sizes[i + 1] for i in range(len(self.sizes) - 1)]
        self.steps = 0
        self.loss_ema: float | None = None

    # -- inference ---------------------------------------------------------

    def forward(self, x: Sequence[float]) -> list[list[float]]:
        """Activations for every layer (input included)."""
        if len(x) != self.sizes[0]:
            raise ValueError(f"expected {self.sizes[0]} inputs, got {len(x)}")
        acts = [[float(v) for v in x]]
        for lw, lb in zip(self.weights, self.biases):
            prev = acts[-1]
            acts.append([
                _sigmoid(sum(w * p for w, p in zip(neuron, prev)) + b)
                for neuron, b in zip(lw, lb)
            ])
        return acts

    def predict(self, x: Sequence[float]) -> list[float]:
        return self.forward(x)[-1]

    # -- online learning ---------------------------------------------------

    def train_step(self, x: Sequence[float], y: Sequence[float]) -> float:
        """One example in, one gradient step out. Returns squared-error loss."""
        acts = self.forward(x)
        out = acts[-1]
        if len(y) != len(out):
            raise ValueError(f"expected {len(out)} targets, got {len(y)}")
        loss = sum((t - o) ** 2 for t, o in zip(y, out)) / len(out)

        # delta for the output layer (sigmoid derivative: o * (1 - o))
        delta = [(o - t) * o * (1.0 - o) for o, t in zip(out, y)]
        for li in range(len(self.weights) - 1, -1, -1):
            prev = acts[li]
            if li > 0:  # propagate before the weights move
                nxt = [
                    prev[j] * (1.0 - prev[j]) * sum(
                        self.weights[li][k][j] * delta[k]
                        for k in range(len(delta)))
                    for j in range(len(prev))
                ]
            for k, d in enumerate(delta):
                row = self.weights[li][k]
                for j, p in enumerate(prev):
                    row[j] -= self.lr * d * p
                self.biases[li][k] -= self.lr * d
            if li > 0:
                delta = nxt

        self.steps += 1
        self.loss_ema = loss if self.loss_ema is None else \
            0.98 * self.loss_ema + 0.02 * loss
        return loss

    # -- persistence ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"sizes": self.sizes, "lr": self.lr, "steps": self.steps,
                "loss_ema": self.loss_ema,
                "weights": self.weights, "biases": self.biases}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SynapseNet":
        net = cls(data["sizes"], lr=data.get("lr", 0.1))
        net.weights = [[list(map(float, row)) for row in layer]
                       for layer in data["weights"]]
        net.biases = [list(map(float, layer)) for layer in data["biases"]]
        net.steps = int(data.get("steps", 0))
        net.loss_ema = data.get("loss_ema")
        return net


class FleetBrain:
    """The ship's self-training brain: predicts whether hiring a provider
    will succeed, learning from every real outcome.

    Features (all scaled to 0..1): price, reputation, direct/relay,
    e2e support, CPU count, GPU count. Target: did the chunk succeed?
    """

    FEATURE_NAMES = ["price", "rep", "direct", "e2e", "cpu", "gpu"]

    def __init__(self, path: str | None = None, lr: float = 0.4,
                 seed: int | None = None):
        self.path = path
        self.net: SynapseNet | None = None
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.net = SynapseNet.from_dict(json.load(fh))
            except (OSError, ValueError, KeyError, TypeError):
                self.net = None  # corrupt brain file: start fresh
        if self.net is None:
            self.net = SynapseNet([len(self.FEATURE_NAMES), 8, 4, 1],
                                  lr=lr, seed=seed)
        self.loss_history: deque[float] = deque(maxlen=LOSS_HISTORY)
        self.acc_ema: float | None = None

    # -- feature extraction ------------------------------------------------

    def features(self, record: dict[str, Any], rep: float) -> list[float]:
        res = record.get("resources") or {}
        return [
            min(1.0, float(record.get("price", 1.0)) / 5.0),
            max(0.0, min(1.0, float(rep))),
            0.0 if record.get("relay") else 1.0,
            1.0 if record.get("e2e") else 0.0,
            min(1.0, float(res.get("cpu_count") or 1) / 16.0),
            min(1.0, len(res.get("gpus") or []) / 4.0),
        ]

    # -- the two verbs -------------------------------------------------------

    def score(self, record: dict[str, Any], rep: float) -> float:
        """Predicted probability that hiring this provider succeeds."""
        return self.net.predict(self.features(record, rep))[0]

    def learn(self, record: dict[str, Any], rep: float, ok: bool) -> float:
        """Feed one real outcome back into the synapses."""
        x = self.features(record, rep)
        pred = self.net.predict(x)[0]
        loss = self.net.train_step(x, [1.0 if ok else 0.0])
        self.loss_history.append(round(loss, 5))
        hit = 1.0 if (pred >= 0.5) == ok else 0.0
        self.acc_ema = hit if self.acc_ema is None else \
            0.95 * self.acc_ema + 0.05 * hit
        if self.path and self.net.steps % AUTOSAVE_EVERY == 0:
            self.save()
        return loss

    @property
    def trained(self) -> bool:
        return self.net.steps >= WARMUP_STEPS

    def rank_score(self, record: dict[str, Any], rep: float) -> float:
        """Blended ranking score used by the consumer: mostly reputation,
        seasoned with the brain once it has seen enough outcomes."""
        if not self.trained:
            return rep
        return (1.0 - BRAIN_WEIGHT) * rep + BRAIN_WEIGHT * self.score(record, rep)

    # -- persistence / UI ----------------------------------------------------

    def save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.net.to_dict(), fh)
            os.replace(tmp, self.path)
        except OSError:
            pass  # a brain that cannot persist still learns in memory

    def snapshot_ui(self) -> dict[str, Any]:
        """Compact live state for the dashboard's synapse visualisation."""
        return {
            "sizes": self.net.sizes,
            "features": self.FEATURE_NAMES,
            "weights": [[[round(w, 3) for w in row] for row in layer]
                        for layer in self.net.weights],
            "steps": self.net.steps,
            "loss": round(self.net.loss_ema, 5) if self.net.loss_ema is not None else None,
            "accuracy": round(self.acc_ema, 3) if self.acc_ema is not None else None,
            "loss_history": list(self.loss_history),
            "trained": self.trained,
        }
