"""v0.10 hardening: rate limits, connection caps, ledger checkpoints."""

import asyncio
import unittest

from kemi.crypto import digest
from kemi.gossip_ledger import GENESIS_CREDITS, GossipLedger
from kemi.identity import Identity
from kemi.node import PeerNode
from kemi.protocol import request
from kemi.ratelimit import IPGuard, Limits, TokenBucket

DIFF = 4


class TokenBucketTests(unittest.TestCase):
    def test_burst_then_block(self):
        bucket = TokenBucket(rate=1000.0, burst=3.0)
        self.assertTrue(all(bucket.take() for _ in range(3)))
        self.assertFalse(bucket.take())

    def test_refill(self):
        import time

        bucket = TokenBucket(rate=1000.0, burst=2.0)
        bucket.take(); bucket.take()
        self.assertFalse(bucket.take())
        time.sleep(0.01)  # 1000/s refills fast
        self.assertTrue(bucket.take())


class IPGuardTests(unittest.TestCase):
    def test_concurrency_accounting(self):
        guard = IPGuard(rate=100, burst=100, max_concurrent=2)
        self.assertTrue(guard.try_connect("1.1.1.1"))
        self.assertTrue(guard.try_connect("1.1.1.1"))
        self.assertFalse(guard.try_connect("1.1.1.1"))
        self.assertTrue(guard.try_connect("2.2.2.2"))  # other IPs unaffected
        guard.disconnect("1.1.1.1")
        self.assertTrue(guard.try_connect("1.1.1.1"))

    def test_eviction_keeps_active_connections(self):
        guard = IPGuard(rate=100, burst=100, max_concurrent=4, max_tracked=4)
        guard.try_connect("9.9.9.9")  # active: must survive eviction
        for i in range(10):
            guard.allow_request(f"10.0.0.{i}")
        self.assertEqual(guard.connections("9.9.9.9"), 1)


class DosProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        for node in getattr(self, "nodes", []):
            await node.stop()

    async def _spawn(self, **kwargs) -> PeerNode:
        self.nodes = getattr(self, "nodes", [])
        node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1",
                        port=0, difficulty=DIFF, sandbox=False, **kwargs)
        await node.start()
        self.nodes.append(node)
        return node

    async def test_request_rate_limit(self):
        node = await self._spawn(limits=Limits(per_ip_rate=0.1, per_ip_burst=3))
        allowed, limited = 0, 0
        for _ in range(8):
            try:
                response = await request("127.0.0.1", node.port,
                                         {"type": "node.info"}, timeout=5.0)
                if response.get("ok"):
                    allowed += 1
                elif response.get("error") == "rate_limited":
                    limited += 1
            except Exception:
                limited += 1
        self.assertLessEqual(allowed, 3)
        self.assertGreaterEqual(limited, 5)

    async def test_per_ip_connection_cap(self):
        node = await self._spawn(limits=Limits(per_ip_connections=2,
                                               per_ip_rate=1000, per_ip_burst=1000))
        # hold two connections open without sending anything
        holders = [await asyncio.open_connection("127.0.0.1", node.port)
                   for _ in range(2)]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", node.port)
            data = await asyncio.wait_for(reader.read(64), timeout=5.0)
            self.assertEqual(data, b"")  # third connection: shed immediately
            writer.close()
        finally:
            for _, writer in holders:
                writer.close()

    async def test_relay_session_cap(self):
        relay = await self._spawn(limits=Limits(max_relay_sessions=1,
                                                per_ip_rate=1000, per_ip_burst=1000))
        peers = [("127.0.0.1", relay.port)]
        first = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
                         bootstrap=peers, difficulty=DIFF, sandbox=False,
                         provide=True, force_relay=True)
        await first.start()
        self.nodes.append(first)
        await asyncio.sleep(0.3)
        self.assertEqual(len(relay._relay_sessions), 1)

        second = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
                          bootstrap=peers, difficulty=DIFF, sandbox=False,
                          provide=True, force_relay=True)
        await second.start()
        self.nodes.append(second)
        await asyncio.sleep(0.5)
        self.assertEqual(len(relay._relay_sessions), 1)  # capacity respected


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.alice = Identity.create(DIFF)
        self.bob = Identity.create(DIFF)

    def _ledger(self) -> GossipLedger:
        ledger = GossipLedger(":memory:", difficulty=DIFF)
        self.addCleanup(ledger.close)
        return ledger

    def test_snapshot_hash_is_order_independent(self):
        a, b = self._ledger(), self._ledger()
        txs = [a.make_tx(self.alice, self.bob.node_id, amount)
               for amount in (5.0, 7.5, 2.25)]
        for tx in txs:
            a.add_tx(tx)
        for tx in reversed(txs):
            b.add_tx(tx)
        self.assertEqual(digest(a.snapshot()), digest(b.snapshot()))

    def test_prune_preserves_state_and_blocks_replay(self):
        ledger = self._ledger()
        txs = [ledger.make_tx(self.alice, self.bob.node_id, amount)
               for amount in (10.0, 20.0)]
        for tx in txs:
            ledger.add_tx(tx)
        balance_before = ledger.balance(self.alice.node_id)
        earned_before = ledger.total_earned(self.bob.node_id)
        seq_before = ledger.next_seq(self.alice.node_id)

        pruned = ledger.prune()
        self.assertEqual(pruned, 2)
        self.assertEqual(ledger.tx_count(), 0)
        self.assertEqual(ledger.balance(self.alice.node_id), balance_before)
        self.assertEqual(ledger.total_earned(self.bob.node_id), earned_before)
        self.assertEqual(ledger.next_seq(self.alice.node_id), seq_before)
        # replaying a pre-checkpoint transfer must not double-count
        self.assertEqual(ledger.add_tx(txs[0]), "stale")
        self.assertEqual(ledger.balance(self.alice.node_id), balance_before)
        # spending continues seamlessly after the checkpoint
        ledger.add_tx(ledger.make_tx(self.alice, self.bob.node_id, 1.0))
        self.assertEqual(ledger.balance(self.alice.node_id), balance_before - 1.0)

    def test_prune_preserves_flags(self):
        from kemi.crypto import sign_envelope

        ledger = self._ledger()
        tx1 = ledger.make_tx(self.alice, self.bob.node_id, 5.0)
        tx2 = sign_envelope(self.alice.key,
                            {**tx1["payload"], "to": Identity.create(DIFF).node_id})
        ledger.add_tx(tx1)
        ledger.add_tx(tx2)
        self.assertTrue(ledger.is_flagged(self.alice.node_id))
        ledger.prune()
        self.assertTrue(ledger.is_flagged(self.alice.node_id))

    def test_adopt_snapshot(self):
        source = self._ledger()
        for amount in (10.0, 5.0):
            source.add_tx(source.make_tx(self.alice, self.bob.node_id, amount))
        snap = source.snapshot()

        fresh = self._ledger()
        fresh.adopt_snapshot(snap)
        self.assertEqual(fresh.balance(self.alice.node_id),
                         source.balance(self.alice.node_id))
        self.assertEqual(fresh.total_earned(self.bob.node_id), 15.0)
        # adopting into a used ledger is refused
        with self.assertRaises(ValueError):
            source.adopt_snapshot(snap)


class FastBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        for node in getattr(self, "nodes", []):
            await node.stop()

    async def _spawn(self, bootstrap=None, **kwargs) -> PeerNode:
        self.nodes = getattr(self, "nodes", [])
        node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
                        bootstrap=bootstrap or [], difficulty=DIFF,
                        sandbox=False, **kwargs)
        await node.start()
        self.nodes.append(node)
        return node

    async def test_new_ship_adopts_quorum_snapshot(self):
        from kemi.consumer import Consumer, Job

        a = await self._spawn()
        peers_a = [("127.0.0.1", a.port)]
        b = await self._spawn(bootstrap=peers_a)
        provider = await self._spawn(bootstrap=peers_a, provide=True, price=1.0)
        consumer = Consumer(await self._spawn(bootstrap=peers_a))
        await consumer.run_job(Job(task="hash.sha256",
                                   items=[f"c-{i}" for i in range(6)], chunk_size=2))
        # let a and b converge so their snapshot hashes agree
        for _ in range(60):
            await a.sync_ledger()
            await b.sync_ledger()
            if (a.ledger.tx_count() == b.ledger.tx_count()
                    == consumer.node.ledger.tx_count()):
                break
            await asyncio.sleep(0.1)
        # both sources prune: history is gone, only snapshots remain
        a.ledger.prune()
        b.ledger.prune()
        self.assertEqual(digest(a.ledger.snapshot()), digest(b.ledger.snapshot()))

        newcomer = await self._spawn(
            bootstrap=[("127.0.0.1", a.port), ("127.0.0.1", b.port)])
        self.assertTrue(newcomer.ledger.has_base())
        self.assertEqual(newcomer.ledger.balance(provider.identity.node_id),
                         a.ledger.balance(provider.identity.node_id))
        # and the adopted ledger is fully usable: pay for a fresh job
        report = await Consumer(newcomer).run_job(
            Job(task="hash.sha256", items=["x", "y"], chunk_size=1))
        self.assertEqual(len(report.results), 2)
        self.assertEqual(newcomer.ledger.balance(newcomer.identity.node_id),
                         GENESIS_CREDITS - report.spent)


if __name__ == "__main__":
    unittest.main()
