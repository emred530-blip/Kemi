"""Pluggable AI inference backends for the ``ai.generate`` task.

Providers choose which backend they run. The default ``mock`` backend needs
no dependencies and produces deterministic pseudo-text, which is also what
makes redundancy-based result verification testable. The ``transformers``
backend runs a real Hugging Face text-generation pipeline when the optional
``kemi[ai]`` extras are installed.
"""

from __future__ import annotations

import hashlib
from typing import Protocol


class AIBackend(Protocol):
    name: str

    def generate(self, prompt: str, max_tokens: int = 64) -> str: ...


_WORDS = (
    "veri ag dugum islem guc paylasim kume katman model sorgu yanit "
    "akis blok zincir kredi takas esler arasi hesap kaynak gorev parca"
).split()


class MockBackend:
    """Deterministic stand-in for a real model: same prompt, same output."""

    name = "mock"

    def generate(self, prompt: str, max_tokens: int = 64) -> str:
        seed = hashlib.sha256(prompt.encode("utf-8")).digest()
        words: list[str] = []
        counter = 0
        while len(words) < min(max_tokens, 64):
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            words.extend(_WORDS[b % len(_WORDS)] for b in block[:8])
            counter += 1
        return " ".join(words[:max_tokens])


class TransformersBackend:
    """Real local inference via Hugging Face transformers (optional extra)."""

    name = "transformers"

    def __init__(self, model: str = "distilgpt2"):
        try:
            from transformers import pipeline  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "transformers backend requires `pip install kemi[ai]`"
            ) from exc
        self._pipe = pipeline("text-generation", model=model)

    def generate(self, prompt: str, max_tokens: int = 64) -> str:  # pragma: no cover
        out = self._pipe(prompt, max_new_tokens=max_tokens, num_return_sequences=1)
        return out[0]["generated_text"]


def load_backend(name: str, **kwargs: object) -> AIBackend:
    if name == "mock":
        return MockBackend()
    if name == "transformers":
        return TransformersBackend(**kwargs)  # type: ignore[arg-type]
    raise ValueError(f"unknown AI backend: {name!r} (available: mock, transformers)")
