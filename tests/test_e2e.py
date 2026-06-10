"""End-to-end tests: a full local swarm (tracker + providers + consumer)."""

import hashlib
import unittest

from kemi.consumer import Consumer, Job, JobError
from kemi.identity import Identity
from kemi.ledger import STARTING_BALANCE
from kemi.provider import ProviderNode
from kemi.tracker import Tracker


class DishonestProvider(ProviderNode):
    """Returns fabricated results (but still demands payment)."""

    async def _on_task_execute(self, message):
        response = await super()._on_task_execute(message)
        if response.get("ok"):
            response["results"] = ["sahte-sonuc"] * len(response["results"])
        return response


class SwarmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tracker = Tracker(host="127.0.0.1", port=0)
        await self.tracker.start()
        self.nodes = []

    async def asyncTearDown(self):
        for node in self.nodes:
            await node.stop()
        await self.tracker.stop()

    async def _start_provider(self, cls=ProviderNode, price=1.0):
        node = cls(
            identity=Identity.create(),
            tracker_host="127.0.0.1",
            tracker_port=self.tracker.port,
            host="127.0.0.1",
            price=price,
        )
        await node.start()
        self.nodes.append(node)
        return node

    async def test_job_distribution_and_payment(self):
        p1 = await self._start_provider(price=0.5)
        p2 = await self._start_provider(price=1.0)
        consumer = Consumer(Identity.create(), "127.0.0.1", self.tracker.port)

        items = [f"parça-{i}" for i in range(20)]
        report = await consumer.run_job(Job(task="hash.sha256", items=items, chunk_size=3))

        expected = [hashlib.sha256(item.encode()).hexdigest() for item in items]
        self.assertEqual(report.results, expected)
        self.assertEqual(report.chunks, 7)

        # Credits moved: consumer paid exactly what providers earned.
        consumer_balance = await consumer.balance()
        self.assertAlmostEqual(consumer_balance, STARTING_BALANCE - report.spent, places=6)
        earned = 0.0
        for node in (p1, p2):
            balance = (await node._tracker_request({"type": "balance"}))["balance"]
            earned += balance - STARTING_BALANCE
        self.assertAlmostEqual(earned, report.spent, places=6)
        self.assertGreater(report.spent, 0)

    async def test_redundancy_defeats_dishonest_provider(self):
        await self._start_provider(price=1.0)
        await self._start_provider(price=1.0)
        await self._start_provider(cls=DishonestProvider, price=0.1)
        consumer = Consumer(Identity.create(), "127.0.0.1", self.tracker.port)

        items = [f"veri-{i}" for i in range(8)]
        report = await consumer.run_job(
            Job(task="hash.sha256", items=items, chunk_size=2, redundancy=2)
        )
        expected = [hashlib.sha256(item.encode()).hexdigest() for item in items]
        self.assertEqual(report.results, expected)

    async def test_survives_dead_provider(self):
        alive = await self._start_provider()
        dead = await self._start_provider()
        # Kill the second provider's server without unregistering it, so the
        # tracker still lists it and the consumer must route around failures.
        dead._server.close()
        await dead._server.wait_closed()

        consumer = Consumer(Identity.create(), "127.0.0.1", self.tracker.port)
        items = [f"x-{i}" for i in range(6)]
        report = await consumer.run_job(Job(task="hash.sha256", items=items, chunk_size=2))
        self.assertEqual(len(report.results), 6)
        self.assertEqual(list(report.providers_used), [alive.identity.node_id])

    async def test_ai_generate_over_swarm(self):
        await self._start_provider()
        consumer = Consumer(Identity.create(), "127.0.0.1", self.tracker.port)
        report = await consumer.run_job(
            Job(task="ai.generate", items=["merhaba dünya"], params={"max_tokens": 8})
        )
        self.assertEqual(len(report.results), 1)
        self.assertEqual(len(report.results[0].split()), 8)

    async def test_no_provider_for_task(self):
        consumer = Consumer(Identity.create(), "127.0.0.1", self.tracker.port)
        with self.assertRaises(JobError):
            await consumer.run_job(Job(task="hash.sha256", items=["a"]))

    async def test_unsupported_task_rejected_by_provider(self):
        await self._start_provider()
        consumer = Consumer(Identity.create(), "127.0.0.1", self.tracker.port)
        with self.assertRaises(JobError):
            await consumer.run_job(Job(task="evil.exec", items=["a"]))

    async def test_provider_listing(self):
        node = await self._start_provider(price=2.5)
        consumer = Consumer(Identity.create(), "127.0.0.1", self.tracker.port)
        providers = await consumer.list_providers()
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0]["node_id"], node.identity.node_id)
        self.assertEqual(providers[0]["price"], 2.5)
        self.assertIn("hash.sha256", providers[0]["tasks"])
        self.assertIn("ai.generate", providers[0]["tasks"])


if __name__ == "__main__":
    unittest.main()
