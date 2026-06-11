import unittest

from kemi.crypto import sign_envelope
from kemi.gossip_ledger import GENESIS_CREDITS, GossipLedger
from kemi.identity import Identity

DIFF = 4  # low PoW difficulty keeps tests fast


def make_identity() -> Identity:
    return Identity.create(difficulty=DIFF)


class GossipLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = GossipLedger(":memory:", difficulty=DIFF)
        self.addCleanup(self.ledger.close)
        self.alice = make_identity()
        self.bob = make_identity()

    def test_genesis_balance(self):
        self.assertEqual(self.ledger.balance(self.alice.node_id), GENESIS_CREDITS)

    def test_transfer(self):
        tx = self.ledger.make_tx(self.alice, self.bob.node_id, 25.0)
        self.assertEqual(self.ledger.add_tx(tx), "new")
        self.assertEqual(self.ledger.add_tx(tx), "known")
        self.assertEqual(self.ledger.balance(self.alice.node_id), GENESIS_CREDITS - 25.0)
        self.assertEqual(self.ledger.balance(self.bob.node_id), GENESIS_CREDITS + 25.0)

    def test_rejects_forged_and_malformed(self):
        tx = self.ledger.make_tx(self.alice, self.bob.node_id, 5.0)
        forged = {**tx, "payload": {**tx["payload"], "amount": 50.0}}
        self.assertEqual(self.ledger.add_tx(forged), "invalid")
        for bad in (
            {**tx["payload"], "amount": -1.0},
            {**tx["payload"], "seq": 0},
            {**tx["payload"], "to": self.alice.node_id},  # self-transfer
            {**tx["payload"], "ts": 9999999999.0},        # far future
        ):
            envelope = sign_envelope(self.alice.key, bad)
            self.assertEqual(self.ledger.add_tx(envelope), "invalid", bad)

    def test_rejects_weak_identity(self):
        """Senders whose node id lacks the proof-of-work are refused."""
        strict = GossipLedger(":memory:", difficulty=64)
        self.addCleanup(strict.close)
        tx = self.ledger.make_tx(self.alice, self.bob.node_id, 1.0)
        self.assertEqual(strict.add_tx(tx), "invalid")

    def test_double_spend_detection(self):
        tx1 = self.ledger.make_tx(self.alice, self.bob.node_id, 10.0)
        # craft a second transfer reusing the same seq (a real double spend)
        carol = make_identity()
        payload = {**tx1["payload"], "to": carol.node_id}
        tx2 = sign_envelope(self.alice.key, payload)
        self.assertEqual(self.ledger.add_tx(tx1), "new")
        self.assertEqual(self.ledger.add_tx(tx2), "conflict")
        self.assertTrue(self.ledger.is_flagged(self.alice.node_id))
        self.assertFalse(self.ledger.is_flagged(self.bob.node_id))
        # only one of the conflicting txs counts toward balances, and the
        # winner is the same on every replica (deterministic tie-break)
        other = GossipLedger(":memory:", difficulty=DIFF)
        self.addCleanup(other.close)
        other.add_tx(tx2)  # reversed arrival order
        other.add_tx(tx1)
        for account in (self.alice, self.bob, carol):
            self.assertEqual(self.ledger.balance(account.node_id),
                             other.balance(account.node_id))

    def test_overdraft_flags_account(self):
        tx = self.ledger.make_tx(self.alice, self.bob.node_id, GENESIS_CREDITS + 50)
        self.assertEqual(self.ledger.add_tx(tx), "new")  # stored: evidence
        self.assertTrue(self.ledger.is_flagged(self.alice.node_id))

    def test_replication_via_txs_after(self):
        for amount in (5.0, 7.5, 2.25):
            self.ledger.add_tx(self.ledger.make_tx(self.alice, self.bob.node_id, amount))
        replica = GossipLedger(":memory:", difficulty=DIFF)
        self.addCleanup(replica.close)
        envelopes, cursor = self.ledger.txs_after(0)
        for envelope in envelopes:
            self.assertIn(replica.add_tx(envelope), ("new", "known"))
        self.assertEqual(replica.balance(self.bob.node_id),
                         self.ledger.balance(self.bob.node_id))
        more, cursor2 = self.ledger.txs_after(cursor)
        self.assertEqual(more, [])
        self.assertEqual(cursor2, cursor)

    def test_seq_reservation_never_reuses(self):
        seqs = {self.ledger.reserve_seq(self.alice.node_id) for _ in range(5)}
        self.assertEqual(len(seqs), 5)
        # a tx made after burning seqs continues past them
        tx = self.ledger.make_tx(self.alice, self.bob.node_id, 1.0)
        self.assertGreater(tx["payload"]["seq"], max(seqs))


if __name__ == "__main__":
    unittest.main()
