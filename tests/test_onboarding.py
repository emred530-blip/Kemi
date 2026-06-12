"""Onboarding & character layer: ship names, ranks, invites, LAN discovery,
the interactive tutorial and the join wizard's building blocks."""

import asyncio
import contextlib
import io
import unittest

from kemi.gossip_ledger import GossipLedger
from kemi.identity import Identity
from kemi.invite import InviteError, make_invite, parse_invite
from kemi.names import ADJECTIVES, ANIMALS, rank_for, ship_name

DIFF = 4


class ShipNameTests(unittest.TestCase):
    def test_deterministic(self):
        node_id = "ab" * 32
        self.assertEqual(ship_name(node_id), ship_name(node_id))

    def test_format_and_vocabulary(self):
        name = ship_name("cd" * 32)
        adjective, animal, number = name.rsplit("-", 2)
        self.assertIn(adjective, ADJECTIVES)
        self.assertIn(animal, ANIMALS)
        self.assertTrue(10 <= int(number) <= 99)

    def test_spread(self):
        names = {ship_name(f"{i:064x}") for i in range(500)}
        self.assertGreater(len(names), 400)  # collisions should be rare


class RankTests(unittest.TestCase):
    def test_progression(self):
        self.assertEqual(rank_for(0.0)[0], "Cabin Boy")
        self.assertEqual(rank_for(30.0)[0], "Deckhand")
        self.assertEqual(rank_for(150.0)[0], "Helmsman")
        self.assertEqual(rank_for(500.0)[0], "First Mate")
        self.assertEqual(rank_for(1500.0)[0], "Captain")
        title, _, next_threshold = rank_for(10_000.0)
        self.assertEqual(title, "Admiral")
        self.assertIsNone(next_threshold)

    def test_next_threshold(self):
        _, _, next_threshold = rank_for(0.0)
        self.assertEqual(next_threshold, 25.0)

    def test_total_earned_feeds_ranks(self):
        ledger = GossipLedger(":memory:", difficulty=DIFF)
        self.addCleanup(ledger.close)
        alice, bob = Identity.create(DIFF), Identity.create(DIFF)
        for amount in (10.0, 20.0):
            ledger.add_tx(ledger.make_tx(alice, bob.node_id, amount))
        self.assertEqual(ledger.total_earned(bob.node_id), 30.0)
        self.assertEqual(rank_for(ledger.total_earned(bob.node_id))[0], "Deckhand")
        self.assertEqual(ledger.total_earned(alice.node_id), 0.0)  # spending ≠ earning


class InviteTests(unittest.TestCase):
    def test_roundtrip(self):
        peers = [("203.0.113.7", 7700), ("198.51.100.2", 7701)]
        code = make_invite(peers, note="bizim filo")
        self.assertTrue(code.startswith("kemi1-"))
        parsed = parse_invite(code)
        self.assertEqual(parsed["peers"], peers)
        self.assertEqual(parsed["note"], "bizim filo")

    def test_tolerant_parsing(self):
        code = make_invite([("10.0.0.5", 7700)])
        mangled = "  " + code.upper().replace("KEMI1-", "kemi1-") + " \n"
        self.assertEqual(parse_invite(mangled)["peers"], [("10.0.0.5", 7700)])

    def test_garbage_rejected(self):
        for bad in ("", "kemi1-", "kemi1-!!!!", "merhaba", "kemi1-mfzq"):
            with self.assertRaises(InviteError):
                parse_invite(bad)


class LanDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_beacon_is_discovered(self):
        from kemi import lan

        beacon = lan.LanBeacon("a" * 64, tcp_port=7711, name="test-ship")
        await beacon.start()
        try:
            found = await lan.discover(own_id="b" * 64, timeout=1.0)
        finally:
            await beacon.stop()
        if not found:  # multicast may be unavailable in some sandboxes
            self.skipTest("multicast loopback unavailable in this environment")
        self.assertIn(7711, [port for _, port in found])

    async def test_own_beacon_is_ignored(self):
        from kemi import lan

        beacon = lan.LanBeacon("c" * 64, tcp_port=7712)
        await beacon.start()
        try:
            found = await lan.discover(own_id="c" * 64, timeout=1.0)
        finally:
            await beacon.stop()
        self.assertNotIn(7712, [port for _, port in found])


class TutorialTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_tutorial_runs_to_completion(self):
        from kemi.tutorial import run_tutorial

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = await asyncio.wait_for(run_tutorial(fast=True), timeout=120)
        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("STEP 1/5", output)
        self.assertIn("STEP 5/5", output)
        self.assertIn("credits", output)
        self.assertIn("kemi join", output)


class CliSurfaceTests(unittest.TestCase):
    def test_turkish_aliases_resolve(self):
        from kemi.cli import build_parser

        parser = build_parser()
        for argv, command in (
            (["bakiye", "--peer", "1.2.3.4:7700"], "_cmd_balance"),
            (["filo", "--peer", "1.2.3.4:7700"], "_cmd_providers"),
            (["durum", "--peer", "1.2.3.4:7700"], "_cmd_status"),
            (["davet", "--peer", "1.2.3.4:7700"], "_cmd_invite"),
            (["ogren", "--hizli"], "_cmd_learn"),
            (["katil", "--izle"], "_cmd_join"),
            (["join", "--watch"], "_cmd_join"),
            (["learn", "--fast"], "_cmd_learn"),
        ):
            args = parser.parse_args(argv)
            self.assertEqual(args.func.__name__, command, argv)


if __name__ == "__main__":
    unittest.main()
