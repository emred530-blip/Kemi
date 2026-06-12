"""Witness committee: instant double-spend prevention (not just detection).

Before accepting a payment, a provider consults the nodes DHT-closest to
the sender's witness key. The first transfer for a (sender, seq) gets
locked and co-signed; a conflicting one gets vetoed with evidence. Racing
two providers therefore funds at most one of them.
"""

import asyncio
import unittest

from kemi.consumer import Consumer, Job
from kemi.crypto import sign_envelope
from kemi.identity import Identity
from kemi.node import PeerNode
from kemi.protocol import request

DIFF = 4


class WitnessTests(unittest.IsolatedAsyncioTestCase):
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

    def _conflicting_payments(self, consumer_node: PeerNode,
                              to_a: str, to_b: str, amount: float):
        """Two transfers deliberately signed with the same seq."""
        tx_a = consumer_node.ledger.make_tx(consumer_node.identity, to_a, amount)
        payload_b = {**tx_a["payload"], "to": to_b}
        tx_b = sign_envelope(consumer_node.identity.key, payload_b)
        return tx_a, tx_b

    async def test_double_spend_race_funds_at_most_one_provider(self):
        # extra peers so the witness committee actually exists
        for _ in range(3):
            await self._spawn()
        p1 = await self._spawn(provide=True, price=1.0)
        p2 = await self._spawn(provide=True, price=1.0)
        attacker = await self._spawn()

        tx1, tx2 = self._conflicting_payments(
            attacker, p1.identity.node_id, p2.identity.node_id, 2.0)

        async def hit(provider: PeerNode, tx):
            return await request("127.0.0.1", provider.port, {
                "type": "task.execute", "task": "hash.sha256",
                "items": ["a", "b"], "params": {}, "payment": tx,
            }, timeout=15.0)

        r1, r2 = await asyncio.gather(hit(p1, tx1), hit(p2, tx2))
        succeeded = [r for r in (r1, r2) if r.get("ok")]
        self.assertLessEqual(len(succeeded), 1,
                             "double-spend funded both providers!")
        rejected = [r for r in (r1, r2) if not r.get("ok")]
        self.assertGreaterEqual(len(rejected), 1)
        for r in rejected:
            self.assertEqual(r["error"], "payment_rejected")

    async def test_sequential_double_spend_is_vetoed_with_evidence(self):
        for _ in range(2):
            await self._spawn()
        p1 = await self._spawn(provide=True, price=1.0)
        p2 = await self._spawn(provide=True, price=1.0)
        attacker = await self._spawn()
        tx1, tx2 = self._conflicting_payments(
            attacker, p1.identity.node_id, p2.identity.node_id, 2.0)

        first = await request("127.0.0.1", p1.port, {
            "type": "task.execute", "task": "hash.sha256",
            "items": ["a", "b"], "params": {}, "payment": tx1}, timeout=15.0)
        self.assertTrue(first.get("ok"))

        second = await request("127.0.0.1", p2.port, {
            "type": "task.execute", "task": "hash.sha256",
            "items": ["a", "b"], "params": {}, "payment": tx2}, timeout=15.0)
        self.assertFalse(second.get("ok"))
        # the second provider received objective evidence and flagged the account
        self.assertTrue(p2.ledger.is_flagged(attacker.identity.node_id))
        self.assertTrue(p2.reputation.is_banned(attacker.identity.node_id))

    async def test_witness_receipt_roundtrip(self):
        witness = await self._spawn()
        payer = await self._spawn()
        payee = Identity.create(difficulty=DIFF)
        tx = payer.ledger.make_tx(payer.identity, payee.node_id, 1.0)

        response = await request("127.0.0.1", witness.port,
                                 {"type": "tx.witness", "tx": tx}, timeout=10.0)
        self.assertTrue(response["ok"])
        from kemi.crypto import digest, open_envelope

        receipt = open_envelope(response["receipt"])
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["tx_id"], digest(tx["payload"]))
        self.assertEqual(receipt["witness"], witness.identity.node_id)
        # asking again (idempotent retry) still yields a receipt
        again = await request("127.0.0.1", witness.port,
                              {"type": "tx.witness", "tx": tx}, timeout=10.0)
        self.assertTrue(again["ok"])

    async def test_honest_jobs_unaffected_by_witnessing(self):
        for _ in range(2):
            await self._spawn()
        await self._spawn(provide=True, price=0.5)
        await self._spawn(provide=True, price=1.0)
        consumer = Consumer(await self._spawn())
        report = await consumer.run_job(Job(
            task="hash.sha256", items=[f"w-{i}" for i in range(10)],
            chunk_size=2, redundancy=2))
        self.assertEqual(len(report.results), 10)


if __name__ == "__main__":
    unittest.main()
