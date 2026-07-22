"""``kemi chat``: a conversational REPL with the fleet's AI.

Each reply streams in live from the cheapest reputable provider, end-to-end
encrypted, and is paid for with a signed micro-transfer. Conversation
history is kept locally and prepended to each prompt so models with real
context (e.g. via the Ollama backend) can hold a conversation; the fleet
itself stays stateless.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from .consumer import Consumer, JobError
from .names import ship_name

HISTORY_CHAR_BUDGET = 6000  # keep prompts bounded for small models


def build_prompt(history: list[tuple[str, str]], user_message: str) -> str:
    """Flatten the transcript into a single prompt, newest-first trimmed."""
    lines: list[str] = []
    for question, answer in history:
        lines.append(f"User: {question}")
        lines.append(f"Assistant: {answer}")
    lines.append(f"User: {user_message}")
    lines.append("Assistant:")
    prompt = "\n".join(lines)
    while len(prompt) > HISTORY_CHAR_BUDGET and history:
        history.pop(0)
        prompt = build_prompt(history, user_message)
    return prompt


async def chat_once(consumer: Consumer, history: list[tuple[str, str]],
                    user_message: str, *, max_tokens: int = 128,
                    on_token=None, model: str | None = None
                    ) -> tuple[str, float, str, str]:
    """One conversational turn. Returns (reply, credits_spent, provider_id,
    model_name) and appends the exchange to ``history``."""
    prompt = build_prompt(history, user_message)
    parts: list[str] = []
    spent = 0.0
    provider = ""
    served_model = ""
    async for event in consumer.stream_generate([prompt],
                                                {"max_tokens": max_tokens},
                                                model=model):
        if event.get("done"):
            spent = float(event.get("spent", 0.0))
            provider = str(event.get("provider", ""))
            served_model = str(event.get("model") or "")
            break
        parts.append(event["token"])
        if on_token is not None:
            on_token(event["token"])
    reply = "".join(parts).strip()
    history.append((user_message, reply))
    return reply, spent, provider, served_model


async def run_chat(consumer: Consumer, *, max_tokens: int = 128,
                   model: str | None = None) -> int:
    """The interactive loop behind ``kemi chat``."""
    providers = [p for p in await consumer.list_providers("ai.generate", model=model)
                 if p.get("stream")]
    if not providers:
        suffix = f" with model {model!r}" if model else ""
        print(f"no streaming AI providers in this fleet{suffix} "
              "(start one with: kemi node --provide --ai-backend ollama ...)",
              file=sys.stderr)
        return 1
    best = providers[0]
    model_note = f", model {best['model']}" if best.get("model") else ""
    print(f"⚓ chatting with the fleet - best ship: {ship_name(best['node_id'])} "
          f"({best['price']:.2f} cr/item, e2e encrypted{model_note})")
    print("  commands: /balance  /clear  /quit\n")

    history: list[tuple[str, str]] = []
    while True:
        try:
            user_message = (await asyncio.to_thread(input, "you ⚓ ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user_message:
            continue
        if user_message in ("/quit", "/exit", "/q"):
            return 0
        if user_message == "/clear":
            history.clear()
            print("  (history cleared)")
            continue
        if user_message == "/balance":
            print(f"  {await consumer.balance():.2f} credits")
            continue
        print("fleet > ", end="", flush=True)
        try:
            _, spent, provider, _ = await chat_once(
                consumer, history, user_message, max_tokens=max_tokens,
                on_token=lambda tok: print(tok, end="", flush=True), model=model)
        except JobError as exc:
            print(f"\n  error: {exc}", file=sys.stderr)
            continue
        print(f"\n  [{spent:.2f} cr via {ship_name(provider)}]\n")
