"""v0.14 operations: result cache, metrics, dynamic pricing."""

import asyncio
import json
import unittest
import urllib.request

from kemi.consumer import Consumer, Job
from kemi.identity import Identity
from kemi.node import PeerNode
from kemi.protocol import request
from kemi.webui import WebUI

DIFF = 4


class _CountingNode(PeerNode):
    """Counts how many times the task actually executes (vs. cache hits)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executions = 0

    async def _execute_chunk(self, task, items, params):
        # count only real misses by wrapping the cache-aware parent
        before = self.metrics["cache_hits"]
        out = await super()._execute_chunk(task, items, params)
        if self.metrics["cache_hits"] == before:
            self.executions += 1
        return out


class OpsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes: list[PeerNode] = []
        self.bootstrap = await self._spawn()
        self.peers = [("127.0.0.1", self.bootstrap.port)]

    async def asyncTearDown(self):
        for node in self.nodes:
            await node.stop()

    async def _spawn(self, cls=PeerNode, **kwargs) -> PeerNode:
        node = cls(Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
                   bootstrap=getattr(self, "peers", None) or [],
                   difficulty=DIFF, sandbox=False, **kwargs)
        await node.start()
        self.nodes.append(node)
        return node

    async def test_result_cache_avoids_recompute(self):
        provider = await self._spawn(cls=_CountingNode, provide=True, price=1.0)
        consumer = Consumer(await self._spawn())
        job = Job(task="hash.sha256", items=["x"], chunk_size=1)
        r1 = await consumer.run_job(job)
        r2 = await consumer.run_job(Job(task="hash.sha256", items=["x"], chunk_size=1))
        self.assertEqual(r1.results, r2.results)
        # second identical job hit the cache: only one real execution
        self.assertEqual(provider.executions, 1)
        self.assertEqual(provider.metrics["cache_hits"], 1)
        # but the consumer still paid both times
        self.assertGreater(r2.spent, 0)

    async def test_ai_generate_is_not_cached(self):
        provider = await self._spawn(cls=_CountingNode, provide=True, price=1.0)
        consumer = Consumer(await self._spawn())
        for _ in range(2):
            await consumer.run_job(Job(task="ai.generate", items=["hi"],
                                       params={"max_tokens": 4}, chunk_size=1))
        self.assertEqual(provider.executions, 2)  # never cached

    async def test_metrics_counters_and_node_info(self):
        provider = await self._spawn(provide=True, price=1.0)
        consumer = Consumer(await self._spawn())
        await consumer.run_job(Job(task="hash.sha256", items=["a", "b"], chunk_size=1))
        info = await request("127.0.0.1", provider.port, {"type": "node.info"},
                             timeout=5.0)
        m = info["metrics"]
        self.assertEqual(m["chunks_served"], 2)
        self.assertAlmostEqual(m["credits_earned"], 2.0, places=6)

    async def test_prometheus_endpoint(self):
        provider = await self._spawn(provide=True, price=1.0)
        ui = WebUI(provider, host="127.0.0.1", port=0)
        await ui.start()
        try:
            base = f"http://127.0.0.1:{ui.port}"
            text = await asyncio.to_thread(
                lambda: urllib.request.urlopen(base + "/metrics", timeout=10).read().decode())
            self.assertIn("kemi_balance", text)
            self.assertIn("kemi_chunks_served", text)
            self.assertIn("# TYPE kemi_balance gauge", text)
        finally:
            await ui.stop()

    async def test_dynamic_pricing_surges_under_load(self):
        provider = await self._spawn(provide=True, price=1.0, dynamic_price=True,
                                     max_workers=2)
        self.assertEqual(provider.effective_price(), 1.0)  # idle = base
        provider._active_chunks = 2  # simulate full occupancy
        self.assertEqual(provider.effective_price(), 2.0)  # 2x at full load
        provider._active_chunks = 1
        self.assertEqual(provider.effective_price(), 1.5)

    async def test_dynamic_price_floor_still_accepts_base_payment(self):
        # A surged provider must still accept a payment at the base price
        # (a consumer with a stale cheap record should not be rejected).
        provider = await self._spawn(provide=True, price=1.0, dynamic_price=True,
                                     max_workers=2)
        provider._active_chunks = 2  # advertised would be 2.0
        consumer = Consumer(await self._spawn())
        # consumer pays the base 1.0/item (stale record); must succeed
        report = await consumer.run_job(Job(task="hash.sha256", items=["z"],
                                            chunk_size=1))
        self.assertEqual(len(report.results), 1)


if __name__ == "__main__":
    unittest.main()
