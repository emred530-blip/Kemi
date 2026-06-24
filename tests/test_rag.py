"""RAG over the fleet: vector.search task + Fleet.rag_search end to end."""

import unittest

import kemi
from kemi.identity import Identity
from kemi.node import PeerNode
from kemi.tasks import TaskError, run_task

DIFF = 4


class VectorSearchTaskTests(unittest.TestCase):
    def test_cosine_ranking(self):
        item = {"q": [1.0, 0.0], "docs": [[0.0, 1.0], [1.0, 0.0], [0.9, 0.1]]}
        out = run_task("vector.search", [item], {"top_k": 3}, {})
        self.assertEqual([h["index"] for h in out[0]], [1, 2, 0])
        self.assertAlmostEqual(out[0][0]["score"], 1.0, places=9)

    def test_top_k_truncates(self):
        item = {"q": [1.0, 1.0], "docs": [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]}
        self.assertEqual(len(run_task("vector.search", [item], {"top_k": 1}, {})[0]), 1)

    def test_bad_input_rejected(self):
        for bad in ({"docs": [[1.0]]}, {"q": [1.0], "docs": [[1.0, 2.0]]},
                    {"q": "nope", "docs": []}):
            with self.assertRaises(TaskError):
                run_task("vector.search", [bad], {}, {})

    def test_deterministic_and_cacheable(self):
        from kemi.node import NON_CACHEABLE
        self.assertNotIn("vector.search", NON_CACHEABLE)
        item = {"q": [0.3, 0.4], "docs": [[0.3, 0.4], [1.0, 0.0]]}
        self.assertEqual(run_task("vector.search", [item], {}, {}),
                         run_task("vector.search", [item], {}, {}))


class RagFleetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes: list[PeerNode] = []
        self.bootstrap = await self._spawn()
        self.peers = [("127.0.0.1", self.bootstrap.port)]
        await self._spawn(provide=True, price=1.0)

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

    async def test_rag_search_end_to_end(self):
        docs = ["the cat sat on the mat", "quantum chromodynamics",
                "a dog in the park", "distributed systems and consensus"]
        async with kemi.connect(peer=f"127.0.0.1:{self.bootstrap.port}",
                                difficulty=DIFF, sandbox=False) as fleet:
            hits = await fleet.rag_search("the cat", docs, top_k=2)
            self.assertEqual(len(hits), 2)
            self.assertIn("document", hits[0])
            self.assertIn("score", hits[0])
            # scores are sorted best-first
            self.assertGreaterEqual(hits[0]["score"], hits[1]["score"])

    async def test_rag_search_empty_docs(self):
        async with kemi.connect(peer=f"127.0.0.1:{self.bootstrap.port}",
                                difficulty=DIFF, sandbox=False) as fleet:
            self.assertEqual(await fleet.rag_search("q", []), [])


if __name__ == "__main__":
    unittest.main()
