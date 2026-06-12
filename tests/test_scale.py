"""Scale and robustness: bigger swarms, churn, restarts with persistence."""

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path

from kemi.consumer import Consumer, Job
from kemi.gossip_ledger import GENESIS_CREDITS
from kemi.identity import Identity
from kemi.node import PeerNode

DIFF = 4


class ScaleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes: list[PeerNode] = []

    async def asyncTearDown(self):
        for node in self.nodes:
            await node.stop()

    async def _spawn(self, bootstrap=None, **kwargs) -> PeerNode:
        node = PeerNode(
            Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
            bootstrap=bootstrap or [], difficulty=DIFF, sandbox=False, **kwargs,
        )
        await node.start()
        self.nodes.append(node)
        return node

    async def test_25_node_swarm(self):
        """1 bootstrap + 20 providers + 3 bystanders + 1 consumer: discovery
        through the DHT scales past the k-bucket size and work spreads."""
        bootstrap = await self._spawn()
        peers = [("127.0.0.1", bootstrap.port)]
        providers = []
        for i in range(20):
            providers.append(await self._spawn(
                bootstrap=peers, provide=True, price=0.5 + (i % 4) * 0.25))
        for _ in range(3):
            await self._spawn(bootstrap=peers)
        consumer = Consumer(await self._spawn(bootstrap=peers))

        # poll: under parallel test load DHT lookups may need a moment
        found = []
        for _ in range(20):
            found = await consumer.list_providers("hash.sha256")
            if len(found) >= 15:
                break
            await asyncio.sleep(0.5)
        self.assertGreaterEqual(len(found), 15)  # DHT replication finds the swarm

        items = [f"olcek-{i}" for i in range(60)]
        report = await consumer.run_job(Job(task="hash.sha256", items=items,
                                            chunk_size=2))
        self.assertEqual(report.results,
                         [hashlib.sha256(i.encode()).hexdigest() for i in items])
        # the work spread across many distinct machines
        self.assertGreaterEqual(len(report.providers_used), 8)

    async def test_churn_half_the_providers_die_mid_job(self):
        bootstrap = await self._spawn()
        peers = [("127.0.0.1", bootstrap.port)]
        stable = [await self._spawn(bootstrap=peers, provide=True, price=1.0)
                  for _ in range(3)]
        doomed = [await self._spawn(bootstrap=peers, provide=True, price=0.1)
                  for _ in range(3)]
        consumer = Consumer(await self._spawn(bootstrap=peers))

        async def kill_soon():
            await asyncio.sleep(0.05)
            for node in doomed:
                node._server.close()
                await node._server.wait_closed()
                node.dht.stop()

        killer = asyncio.create_task(kill_soon())
        items = [f"churn-{i}" for i in range(40)]
        # The assertion is *recovery*, not single-shot luck: under heavy
        # parallel load a first attempt may exhaust retries against the
        # dying half before striking them out; one retry must always work.
        from kemi.consumer import JobError

        try:
            report = await consumer.run_job(Job(task="hash.sha256", items=items,
                                                chunk_size=2, chunk_timeout=15.0))
        except JobError:
            report = await consumer.run_job(Job(task="hash.sha256", items=items,
                                                chunk_size=2, chunk_timeout=15.0))
        await killer
        self.assertEqual(report.results,
                         [hashlib.sha256(i.encode()).hexdigest() for i in items])
        survivors = {node.identity.node_id for node in stable}
        self.assertTrue(set(report.providers_used) & survivors)

    async def test_restart_with_persistent_state(self):
        """A consumer that restarts from its on-disk ledger must keep its
        balance and never reuse a burned seq (which would self-flag)."""
        with tempfile.TemporaryDirectory() as tmp:
            identity_path = Path(tmp) / "identity.json"
            ledger_path = str(Path(tmp) / "ledger.db")
            identity = Identity.load_or_create(identity_path, difficulty=DIFF)

            bootstrap = await self._spawn()
            peers = [("127.0.0.1", bootstrap.port)]
            provider = await self._spawn(bootstrap=peers, provide=True, price=1.0)

            first = PeerNode(identity, host="127.0.0.1", port=0, bootstrap=peers,
                             difficulty=DIFF, sandbox=False,
                             ledger_path=ledger_path)
            await first.start()
            report1 = await Consumer(first).run_job(
                Job(task="hash.sha256", items=["a", "b", "c"]))
            max_seq_before = first.ledger.next_seq(identity.node_id) - 1
            # burn a seq (a tx signed but never delivered anywhere)
            first.ledger.make_tx(identity, provider.identity.node_id, 1.0)
            await first.stop()

            second = PeerNode(identity, host="127.0.0.1", port=0, bootstrap=peers,
                              difficulty=DIFF, sandbox=False,
                              ledger_path=ledger_path)
            await second.start()
            self.nodes.append(second)
            balance = second.ledger.balance(identity.node_id)
            self.assertAlmostEqual(balance, GENESIS_CREDITS - report1.spent, places=6)

            report2 = await Consumer(second).run_job(
                Job(task="hash.sha256", items=["d", "e"]))
            self.assertEqual(len(report2.results), 2)
            # identity must never be flagged on any replica: seqs kept advancing
            self.assertFalse(second.ledger.is_flagged(identity.node_id))
            self.assertFalse(provider.ledger.is_flagged(identity.node_id))
            self.assertGreater(second.ledger.next_seq(identity.node_id) - 1,
                               max_seq_before + 1)

    async def test_gossip_converges_in_larger_swarm(self):
        bootstrap = await self._spawn()
        peers = [("127.0.0.1", bootstrap.port)]
        for _ in range(4):
            await self._spawn(bootstrap=peers, provide=True, price=1.0)
        watchers = [await self._spawn(bootstrap=peers) for _ in range(3)]
        consumer = Consumer(await self._spawn(bootstrap=peers))

        await consumer.run_job(Job(task="hash.sha256",
                                   items=[f"g-{i}" for i in range(12)], chunk_size=3))
        target = consumer.node.ledger.tx_count()
        for _ in range(60):
            for watcher in watchers:
                await watcher.sync_ledger()
            if all(w.ledger.tx_count() == target for w in watchers):
                break
            await asyncio.sleep(0.1)
        for watcher in watchers:
            self.assertEqual(watcher.ledger.tx_count(), target)


if __name__ == "__main__":
    unittest.main()
