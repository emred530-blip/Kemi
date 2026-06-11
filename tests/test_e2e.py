"""End-to-end tests of the decentralised swarm: DHT discovery, signed
payments over the gossip ledger, redundancy voting, NAT relay - no tracker."""

import asyncio
import hashlib
import unittest
from typing import Any

from kemi.consumer import Consumer, Job, JobError
from kemi.gossip_ledger import GENESIS_CREDITS
from kemi.identity import Identity
from kemi.node import PeerNode

DIFF = 4  # low identity proof-of-work keeps tests fast


class DishonestNode(PeerNode):
    """Returns fabricated results (but still takes the payment)."""

    async def _execute_chunk(self, task: str, items: list[Any], params: dict) -> list[Any]:
        return ["sahte-sonuc"] * len(items)


class SwarmTests(unittest.IsolatedAsyncioTestCase):
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
            host="127.0.0.1",
            port=0,
            bootstrap=getattr(self, "peers", None) or [],
            difficulty=DIFF,
            sandbox=False,  # in-process execution keeps the test suite quick
            **kwargs,
        )
        await node.start()
        self.nodes.append(node)
        return node

    async def _consumer(self) -> Consumer:
        node = await self._spawn()
        return Consumer(node)

    async def test_job_distribution_and_signed_payments(self):
        p1 = await self._spawn(provide=True, price=0.5)
        p2 = await self._spawn(provide=True, price=1.0)
        consumer = await self._consumer()

        items = [f"parça-{i}" for i in range(20)]
        report = await consumer.run_job(Job(task="hash.sha256", items=items, chunk_size=3))

        expected = [hashlib.sha256(item.encode()).hexdigest() for item in items]
        self.assertEqual(report.results, expected)
        self.assertEqual(report.chunks, 7)
        self.assertGreater(report.spent, 0)

        # Conservation of credits on the consumer's own replica.
        my_id = consumer.node.identity.node_id
        self.assertAlmostEqual(consumer.node.ledger.balance(my_id),
                               GENESIS_CREDITS - report.spent, places=6)
        earned = sum(consumer.node.ledger.balance(p.identity.node_id) - GENESIS_CREDITS
                     for p in (p1, p2))
        self.assertAlmostEqual(earned, report.spent, places=6)

    async def test_gossip_convergence_across_replicas(self):
        provider = await self._spawn(provide=True, price=1.0)
        consumer = await self._consumer()
        report = await consumer.run_job(Job(task="hash.sha256",
                                            items=[f"x-{i}" for i in range(6)],
                                            chunk_size=2))
        # A peer that took part in nothing must still converge to the same
        # balances via gossip/anti-entropy.
        for _ in range(40):
            await self.bootstrap.sync_ledger()
            if self.bootstrap.ledger.tx_count() == consumer.node.ledger.tx_count():
                break
            await asyncio.sleep(0.1)
        self.assertEqual(self.bootstrap.ledger.balance(provider.identity.node_id),
                         GENESIS_CREDITS + report.spent)
        self.assertEqual(self.bootstrap.ledger.balance(consumer.node.identity.node_id),
                         GENESIS_CREDITS - report.spent)

    async def test_redundancy_defeats_dishonest_provider(self):
        await self._spawn(provide=True, price=1.0)
        await self._spawn(provide=True, price=1.0)
        liar = await self._spawn(cls=DishonestNode, provide=True, price=0.1)
        consumer = await self._consumer()

        items = [f"veri-{i}" for i in range(8)]
        report = await consumer.run_job(
            Job(task="hash.sha256", items=items, chunk_size=2, redundancy=2)
        )
        expected = [hashlib.sha256(item.encode()).hexdigest() for item in items]
        self.assertEqual(report.results, expected)
        # The liar's mismatches were recorded against it locally.
        self.assertLess(consumer.node.reputation.score(liar.identity.node_id), 0.5)

    async def test_banned_provider_is_excluded_from_scheduling(self):
        good = await self._spawn(provide=True, price=1.0)
        bad = await self._spawn(provide=True, price=0.1)
        consumer = await self._consumer()
        for _ in range(3):
            consumer.node.reputation.record(bad.identity.node_id, "mismatch")
        providers = await consumer.list_providers("hash.sha256")
        ids = [p["node_id"] for p in providers]
        self.assertIn(good.identity.node_id, ids)
        self.assertNotIn(bad.identity.node_id, ids)

    async def test_survives_dead_provider(self):
        alive = await self._spawn(provide=True, price=1.0)
        dead = await self._spawn(provide=True, price=0.5)
        # Kill the TCP service abruptly; its DHT record lingers, so the
        # consumer must route around the failures.
        dead._server.close()
        await dead._server.wait_closed()

        consumer = await self._consumer()
        items = [f"x-{i}" for i in range(6)]
        report = await consumer.run_job(Job(task="hash.sha256", items=items, chunk_size=2))
        self.assertEqual(len(report.results), 6)
        self.assertEqual(list(report.providers_used), [alive.identity.node_id])

    async def test_provider_behind_nat_via_relay(self):
        relayed = await self._spawn(provide=True, price=1.0, force_relay=True)
        await asyncio.sleep(0.2)  # let the relay session establish
        self.assertIn(relayed.identity.node_id, self.bootstrap._relay_sessions)

        consumer = await self._consumer()
        providers = await consumer.list_providers("hash.sha256")
        self.assertEqual(len(providers), 1)
        self.assertIsNotNone(providers[0]["relay"])

        items = ["a", "b", "c"]
        report = await consumer.run_job(Job(task="hash.sha256", items=items, chunk_size=2))
        self.assertEqual(report.results,
                         [hashlib.sha256(i.encode()).hexdigest() for i in items])

    async def test_provider_rejects_bad_payments(self):
        provider = await self._spawn(provide=True, price=1.0)
        consumer = await self._consumer()
        rich_ledger = consumer.node.ledger

        from kemi.consumer import send_to_provider

        record = (await consumer.list_providers("hash.sha256"))[0]

        # underpayment
        cheap_tx = rich_ledger.make_tx(consumer.node.identity, provider.identity.node_id, 0.01)
        response = await send_to_provider(record, {
            "type": "task.execute", "task": "hash.sha256",
            "items": ["a", "b"], "params": {}, "payment": cheap_tx,
        }, timeout=10.0)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "payment_rejected")

        # payment addressed to someone else
        stranger = Identity.create(difficulty=DIFF)
        misdirected = rich_ledger.make_tx(consumer.node.identity, stranger.node_id, 5.0)
        response = await send_to_provider(record, {
            "type": "task.execute", "task": "hash.sha256",
            "items": ["a"], "params": {}, "payment": misdirected,
        }, timeout=10.0)
        self.assertFalse(response["ok"])

        # no payment at all
        response = await send_to_provider(record, {
            "type": "task.execute", "task": "hash.sha256",
            "items": ["a"], "params": {},
        }, timeout=10.0)
        self.assertFalse(response["ok"])

    async def test_ai_generate_over_swarm(self):
        await self._spawn(provide=True, price=1.0)
        consumer = await self._consumer()
        report = await consumer.run_job(
            Job(task="ai.generate", items=["merhaba dünya"], params={"max_tokens": 8})
        )
        self.assertEqual(len(report.results), 1)
        self.assertEqual(len(report.results[0].split()), 8)

    async def test_no_provider_for_task(self):
        consumer = await self._consumer()
        with self.assertRaises(JobError):
            await consumer.run_job(Job(task="hash.sha256", items=["a"]))

    async def test_sandboxed_provider_end_to_end(self):
        node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
                        bootstrap=self.peers, difficulty=DIFF,
                        provide=True, price=1.0, sandbox=True)
        await node.start()
        self.nodes.append(node)
        consumer = await self._consumer()
        items = ["sandbox-a", "sandbox-b"]
        report = await consumer.run_job(Job(task="hash.sha256", items=items))
        self.assertEqual(report.results,
                         [hashlib.sha256(i.encode()).hexdigest() for i in items])


if __name__ == "__main__":
    unittest.main()
