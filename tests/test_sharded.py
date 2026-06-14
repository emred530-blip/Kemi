"""T5 — sharded big-model inference: one transformer, many ships."""

import unittest

import kemi
from kemi.identity import Identity
from kemi.model import (ModelSpec, decode, embed, encode, forward_layers,
                        logits_for_last, next_token)
from kemi.node import PeerNode
from kemi.tasks import TaskError, run_task

DIFF = 4


class ModelMathTests(unittest.TestCase):
    def test_sharded_equals_monolithic(self):
        spec = ModelSpec(n_layers=6)
        hidden = embed(spec, encode("the quick brown fox"))
        mono = forward_layers(spec, 0, spec.n_layers, [r[:] for r in hidden])
        shard = [r[:] for r in hidden]
        for start, end in [(0, 2), (2, 5), (5, 6)]:
            shard = forward_layers(spec, start, end, shard)
        self.assertEqual(mono, shard)

    def test_forward_is_deterministic(self):
        spec = ModelSpec()
        h = embed(spec, encode("kemi"))
        self.assertEqual(forward_layers(spec, 0, 3, [r[:] for r in h]),
                         forward_layers(spec, 0, 3, [r[:] for r in h]))

    def test_layer_range_bounds(self):
        spec = ModelSpec(n_layers=4)
        h = embed(spec, encode("x"))
        with self.assertRaises(ValueError):
            forward_layers(spec, 0, 99, h)

    def test_spec_roundtrip_and_validation(self):
        spec = ModelSpec(n_layers=8, d_model=32, n_heads=4)
        self.assertEqual(ModelSpec.from_dict(spec.to_dict()), spec)
        with self.assertRaises(ValueError):
            ModelSpec.from_dict({"n_layers": 4, "d_model": 30, "n_heads": 4,
                                 "d_ff": 64, "vocab": 256})  # 30 % 4 != 0

    def test_tokenizer_roundtrip(self):
        self.assertEqual(decode(encode("merhaba 🌊")), "merhaba 🌊")

    def test_shard_task_matches_local(self):
        spec = ModelSpec(n_layers=4)
        hidden = embed(spec, encode("abc"))
        out = run_task("ai.shard", [[r[:] for r in hidden]],
                       {"spec": spec.to_dict(), "start": 0, "end": 4}, {})
        self.assertEqual(out[0], forward_layers(spec, 0, 4, [r[:] for r in hidden]))

    def test_shard_task_rejects_bad_input(self):
        spec = ModelSpec(n_layers=2)
        with self.assertRaises(TaskError):
            run_task("ai.shard", [[[1.0, 2.0]]],  # wrong width
                     {"spec": spec.to_dict(), "start": 0, "end": 2}, {})


class ShardedFleetTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_generation_matches_local_model(self):
        # ai.shard needs no AI backend — any provider can run layers
        for _ in range(3):
            await self._spawn(provide=True, price=1.0)
        spec = ModelSpec(n_layers=6)
        async with kemi.connect(peer=f"127.0.0.1:{self.bootstrap.port}",
                                difficulty=DIFF, sandbox=False) as fleet:
            report = await fleet.shard_generate("hello", max_tokens=4, spec=spec)
            self.assertEqual(len(report.tokens), 4)
            self.assertGreater(report.spent, 0)
            self.assertGreaterEqual(report.plan.ships, 2)  # distributed

        # the fleet's output must equal a purely local run of the same model
        tokens = encode("hello")
        for _ in range(4):
            hidden = embed(spec, tokens)
            hidden = forward_layers(spec, 0, spec.n_layers, hidden)
            tokens.append(next_token(logits_for_last(spec, hidden)))
        self.assertEqual(report.tokens, tokens[len(encode("hello")):])

    async def test_layers_are_spread_across_distinct_ships(self):
        providers = [await self._spawn(provide=True, price=1.0) for _ in range(4)]
        spec = ModelSpec(n_layers=8)
        from kemi.consumer import Consumer
        from kemi.sharded import ShardedLLM

        consumer = Consumer(await self._spawn())
        plan = await ShardedLLM(consumer, spec).make_plan()
        # every layer 0..7 is covered exactly once, contiguously
        covered = []
        for _, start, end in plan.assignments:
            covered.extend(range(start, end))
        self.assertEqual(covered, list(range(8)))
        self.assertGreaterEqual(plan.ships, 3)  # genuinely distributed

    async def test_redundancy_cross_checks_each_shard(self):
        for _ in range(3):
            await self._spawn(provide=True, price=1.0)
        spec = ModelSpec(n_layers=4)
        async with kemi.connect(peer=f"127.0.0.1:{self.bootstrap.port}",
                                difficulty=DIFF, sandbox=False) as fleet:
            report = await fleet.shard_generate("hi", max_tokens=2, spec=spec,
                                                redundancy=2)
            self.assertEqual(len(report.tokens), 2)
            self.assertGreater(report.verified_stages, 0)  # shards were verified

    async def test_survives_a_corrupt_shard_provider(self):
        class CorruptNode(PeerNode):
            async def _execute_chunk(self, task, items, params):
                if task == "ai.shard":
                    return [[[9.9] * params["spec"]["d_model"]
                             for _ in row] for row in items]
                return await super()._execute_chunk(task, items, params)

        await self._spawn(provide=True, price=1.0)
        await self._spawn(provide=True, price=1.0)
        corrupt = CorruptNode(Identity.create(difficulty=DIFF), host="127.0.0.1",
                              port=0, bootstrap=self.peers, difficulty=DIFF,
                              sandbox=False, provide=True, price=0.1)
        await corrupt.start()
        self.nodes.append(corrupt)

        spec = ModelSpec(n_layers=4)
        async with kemi.connect(peer=f"127.0.0.1:{self.bootstrap.port}",
                                difficulty=DIFF, sandbox=False) as fleet:
            report = await fleet.shard_generate("hi", max_tokens=2, spec=spec,
                                                redundancy=2)
        # majority of honest providers gives the correct local result
        tokens = encode("hi")
        for _ in range(2):
            hidden = forward_layers(spec, 0, spec.n_layers, embed(spec, tokens))
            tokens.append(next_token(logits_for_last(spec, hidden)))
        self.assertEqual(report.tokens, tokens[len(encode("hi")):])


if __name__ == "__main__":
    unittest.main()
