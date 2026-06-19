"""T1 stake-weighted witnesses and T17 economy simulation."""

import unittest

from kemi.dht import Contact
from kemi.economy import EconomyReport, _gini, render, simulate
from kemi.gossip_ledger import GENESIS_CREDITS
from kemi.identity import Identity
from kemi.node import WITNESS_COUNT, PeerNode

DIFF = 4


class EconomySimTests(unittest.TestCase):
    def test_supply_is_conserved_and_linear(self):
        r = simulate(identities=80, providers=20, rounds=3000)
        self.assertTrue(r.conserved)
        self.assertAlmostEqual(r.total_supply, 80 * GENESIS_CREDITS, places=6)

    def test_credits_flow_to_providers(self):
        r = simulate(identities=60, providers=15, rounds=4000, seed=3)
        # providers (25% of identities) should end up holding the majority
        self.assertGreater(r.provider_share, 0.4)

    def test_deterministic(self):
        self.assertEqual(simulate(seed=42), simulate(seed=42))

    def test_gini_bounds(self):
        self.assertEqual(_gini([]), 0.0)
        self.assertEqual(_gini([5, 5, 5, 5]), 0.0)         # perfect equality
        self.assertGreater(_gini([0, 0, 0, 100]), 0.5)     # high inequality

    def test_report_renders(self):
        text = render(simulate(identities=10, rounds=100))
        self.assertIn("total supply", text)
        self.assertIn("Gini", text)


class StakeWeightedWitnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1",
                             port=0, difficulty=DIFF, sandbox=False)
        await self.node.start()

    async def asyncTearDown(self):
        await self.node.stop()

    async def test_committee_prefers_higher_earners(self):
        # build more candidates than the committee size, give some of them
        # earnings on our ledger, and confirm the richest are chosen
        rich = [Identity.create(difficulty=DIFF) for _ in range(3)]
        poor = [Identity.create(difficulty=DIFF) for _ in range(WITNESS_COUNT + 2)]
        payer = Identity.create(difficulty=DIFF)
        for r in rich:
            # credit each rich identity from a distinct funded payer
            funder = Identity.create(difficulty=DIFF)
            self.node.ledger.add_tx(
                self.node.ledger.make_tx(funder, r.node_id, 50.0))

        candidates = [Contact(i.node_id, "127.0.0.1", 9000 + n)
                      for n, i in enumerate(rich + poor)]
        committee = self.node._rank_witnesses(candidates)
        self.assertEqual(len(committee), WITNESS_COUNT)
        chosen = {c.node_id for c in committee}
        for r in rich:
            self.assertIn(r.node_id, chosen)  # every high-earner made the cut

    async def test_rank_truncates_to_committee_size(self):
        candidates = [Contact(Identity.create(difficulty=DIFF).node_id,
                              "127.0.0.1", 9100 + n)
                      for n in range(WITNESS_COUNT + 5)]
        self.assertEqual(len(self.node._rank_witnesses(candidates)), WITNESS_COUNT)


if __name__ == "__main__":
    unittest.main()
