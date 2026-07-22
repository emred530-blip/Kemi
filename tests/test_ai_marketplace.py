"""T6/T7 — multi-model marketplace and ai.embed over the fleet."""

import time
import unittest

import kemi
from kemi.ai_backends import MockBackend, OllamaBackend, backend_model, supports_embedding
from kemi.identity import Identity
from kemi.node import PeerNode
from kemi.tasks import run_task

DIFF = 4


class EmbeddingBackendTests(unittest.TestCase):
    def test_mock_embedding_deterministic_unit_norm(self):
        backend = MockBackend()
        a, a2, b = backend.embed("rag"), backend.embed("rag"), backend.embed("other")
        self.assertEqual(a, a2)
        self.assertNotEqual(a, b)
        self.assertAlmostEqual(sum(x * x for x in a) ** 0.5, 1.0, places=6)

    def test_capability_helpers(self):
        self.assertTrue(supports_embedding(MockBackend()))
        self.assertEqual(backend_model(MockBackend()), "mock")
        self.assertEqual(backend_model(OllamaBackend(model="llama3.2")), "llama3.2")
        self.assertIsNone(backend_model(None))

    def test_embed_task_runs_and_guards(self):
        ctx = {"ai_backend": MockBackend()}
        out = run_task("ai.embed", ["a", "b"], {}, ctx)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(out[0]), 64)
        from kemi.tasks import TaskError

        with self.assertRaises(TaskError):
            run_task("ai.embed", ["a"], {}, {})  # no backend


class _NamedMock(MockBackend):
    def __init__(self, model_name):
        super().__init__()
        self.model = model_name


class MarketplaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes: list[PeerNode] = []
        self.bootstrap = await self._spawn()
        self.peers = [("127.0.0.1", self.bootstrap.port)]

    async def asyncTearDown(self):
        for node in self.nodes:
            await node.stop()

    async def _spawn(self, **kwargs) -> PeerNode:
        kwargs.setdefault("sandbox", False)
        node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
                        bootstrap=getattr(self, "peers", None) or [],
                        difficulty=DIFF, **kwargs)
        await node.start()
        self.nodes.append(node)
        return node

    async def test_providers_advertise_model_and_embed(self):
        await self._spawn(provide=True, price=1.0,
                          ai_backend=_NamedMock("llama3.2"))
        fleet_node = await self._spawn()
        from kemi.consumer import Consumer

        records = await Consumer(fleet_node).list_providers("ai.generate")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["model"], "llama3.2")
        self.assertTrue(records[0]["embed"])
        # ai.embed is discoverable as its own task key
        self.assertEqual(len(await Consumer(fleet_node).list_providers("ai.embed")), 1)

    async def test_model_pinning_selects_the_right_provider(self):
        await self._spawn(provide=True, price=1.0, ai_backend=_NamedMock("llama3.2"))
        await self._spawn(provide=True, price=0.5, ai_backend=_NamedMock("mistral"))
        async with kemi.connect(peer=f"127.0.0.1:{self.bootstrap.port}",
                                difficulty=DIFF, sandbox=False) as fleet:
            self.assertEqual(set(await fleet.models()), {"llama3.2", "mistral"})

            llama = await fleet.generate("hi", max_tokens=4, model="llama3.2")
            self.assertEqual(llama, _NamedMock("llama3.2").generate("hi", max_tokens=4))

            from kemi import JobError
            with self.assertRaises(JobError):
                await fleet.generate("hi", model="no-such-model")

    async def test_embed_over_the_fleet(self):
        await self._spawn(provide=True, price=1.0, ai_backend=_NamedMock("nomic"))
        async with kemi.connect(peer=f"127.0.0.1:{self.bootstrap.port}",
                                difficulty=DIFF, sandbox=False) as fleet:
            vectors = await fleet.embed(["alpha", "beta", "gamma"])
            self.assertEqual(len(vectors), 3)
            self.assertEqual(len(vectors[0]), 64)
            # consistent with a local mock embedding
            self.assertEqual(vectors[0], _NamedMock("nomic").embed("alpha"))

    async def test_embed_works_on_a_sandboxed_provider(self):
        # Regression: ai.embed needs the in-process backend object, so it
        # must be exempt from the sandbox subprocess — a default
        # (sandbox=True) provider used to fail every embed chunk with
        # "this provider has no AI backend configured".
        from kemi.sandbox import UNSANDBOXED_TASKS
        self.assertIn("ai.embed", UNSANDBOXED_TASKS)
        await self._spawn(provide=True, price=1.0,
                          ai_backend=_NamedMock("nomic"), sandbox=True)
        async with kemi.connect(peer=f"127.0.0.1:{self.bootstrap.port}",
                                difficulty=DIFF, sandbox=False) as fleet:
            vectors = await fleet.embed(["alpha"])
            self.assertEqual(vectors[0], _NamedMock("nomic").embed("alpha"))


class _StallBackend(MockBackend):
    """Accepts a stream but produces no output for a long while."""

    def __init__(self, delay: float = 4.0):
        super().__init__()
        self.delay = delay
        self.model = "stall-llm"

    def stream(self, prompt, max_tokens=64):
        time.sleep(self.delay)
        yield from super().stream(prompt, max_tokens)


class FleetAnswersTests(unittest.IsolatedAsyncioTestCase):
    """People asking the fleet a question get a real, prompt answer."""

    async def asyncSetUp(self):
        self.nodes: list[PeerNode] = []
        self.bootstrap = await self._spawn()
        self.peers = [("127.0.0.1", self.bootstrap.port)]

    async def asyncTearDown(self):
        for node in self.nodes:
            await node.stop()

    async def _spawn(self, **kwargs) -> PeerNode:
        kwargs.setdefault("sandbox", False)
        node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
                        bootstrap=getattr(self, "peers", None) or [],
                        difficulty=DIFF, **kwargs)
        await node.start()
        self.nodes.append(node)
        return node

    async def test_real_model_outranks_cheaper_mock_for_ai_tasks(self):
        from kemi.consumer import Consumer

        await self._spawn(provide=True, price=0.2)  # default mock backend
        await self._spawn(provide=True, price=1.0,
                          ai_backend=_NamedMock("llama-test"))
        consumer = Consumer(await self._spawn())
        records = await consumer.list_providers("ai.generate")
        self.assertEqual([r["model"] for r in records], ["llama-test", "mock"])
        # non-AI tasks keep the reputation/price order: cheap ship first
        hashes = await consumer.list_providers("hash.sha256")
        self.assertEqual(hashes[0]["price"], 0.2)
        # tasks that don't touch the chat backend (sharded layers, …) must
        # not be re-ranked by an irrelevant model name either
        layers = await consumer.list_providers("ai.layer")
        self.assertEqual(layers[0]["price"], 0.2)

    async def test_stalled_provider_fails_over_before_first_token(self):
        from kemi.consumer import Consumer

        stall = await self._spawn(provide=True, price=0.2,
                                  ai_backend=_StallBackend())
        healthy = await self._spawn(provide=True, price=1.0)  # mock backend
        me = await self._spawn()
        consumer = Consumer(me)
        started = time.monotonic()
        done = None
        # The stalling ship advertises a "real" model so it is hired first;
        # the consumer must abandon it fast and finish on the healthy ship.
        async for event in consumer.stream_generate(
                ["ahoy"], {"max_tokens": 4}, first_token_timeout=1.0):
            if event.get("done"):
                done = event
        self.assertIsNotNone(done)
        self.assertEqual(done["provider"], healthy.identity.node_id)
        self.assertEqual(done["model"], "mock")
        self.assertLess(time.monotonic() - started, 6.0)
        self.assertLess(me.reputation.score(stall.identity.node_id),
                        me.reputation.score(healthy.identity.node_id))
        # The abandoned ship committed our payment (it sent the "accepted"
        # receipt), so the local ledger must mirror BOTH transfers — a
        # failover that forgets the first one could overdraw the account
        # and get it permanently flagged.
        self.assertAlmostEqual(me.ledger.balance(me.identity.node_id),
                               100.0 - 0.2 - 1.0, places=6)

    async def test_warming_ship_is_not_abandoned_while_loading(self):
        from kemi import node as node_module
        from kemi.consumer import Consumer

        old_interval = node_module.STREAM_WARMING_INTERVAL
        node_module.STREAM_WARMING_INTERVAL = 0.5
        try:
            slow = await self._spawn(provide=True, price=0.5,
                                     ai_backend=_StallBackend(delay=2.0))
            me = await self._spawn()
            consumer = Consumer(me)
            done = None
            # 1.2s of dead silence would abandon the ship, but its periodic
            # "warming" liveness frames (every 0.5s) keep it hired while the
            # model takes 2s to produce a first token.
            async for event in consumer.stream_generate(
                    ["ahoy"], {"max_tokens": 4}, first_token_timeout=1.2):
                if event.get("done"):
                    done = event
            self.assertIsNotNone(done)
            self.assertEqual(done["provider"], slow.identity.node_id)
        finally:
            node_module.STREAM_WARMING_INTERVAL = old_interval


if __name__ == "__main__":
    unittest.main()
