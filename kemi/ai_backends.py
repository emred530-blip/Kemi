"""Pluggable AI inference backends for the ``ai.generate`` task.

Backends implement ``generate`` (full completion) and optionally ``stream``
(yield tokens as they are produced - the swarm forwards them to the
consumer live). Providers choose what they run:

* ``mock``         - deterministic pseudo-text, zero dependencies. Default;
                     also what makes redundancy voting testable.
* ``ollama``       - a real local model served by Ollama (https://ollama.com),
                     spoken to over its localhost HTTP API with stdlib only.
                     This is the easiest way to put genuine LLM inference on
                     the swarm: install Ollama, ``ollama pull llama3.2``, then
                     ``kemi node --provide --ai-backend ollama --ai-model llama3.2``.
* ``transformers`` - Hugging Face pipeline in-process (``pip install kemi[ai]``),
                     uses the GPU when available.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any, Iterator, Protocol


class AIBackendError(Exception):
    pass


class AIBackend(Protocol):
    name: str
    model: str

    def generate(self, prompt: str, max_tokens: int = 64) -> str: ...

    # Optional: def stream(self, prompt, max_tokens) -> Iterator[str]
    # Optional: def embed(self, text: str) -> list[float]


_WORDS = (
    "veri ag dugum islem guc paylasim kume katman model sorgu yanit "
    "akis blok zincir kredi takas esler arasi hesap kaynak gorev parca"
).split()


EMBED_DIM = 64  # mock embedding width


class MockBackend:
    """Deterministic stand-in for a real model: same prompt, same output."""

    name = "mock"
    model = "mock"

    def __init__(self, token_delay: float = 0.0):
        self.token_delay = token_delay

    def _tokens(self, prompt: str, max_tokens: int) -> list[str]:
        seed = hashlib.sha256(prompt.encode("utf-8")).digest()
        words: list[str] = []
        counter = 0
        while len(words) < max_tokens:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            words.extend(_WORDS[b % len(_WORDS)] for b in block[:8])
            counter += 1
        return words[:max_tokens]

    def generate(self, prompt: str, max_tokens: int = 64) -> str:
        return " ".join(self._tokens(prompt, max_tokens))

    def stream(self, prompt: str, max_tokens: int = 64) -> Iterator[str]:
        for i, word in enumerate(self._tokens(prompt, max_tokens)):
            if self.token_delay:
                time.sleep(self.token_delay)
            yield word if i == 0 else " " + word

    def embed(self, text: str) -> list[float]:
        """Deterministic unit-norm pseudo-embedding (same text, same vector)."""
        vector: list[float] = []
        counter = 0
        while len(vector) < EMBED_DIM:
            block = hashlib.sha256(f"embed:{text}:{counter}".encode("utf-8")).digest()
            vector.extend((b - 127.5) / 127.5 for b in block)
            counter += 1
        vector = vector[:EMBED_DIM]
        norm = sum(v * v for v in vector) ** 0.5 or 1.0
        return [round(v / norm, 9) for v in vector]


class OllamaBackend:
    """Real local LLM inference through an Ollama server's HTTP API."""

    name = "ollama"

    def __init__(self, model: str = "llama3.2", url: str = "http://127.0.0.1:11434",
                 timeout: float = 300.0):
        self.model = model
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _request(self, prompt: str, max_tokens: int, stream: bool):
        body = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": stream,
            "options": {"num_predict": max_tokens},
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}/api/generate", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except (urllib.error.URLError, OSError) as exc:
            raise AIBackendError(
                f"ollama unreachable at {self.url} ({exc}); is `ollama serve` running?"
            )

    def stream(self, prompt: str, max_tokens: int = 64) -> Iterator[str]:
        with self._request(prompt, max_tokens, stream=True) as response:
            for line in response:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    raise AIBackendError(f"ollama: {event['error']}")
                token = event.get("response", "")
                if token:
                    yield token
                if event.get("done"):
                    return

    def generate(self, prompt: str, max_tokens: int = 64) -> str:
        return "".join(self.stream(prompt, max_tokens))

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}/api/embed", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise AIBackendError(f"ollama embed failed: {exc}")
        embeddings = payload.get("embeddings")
        if not embeddings or not isinstance(embeddings, list):
            raise AIBackendError(f"ollama returned no embedding for model {self.model!r}")
        return [float(x) for x in embeddings[0]]


class TransformersBackend:
    """Hugging Face pipeline in-process (optional ``kemi[ai]`` extra)."""

    name = "transformers"

    def __init__(self, model: str = "distilgpt2", device: str | None = None):
        try:
            from transformers import pipeline  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise AIBackendError(
                "transformers backend requires `pip install kemi[ai]`"
            ) from exc
        kwargs: dict[str, Any] = {}
        if device is not None:
            kwargs["device"] = device
        self._pipe = pipeline("text-generation", model=model, **kwargs)

    def generate(self, prompt: str, max_tokens: int = 64) -> str:  # pragma: no cover
        out = self._pipe(prompt, max_new_tokens=max_tokens, num_return_sequences=1)
        return out[0]["generated_text"]


def load_backend(name: str, **kwargs: Any) -> AIBackend:
    backends = {"mock": MockBackend, "ollama": OllamaBackend,
                "transformers": TransformersBackend}
    if name not in backends:
        raise ValueError(
            f"unknown AI backend: {name!r} (available: {', '.join(backends)})")
    return backends[name](**kwargs)


def supports_streaming(backend: AIBackend | None) -> bool:
    return backend is not None and callable(getattr(backend, "stream", None))


def supports_embedding(backend: AIBackend | None) -> bool:
    return backend is not None and callable(getattr(backend, "embed", None))


def backend_model(backend: AIBackend | None) -> str | None:
    return getattr(backend, "model", None) if backend is not None else None
