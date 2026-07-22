"""OpenAI-compatible gateway: drop-in /v1 API backed by the fleet."""

import asyncio
import json
import time
import unittest
import urllib.error
import urllib.request

from kemi.identity import Identity
from kemi.node import PeerNode
from kemi.openai_gateway import OpenAIGateway, _flatten_messages

DIFF = 4


def _post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer kemi"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read())


def _get(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.status, json.loads(r.read())


class FlattenTests(unittest.TestCase):
    def test_flatten_ends_with_assistant(self):
        p = _flatten_messages([{"role": "user", "content": "hello"}])
        self.assertTrue(p.endswith("Assistant:"))
        self.assertIn("User: hello", p)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes: list[PeerNode] = []
        self.bootstrap = await self._spawn()
        self.peers = [("127.0.0.1", self.bootstrap.port)]
        await self._spawn(provide=True, price=1.0)
        self.client = await self._spawn()
        self.gw = OpenAIGateway(self.client, host="127.0.0.1", port=0)
        await self.gw.start()
        self.base = f"http://127.0.0.1:{self.gw.port}"

    async def asyncTearDown(self):
        await self.gw.stop()
        for node in self.nodes:
            await node.stop()

    async def _spawn(self, **kwargs) -> PeerNode:
        node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
                        bootstrap=getattr(self, "peers", None) or [],
                        difficulty=DIFF, sandbox=False, **kwargs)
        await node.start()
        self.nodes.append(node)
        return node

    async def test_models_endpoint(self):
        status, body = await asyncio.to_thread(_get, self.base + "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(body["object"], "list")
        self.assertTrue(body["data"])

    async def test_chat_completions_openai_shape(self):
        status, body = await asyncio.to_thread(
            _post, self.base + "/v1/chat/completions",
            {"model": "kemi-fleet",
             "messages": [{"role": "user", "content": "hello fleet"}],
             "max_tokens": 8})
        self.assertEqual(status, 200)
        self.assertEqual(body["object"], "chat.completion")
        msg = body["choices"][0]["message"]
        self.assertEqual(msg["role"], "assistant")
        self.assertTrue(msg["content"])
        self.assertIn("usage", body)

    async def test_chat_streaming_sse(self):
        # raw SSE: collect data: lines and confirm a [DONE] terminator
        def stream():
            req = urllib.request.Request(
                self.base + "/v1/chat/completions",
                data=json.dumps({"messages": [{"role": "user", "content": "hi"}],
                                 "max_tokens": 6, "stream": True}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode()

        text = await asyncio.to_thread(stream)
        self.assertIn("data:", text)
        self.assertIn("[DONE]", text)
        self.assertIn("chat.completion.chunk", text)

    async def test_embeddings_endpoint(self):
        status, body = await asyncio.to_thread(
            _post, self.base + "/v1/embeddings",
            {"model": "kemi-fleet", "input": ["alpha", "beta"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(body["data"][0]["object"], "embedding")
        self.assertTrue(body["data"][0]["embedding"])

    async def test_bad_request(self):
        try:
            status, _ = await asyncio.to_thread(
                _post, self.base + "/v1/chat/completions", {"no": "messages"})
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 400)


class GatewayDeadlineTests(unittest.IsolatedAsyncioTestCase):
    """A fleet that cannot answer must say so, never hang the client."""

    async def asyncSetUp(self):
        self.node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1",
                             port=0, bootstrap=[], difficulty=DIFF, sandbox=False)
        await self.node.start()
        self.gw = OpenAIGateway(self.node, host="127.0.0.1", port=0, deadline=0.5)
        await self.gw.start()
        self.base = f"http://127.0.0.1:{self.gw.port}"

    async def asyncTearDown(self):
        await self.gw.stop()
        await self.node.stop()

    async def test_stuck_fleet_returns_504_not_a_hang(self):
        async def stuck(prompt, model, max_tokens):
            await asyncio.sleep(30)

        self.gw._generate = stuck
        started = time.monotonic()
        try:
            status, _ = await asyncio.to_thread(
                _post, self.base + "/v1/chat/completions",
                {"messages": [{"role": "user", "content": "hi"}]})
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 504)
        self.assertLess(time.monotonic() - started, 10)

    async def test_streaming_error_is_spoken_in_band(self):
        # No providers in this fleet: the SSE stream must carry the error
        # and a [DONE] instead of silently dropping the connection.
        def stream():
            req = urllib.request.Request(
                self.base + "/v1/chat/completions",
                data=json.dumps({"messages": [{"role": "user", "content": "hi"}],
                                 "stream": True}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode()

        text = await asyncio.to_thread(stream)
        self.assertIn('"error"', text)
        self.assertIn("[DONE]", text)


if __name__ == "__main__":
    unittest.main()
