"""OpenAI-compatible API gateway — use the fleet from any existing AI tool.

Exposes a localhost endpoint that speaks the OpenAI REST API
(`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`) backed by the Kemi
fleet. Point any OpenAI-compatible client (Cursor, Continue, LangChain, the
`openai` SDK, OpenWebUI, …) at it:

    export OPENAI_BASE_URL=http://127.0.0.1:11434/v1
    export OPENAI_API_KEY=kemi          # any non-empty string

and existing apps run on the fleet — no API key, no account, no rewrite. The
heavy inference happens on provider ships; this gateway just translates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .consumer import Consumer, JobError

log = logging.getLogger("kemi.openai")

MAX_BODY = 8 * 1024 * 1024
REQUEST_DEADLINE = 180.0  # overall cap per request; past it the client gets 504


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Turn OpenAI chat messages into a single prompt for the fleet."""
    lines = []
    for m in messages:
        role = str(m.get("role", "user")).capitalize()
        lines.append(f"{role}: {m.get('content', '')}")
    lines.append("Assistant:")
    return "\n".join(lines)


class OpenAIGateway:
    def __init__(self, node, host: str = "127.0.0.1", port: int = 11434,
                 deadline: float = REQUEST_DEADLINE):
        self.node = node
        self.consumer = Consumer(node)
        self.host = host
        self.port = port
        self.deadline = deadline
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("OpenAI-compatible gateway on http://%s:%s/v1", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    # -- HTTP plumbing -------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            parts = line.decode("latin-1").split()
            if len(parts) < 2:
                writer.close()
                return
            method, path = parts[0], parts[1]
            length = 0
            while True:
                header = await asyncio.wait_for(reader.readline(), timeout=30.0)
                if header in (b"\r\n", b"\n", b""):
                    break
                name, _, value = header.decode("latin-1").partition(":")
                if name.strip().lower() == "content-length":
                    length = min(int(value.strip()), MAX_BODY + 1)
            body = await reader.readexactly(length) if length else b""
            if length > MAX_BODY:
                await self._send(writer, 413, {"error": "body too large"})
                return
            await self._route(writer, method, path.split("?")[0], body)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError,
                ValueError, OSError):
            pass
        except Exception:
            log.exception("gateway request failed")
        finally:
            writer.close()

    async def _route(self, writer, method: str, path: str, body: bytes) -> None:
        if method == "GET" and path == "/v1/models":
            await self._models(writer)
        elif method == "POST" and path == "/v1/chat/completions":
            await self._chat(writer, body)
        elif method == "POST" and path == "/v1/embeddings":
            await self._embeddings(writer, body)
        else:
            await self._send(writer, 404, {"error": {"message": "not found"}})

    async def _models(self, writer) -> None:
        try:
            names = await self.consumer.models("ai.generate")
        except Exception:
            names = []
        if not names:
            names = ["kemi-fleet"]   # always advertise at least a default
        data = [{"id": n, "object": "model", "owned_by": "kemi"} for n in names]
        await self._send(writer, 200, {"object": "list", "data": data})

    async def _chat(self, writer, body: bytes) -> None:
        try:
            req = json.loads(body.decode("utf-8"))
            messages = req["messages"]
            assert isinstance(messages, list)
        except (ValueError, KeyError, AssertionError, UnicodeDecodeError) as exc:
            await self._send(writer, 400, {"error": {"message": f"bad request: {exc}"}})
            return
        prompt = _flatten_messages(messages)
        model = req.get("model")
        if model in (None, "kemi-fleet", "default"):
            model = None
        max_tokens = int(req.get("max_tokens") or 128)
        created = int(time.time())
        cid = f"chatcmpl-{created}"

        if req.get("stream"):
            await self._chat_stream(writer, prompt, model, max_tokens, cid, created)
            return
        try:
            text = await asyncio.wait_for(
                self._generate(prompt, model, max_tokens), timeout=self.deadline)
        except JobError as exc:
            await self._send(writer, 502, {"error": {"message": str(exc)}})
            return
        except asyncio.TimeoutError:
            await self._send(writer, 504, {"error": {"message":
                f"the fleet produced no answer within {self.deadline:.0f}s"}})
            return
        await self._send(writer, 200, {
            "id": cid, "object": "chat.completion", "created": created,
            "model": model or "kemi-fleet",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": len(prompt.split()),
                      "completion_tokens": len(text.split()),
                      "total_tokens": len(prompt.split()) + len(text.split())},
        })

    async def _generate(self, prompt: str, model, max_tokens: int) -> str:
        parts = []
        async for event in self.consumer.stream_generate(
                [prompt], {"max_tokens": max_tokens}, model=model):
            if event.get("done"):
                break
            parts.append(event["token"])
        return "".join(parts).strip()

    async def _chat_stream(self, writer, prompt, model, max_tokens, cid, created) -> None:
        head = ("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                "Cache-Control: no-store\r\nConnection: close\r\n\r\n")
        writer.write(head.encode("latin-1"))
        await writer.drain()

        def sse(obj: dict, finish=None) -> bytes:
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": model or "kemi-fleet",
                     "choices": [{"index": 0, "delta": obj, "finish_reason": finish}]}
            return f"data: {json.dumps(chunk)}\n\n".encode("utf-8")

        try:
            writer.write(sse({"role": "assistant"}))
            async for event in self.consumer.stream_generate(
                    [prompt], {"max_tokens": max_tokens}, model=model):
                if event.get("done"):
                    break
                writer.write(sse({"content": event["token"]}))
                await writer.drain()
            writer.write(sse({}, finish="stop"))
            writer.write(b"data: [DONE]\n\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        except JobError as exc:
            # Headers are already out, so speak the error in-band instead of
            # silently dropping the connection and leaving the client waiting.
            try:
                writer.write(f"data: {json.dumps({'error': {'message': str(exc)}})}"
                             "\n\n".encode("utf-8"))
                writer.write(b"data: [DONE]\n\n")
                await writer.drain()
            except (ConnectionError, OSError):
                pass

    async def _embeddings(self, writer, body: bytes) -> None:
        try:
            req = json.loads(body.decode("utf-8"))
            text_input = req["input"]
            inputs = [text_input] if isinstance(text_input, str) else list(text_input)
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            await self._send(writer, 400, {"error": {"message": f"bad request: {exc}"}})
            return
        model = req.get("model")
        if model in (None, "kemi-fleet", "default"):
            model = None
        try:
            vectors = await asyncio.wait_for(
                self.consumer.run_job_embeddings(inputs, model), timeout=self.deadline)
        except JobError as exc:
            await self._send(writer, 502, {"error": {"message": str(exc)}})
            return
        except asyncio.TimeoutError:
            await self._send(writer, 504, {"error": {"message":
                f"the fleet produced no embeddings within {self.deadline:.0f}s"}})
            return
        data = [{"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(vectors)]
        await self._send(writer, 200, {"object": "list", "data": data,
                                       "model": model or "kemi-fleet"})

    async def _send(self, writer, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
                  413: "Payload Too Large", 502: "Bad Gateway",
                  504: "Gateway Timeout"}.get(status, "OK")
        head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n")
        try:
            writer.write(head.encode("latin-1") + data)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
