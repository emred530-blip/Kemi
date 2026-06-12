"""The high-level library API, chat helpers, doctor and CLI usability."""

import asyncio
import hashlib
import unittest

import kemi
from kemi.chat import build_prompt, chat_once
from kemi.consumer import Consumer
from kemi.identity import Identity
from kemi.node import PeerNode

DIFF = 4


class FleetApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes: list[PeerNode] = []
        self.bootstrap = await self._spawn()
        self.peers = [("127.0.0.1", self.bootstrap.port)]
        await self._spawn(provide=True, price=0.5)
        await self._spawn(provide=True, price=1.0)

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

    def _connect(self):
        return kemi.connect(peer=f"127.0.0.1:{self.bootstrap.port}",
                            difficulty=DIFF, sandbox=False)

    async def test_three_line_usage(self):
        async with self._connect() as fleet:
            results = await fleet.run("hash.sha256", ["a", "b", "c"])
        self.assertEqual(results,
                         [hashlib.sha256(x.encode()).hexdigest() for x in "abc"])

    async def test_identity_and_money_surface(self):
        async with self._connect() as fleet:
            self.assertEqual(len(fleet.node_id), 64)
            self.assertIn("-", fleet.ship)
            self.assertIn("Cabin Boy", fleet.rank())
            self.assertEqual(await fleet.balance(), 100.0)
            providers = await fleet.providers("hash.sha256")
            self.assertEqual(len(providers), 2)

    async def test_generate_and_stream_agree(self):
        async with self._connect() as fleet:
            full = await fleet.generate("hello fleet", max_tokens=8)
            streamed = ""
            async for token in fleet.stream("hello fleet", max_tokens=8):
                streamed += token
        self.assertEqual(full, streamed)
        self.assertEqual(len(full.split()), 8)

    async def test_full_report_and_pipeline(self):
        async with self._connect() as fleet:
            report = await fleet.run("hash.sha256", ["x"], full_report=True)
            self.assertEqual(report.chunks, 1)
            self.assertGreater(report.spent, 0)
            pipe = await fleet.pipeline(
                [{"task": "ai.layer", "params": {"layer": 0}},
                 {"task": "ai.layer", "params": {"layer": 1}}],
                items=[[0.5, -0.5]])
            self.assertEqual(len(pipe.results), 1)
            self.assertEqual(len(pipe.stages), 2)

    async def test_peer_normalisation_rejects_garbage(self):
        with self.assertRaises(ValueError):
            async with kemi.connect(peer="not-an-endpoint"):
                pass


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_prompt_keeps_transcript_and_trims(self):
        history = [("hi", "hello"), ("how are you", "fine")]
        prompt = build_prompt(history, "great")
        self.assertIn("User: hi", prompt)
        self.assertIn("Assistant: fine", prompt)
        self.assertTrue(prompt.endswith("User: great\nAssistant:"))

        long_history = [("q" * 500, "a" * 500) for _ in range(20)]
        trimmed = build_prompt(long_history, "short")
        self.assertLessEqual(len(trimmed), 7000)
        self.assertTrue(trimmed.endswith("User: short\nAssistant:"))

    async def test_chat_once_streams_pays_and_remembers(self):
        nodes: list[PeerNode] = []

        async def spawn(**kwargs):
            node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1",
                            port=0, bootstrap=peers, difficulty=DIFF,
                            sandbox=False, **kwargs)
            await node.start()
            nodes.append(node)
            return node

        peers: list = []
        bootstrap = await spawn()
        peers.append(("127.0.0.1", bootstrap.port))
        await spawn(provide=True, price=1.0)
        me = await spawn()
        try:
            consumer = Consumer(me)
            history: list = []
            tokens: list[str] = []
            reply, spent, provider = await chat_once(
                consumer, history, "hello there", max_tokens=6,
                on_token=tokens.append)
            self.assertEqual(reply, "".join(tokens).strip())
            self.assertEqual(spent, 1.0)
            self.assertEqual(len(provider), 64)
            self.assertEqual(history, [("hello there", reply)])
            # second turn includes the first exchange in its prompt
            await chat_once(consumer, history, "and again", max_tokens=6)
            self.assertEqual(len(history), 2)
        finally:
            for node in nodes:
                await node.stop()


class DoctorTests(unittest.IsolatedAsyncioTestCase):
    async def test_doctor_passes_on_this_machine(self):
        from kemi.doctor import CRITICAL_FAILURES, run_checks

        checks = await run_checks()
        names = {c["name"] for c in checks}
        self.assertTrue(CRITICAL_FAILURES <= names)
        for check in checks:
            if check["name"] in CRITICAL_FAILURES:
                self.assertTrue(check["ok"], check)

    async def test_doctor_reports_unreachable_peer(self):
        from kemi.doctor import run_checks

        checks = await run_checks(peer=("127.0.0.1", 1))
        peer_check = next(c for c in checks if c["name"] == "peer")
        self.assertFalse(peer_check["ok"])


class RunLinesTests(unittest.TestCase):
    def test_lines_flag_parses(self):
        from kemi.cli import build_parser

        args = build_parser().parse_args(
            ["run", "--peer", "1.2.3.4:7700", "--task", "text.wordcount",
             "--input", "-", "--lines"])
        self.assertTrue(args.lines)

    def test_chat_and_doctor_commands_exist(self):
        from kemi.cli import build_parser

        parser = build_parser()
        chat = parser.parse_args(["chat", "--peer", "1.2.3.4:7700"])
        self.assertEqual(chat.func.__name__, "_cmd_chat")
        doctor = parser.parse_args(["doctor"])
        self.assertEqual(doctor.func.__name__, "_cmd_doctor")
        sohbet = parser.parse_args(["sohbet", "--peer", "1.2.3.4:7700"])
        self.assertEqual(sohbet.func.__name__, "_cmd_chat")


if __name__ == "__main__":
    unittest.main()
