"""Wallet history and reputation decay."""

import unittest

from kemi.gossip_ledger import GENESIS_CREDITS, GossipLedger
from kemi.identity import Identity
from kemi.reputation import ReputationStore

DIFF = 4


class WalletHistoryTests(unittest.TestCase):
    def setUp(self):
        self.ledger = GossipLedger(":memory:", difficulty=DIFF)
        self.addCleanup(self.ledger.close)
        self.alice = Identity.create(DIFF)
        self.bob = Identity.create(DIFF)

    def test_history_tags_direction_and_delta(self):
        self.ledger.add_tx(self.ledger.make_tx(self.alice, self.bob.node_id, 10.0))
        self.ledger.add_tx(self.ledger.make_tx(self.bob, self.alice.node_id, 4.0))
        hist = self.ledger.history(self.alice.node_id)
        self.assertEqual(len(hist), 2)
        # newest first: the 4.0 received from bob
        self.assertEqual(hist[0]["direction"], "received")
        self.assertEqual(hist[0]["delta"], 4.0)
        sent = next(h for h in hist if h["direction"] == "sent")
        self.assertEqual(sent["delta"], -10.0)
        self.assertEqual(sent["counterparty"], self.bob.node_id)

    def test_history_only_my_transactions(self):
        carol = Identity.create(DIFF)
        self.ledger.add_tx(self.ledger.make_tx(self.bob, carol.node_id, 5.0))
        self.assertEqual(self.ledger.history(self.alice.node_id), [])


class ReputationDecayTests(unittest.TestCase):
    def setUp(self):
        self.rep = ReputationStore(":memory:")
        self.addCleanup(self.rep.close)

    def test_transient_marks_fade(self):
        for _ in range(3):
            self.rep.record("flaky", "mismatch")
        low = self.rep.score("flaky")
        for _ in range(20):
            self.rep.decay(0.5)
        # after heavy decay the score recovers toward neutral 0.5
        self.assertGreater(self.rep.score("flaky"), low + 0.2)

    def test_double_spend_condemnation_is_permanent(self):
        self.rep.record("cheat", "double_spend")
        self.assertTrue(self.rep.is_banned("cheat"))
        for _ in range(100):
            self.rep.decay(0.5)
        self.assertTrue(self.rep.is_banned("cheat"))  # never un-bans

    def test_decay_keeps_good_history_good(self):
        for _ in range(10):
            self.rep.record("solid", "chunk_ok")
        self.rep.decay(0.9)
        self.assertGreater(self.rep.score("solid"), 0.5)

    def test_legacy_db_without_condemned_column_migrates(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE reputation (node_id TEXT PRIMARY KEY, "
                        "good REAL DEFAULT 0, bad REAL DEFAULT 0, events INTEGER DEFAULT 0)")
            old.execute("INSERT INTO reputation VALUES ('x', 5, 0, 5)")
            old.commit()
            old.close()
            rep = ReputationStore(path)   # must migrate, not crash
            self.addCleanup(rep.close)
            self.assertFalse(rep.is_banned("x"))
            rep.record("x", "chunk_ok")   # write path works post-migration


if __name__ == "__main__":
    unittest.main()
