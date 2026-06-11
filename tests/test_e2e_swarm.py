"""End-to-end tests for encrypted payloads and pipeline jobs over the swarm."""

import asyncio
import hashlib
import json
import unittest
from typing import Any

from kemi.consumer import Consumer, Job, PipelineStage
from kemi.identity import Identity
from kemi.node import PeerNode
from kemi.tasks import run_task

DIFF = 4


class SwarmBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes: list[PeerNode] = []
        self.bootstrap = await self._spawn()
        self.peers = [("127.0.0.1", self.bootstrap.port)]

    async def asyncTearDown(self):
        for node in self.nodes:
            await node.stop()

    async def _spawn(self, cls=PeerNode, **kwargs) -> PeerNode:
        node = cls(
            Identity.create(difficulty=DIFF),
            host="127.0.0.1", port=0,
            bootstrap=getattr(self, "peers", None) or [],
            difficulty=DIFF, sandbox=False,
            **kwargs,
        )
        await node.start()
        self.nodes.append(node)
        return node


class EncryptedSwarmTests(SwarmBase):
    async def test_encrypted_job_end_to_end(self):
        await self._spawn(provide=True, price=1.0)
        consumer = Consumer(await self._spawn())
        items = ["gizli-1", "gizli-2", "gizli-3"]
        report = await consumer.run_job(Job(task="hash.sha256", items=items,
                                            chunk_size=2, encrypt=True))
        self.assertEqual(report.results,
                         [hashlib.sha256(i.encode()).hexdigest() for i in items])
        self.assertEqual(report.encrypted_chunks, report.chunks)

    async def test_plaintext_optout_still_works(self):
        await self._spawn(provide=True, price=1.0)
        consumer = Consumer(await self._spawn())
        report = await consumer.run_job(Job(task="hash.sha256", items=["a"],
                                            encrypt=False))
        self.assertEqual(report.encrypted_chunks, 0)
        self.assertEqual(len(report.results), 1)

    async def test_relay_sees_only_ciphertext(self):
        """Even the relay carrying a NATed provider's traffic learns nothing."""
        relayed = await self._spawn(provide=True, price=1.0, force_relay=True)
        await asyncio.sleep(0.2)

        observed: list[dict[str, Any]] = []
        original_forward = type(self.bootstrap)._on_relay_forward

        async def spying_forward(node, message):
            observed.append(json.loads(json.dumps(message.get("inner", {}))))
            return await original_forward(node, message)

        self.bootstrap._on_relay_forward = spying_forward.__get__(self.bootstrap)

        consumer = Consumer(await self._spawn())
        secret = "çok-gizli-veri-12345"
        report = await consumer.run_job(Job(task="hash.sha256", items=[secret],
                                            encrypt=True))
        self.assertEqual(report.results, [hashlib.sha256(secret.encode()).hexdigest()])
        self.assertGreater(len(observed), 0)
        for inner in observed:
            self.assertNotIn("items", inner)        # no plaintext fields at all
            self.assertIn("enc", inner)
            self.assertNotIn(secret, json.dumps(inner))  # secret never on the wire

    async def test_garbage_ciphertext_is_rejected(self):
        provider = await self._spawn(provide=True, price=1.0)
        consumer_node = await self._spawn()
        tx = consumer_node.ledger.make_tx(consumer_node.identity,
                                          provider.identity.node_id, 5.0)
        from kemi.protocol import request

        response = await request("127.0.0.1", provider.port, {
            "type": "task.execute", "task": "hash.sha256",
            "enc": {"n": "00" * 24, "c": "deadbeef"}, "payment": tx,
        }, timeout=10.0)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "decrypt_failed")


class PipelineTests(SwarmBase):
    async def test_pipeline_matches_local_composition(self):
        await self._spawn(provide=True, price=0.5)
        await self._spawn(provide=True, price=1.0)
        consumer = Consumer(await self._spawn())

        vectors = [[0.1 * i, 0.2, -0.3 * i, 0.4] for i in range(6)]
        stages = [PipelineStage(task="ai.layer", params={"layer": layer}, chunk_size=2)
                  for layer in range(3)]
        report = await consumer.run_pipeline(stages, vectors)

        expected = vectors
        for layer in range(3):
            expected = run_task("ai.layer", expected, {"layer": layer}, {})
        self.assertEqual(report.results, expected)
        self.assertEqual(len(report.stages), 3)
        self.assertGreater(report.spent, 0)
        # every stage travelled encrypted
        for stage_report in report.stages:
            self.assertEqual(stage_report.encrypted_chunks, stage_report.chunks)

    async def test_pipeline_with_redundancy(self):
        await self._spawn(provide=True, price=1.0)
        await self._spawn(provide=True, price=1.0)
        consumer = Consumer(await self._spawn())
        vectors = [[1.0, -1.0]]
        stages = [PipelineStage(task="ai.layer", params={"layer": 0}, redundancy=2)]
        report = await consumer.run_pipeline(stages, vectors)
        self.assertEqual(report.results,
                         run_task("ai.layer", vectors, {"layer": 0}, {}))

    async def test_ai_layer_works_without_backend(self):
        """ai.layer is pure math: providers without an AI backend offer it."""
        node = await self._spawn(provide=True, price=1.0, ai_backend=None)
        self.assertIn("ai.layer", node.supported_tasks)
        self.assertNotIn("ai.generate", node.supported_tasks)


class StatusTests(SwarmBase):
    async def test_node_info_reports_health(self):
        provider = await self._spawn(provide=True, price=2.0)
        from kemi.protocol import request

        info = await request("127.0.0.1", provider.port, {"type": "node.info"},
                             timeout=10.0)
        self.assertTrue(info["ok"])
        self.assertTrue(info["provide"])
        self.assertEqual(info["price"], 2.0)
        self.assertIn("ai.layer", info["tasks"])
        self.assertIn("gpus", info["resources"])


if __name__ == "__main__":
    unittest.main()
