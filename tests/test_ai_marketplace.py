"""T6/T7 — multi-model marketplace and ai.embed over the fleet."""

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
        node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
                        bootstrap=getattr(self, "peers", None) or [],
                        difficulty=DIFF, sandbox=False, **kwargs)
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


if __name__ == "__main__":
    unittest.main()
