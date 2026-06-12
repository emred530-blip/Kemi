"""Live token streaming over the swarm."""

import asyncio
import unittest
from typing import Any

from kemi.ai_backends import MockBackend
from kemi.consumer import Consumer, JobError
from kemi.gossip_ledger import GENESIS_CREDITS
from kemi.identity import Identity
from kemi.node import PeerNode

DIFF = 4


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes: list[PeerNode] = []
        self.bootstrap = await self._spawn()
        self.peers = [("127.0.0.1", self.bootstrap.port)]

    async def asyncTearDown(self):
        for node in self.nodes:
            await node.stop()

    async def _spawn(self, **kwargs) -> PeerNode:
        node = PeerNode(
            Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
            bootstrap=getattr(self, "peers", None) or [],
            difficulty=DIFF, sandbox=False, **kwargs,
        )
        await node.start()
        self.nodes.append(node)
        return node

    async def _collect(self, consumer: Consumer, prompts: list[str],
                       params: dict[str, Any]):
        tokens: dict[int, str] = {}
        summary = None
        async for event in consumer.stream_generate(prompts, params):
            if event.get("done"):
                summary = event
                break
            tokens[event["item"]] = tokens.get(event["item"], "") + event["token"]
        return tokens, summary

    async def test_stream_matches_batch_output_and_pays(self):
        provider = await self._spawn(provide=True, price=1.0)
        consumer = Consumer(await self._spawn())

        prompts = ["akış testi", "ikinci istem"]
        tokens, summary = await self._collect(consumer, prompts, {"max_tokens": 6})

        backend = MockBackend()
        for idx, prompt in enumerate(prompts):
            self.assertEqual(tokens[idx], backend.generate(prompt, max_tokens=6))
        self.assertIsNotNone(summary)
        self.assertEqual(summary["results"], [tokens[0], tokens[1]])
        self.assertEqual(summary["spent"], 2.0)

        my_id = consumer.node.identity.node_id
        self.assertEqual(consumer.node.ledger.balance(my_id), GENESIS_CREDITS - 2.0)
        self.assertEqual(consumer.node.ledger.balance(provider.identity.node_id),
                         GENESIS_CREDITS + 2.0)

    async def test_stream_is_encrypted_by_default(self):
        """Sniff the provider's socket traffic: prompts and tokens must not
        appear in cleartext anywhere on the wire."""
        provider = await self._spawn(provide=True, price=1.0)
        consumer = Consumer(await self._spawn())

        secret_prompt = "gizli-istem-987"
        seen: list[bytes] = []
        original = type(provider)._serve_task_stream

        async def spying(node, message, writer):
            import json as _json

            seen.append(_json.dumps(message, ensure_ascii=False).encode())
            await original(node, message, writer)

        provider._serve_task_stream = spying.__get__(provider)
        tokens, summary = await self._collect(consumer, [secret_prompt],
                                              {"max_tokens": 4})
        self.assertTrue(tokens[0])
        self.assertGreater(len(seen), 0)
        for blob in seen:
            self.assertNotIn(secret_prompt.encode(), blob)

    async def test_streaming_through_relay(self):
        """A NATed provider's tokens flow live through its relay peer."""
        relayed = await self._spawn(provide=True, price=1.0, force_relay=True)
        await asyncio.sleep(0.2)
        self.assertIn(relayed.identity.node_id, self.bootstrap._relay_sessions)

        consumer = Consumer(await self._spawn())
        prompt = "relay üzerinden akış"
        tokens, summary = await self._collect(consumer, [prompt], {"max_tokens": 5})
        self.assertEqual(tokens[0], MockBackend().generate(prompt, max_tokens=5))
        self.assertIsNotNone(summary)
        self.assertEqual(summary["provider"], relayed.identity.node_id)
        self.assertEqual(summary["spent"], 1.0)

    async def test_no_backend_no_stream(self):
        await self._spawn(provide=True, price=1.0, ai_backend=None)
        consumer = Consumer(await self._spawn())
        with self.assertRaises(JobError):
            async for _ in consumer.stream_generate(["selam"]):
                pass

    async def test_underpayment_is_rejected_before_tokens(self):
        provider = await self._spawn(provide=True, price=5.0)
        consumer_node = await self._spawn()
        from kemi.protocol import stream_request

        tx = consumer_node.ledger.make_tx(consumer_node.identity,
                                          provider.identity.node_id, 0.01)
        events = []
        async for msg in stream_request("127.0.0.1", provider.port, {
            "type": "task.stream", "task": "ai.generate",
            "items": ["selam"], "params": {}, "payment": tx,
        }, timeout=10.0):
            events.append(msg)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].get("ok", False))
        self.assertEqual(events[0]["error"], "payment_rejected")


if __name__ == "__main__":
    unittest.main()
