import unittest

from kemi.ledger import STARTING_BALANCE, Ledger, LedgerError


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.close)

    def test_faucet_only_once(self):
        self.assertEqual(self.ledger.ensure_account("alice"), STARTING_BALANCE)
        self.assertEqual(self.ledger.ensure_account("alice"), STARTING_BALANCE)
        self.assertEqual(self.ledger.balance("alice"), STARTING_BALANCE)

    def test_unknown_account(self):
        with self.assertRaises(LedgerError):
            self.ledger.balance("ghost")

    def test_escrow_lifecycle(self):
        self.ledger.ensure_account("alice")
        escrow_id, key = self.ledger.escrow_create("alice", 40.0)
        self.assertEqual(self.ledger.balance("alice"), STARTING_BALANCE - 40.0)

        balance = self.ledger.escrow_redeem(escrow_id, key, "bob", 15.0)
        self.assertEqual(balance, STARTING_BALANCE + 15.0)

        refunded = self.ledger.escrow_release(escrow_id, "alice")
        self.assertEqual(refunded, 25.0)
        self.assertEqual(self.ledger.balance("alice"), STARTING_BALANCE - 15.0)

        with self.assertRaises(LedgerError):
            self.ledger.escrow_redeem(escrow_id, key, "bob", 1.0)

    def test_escrow_insufficient_funds(self):
        self.ledger.ensure_account("alice")
        with self.assertRaises(LedgerError):
            self.ledger.escrow_create("alice", STARTING_BALANCE + 1)

    def test_escrow_wrong_key(self):
        self.ledger.ensure_account("alice")
        escrow_id, _ = self.ledger.escrow_create("alice", 10.0)
        with self.assertRaises(LedgerError) as ctx:
            self.ledger.escrow_redeem(escrow_id, "stolen-key", "mallory", 10.0)
        self.assertEqual(ctx.exception.code, "bad_redeem_key")

    def test_escrow_overdraw(self):
        self.ledger.ensure_account("alice")
        escrow_id, key = self.ledger.escrow_create("alice", 10.0)
        with self.assertRaises(LedgerError):
            self.ledger.escrow_redeem(escrow_id, key, "bob", 11.0)

    def test_escrow_release_requires_owner(self):
        self.ledger.ensure_account("alice")
        escrow_id, _ = self.ledger.escrow_create("alice", 10.0)
        with self.assertRaises(LedgerError):
            self.ledger.escrow_release(escrow_id, "mallory")


if __name__ == "__main__":
    unittest.main()
