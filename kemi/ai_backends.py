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

    def generate(self, prompt: str, max_tokens: int = 64) -> str: ...

    # Optional: def stream(self, prompt, max_tokens) -> Iterator[str]


_WORDS = (
    "veri ag dugum islem guc paylasim kume katman model sorgu yanit "
    "akis blok zincir kredi takas esler arasi hesap kaynak gorev parca"
).split()


class MockBackend:
    """Deterministic stand-in for a real model: same prompt, same output."""

    name = "mock"

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
