"""The self-training synapse network (fleet brain)."""

import json
import tempfile
import unittest
from pathlib import Path

from kemi.synapse import WARMUP_STEPS, FleetBrain, SynapseNet


class SynapseNetTests(unittest.TestCase):
    def test_learns_xor_online(self):
        net = SynapseNet([2, 6, 6, 1], lr=0.6, seed=7)
        data = [([0, 0], [0]), ([0, 1], [1]), ([1, 0], [1]), ([1, 1], [0])]
        for _ in range(4000):
            for x, y in data:
                net.train_step(x, y)
        for x, y in data:
            pred = net.predict(x)[0]
            if y[0] == 1:
                self.assertGreater(pred, 0.7, f"xor{x} should be high, got {pred}")
            else:
                self.assertLess(pred, 0.3, f"xor{x} should be low, got {pred}")

    def test_loss_decreases_with_training(self):
        net = SynapseNet([2, 4, 1], lr=0.5, seed=1)
        first = net.train_step([1, 0], [1])
        for _ in range(500):
            net.train_step([1, 0], [1])
        self.assertLess(net.loss_ema, first)
        self.assertEqual(net.steps, 501)

    def test_roundtrip_serialisation(self):
        net = SynapseNet([3, 5, 2], lr=0.2, seed=3)
        for i in range(50):
            net.train_step([i % 2, 0.5, 1], [1, 0])
        clone = SynapseNet.from_dict(json.loads(json.dumps(net.to_dict())))
        self.assertEqual(clone.predict([1, 0.5, 1]), net.predict([1, 0.5, 1]))
        self.assertEqual(clone.steps, net.steps)

    def test_input_validation(self):
        net = SynapseNet([2, 2, 1])
        with self.assertRaises(ValueError):
            net.predict([1, 2, 3])
        with self.assertRaises(ValueError):
            net.train_step([1, 2], [1, 2])
        with self.assertRaises(ValueError):
            SynapseNet([4])


def _record(price, relay=False, e2e=True, cpu=4, gpus=0):
    return {"price": price, "relay": relay, "e2e": e2e,
            "resources": {"cpu_count": cpu, "gpus": [{}] * gpus}}


class FleetBrainTests(unittest.TestCase):
    def test_learns_who_to_trust(self):
        brain = FleetBrain(lr=0.5, seed=11)
        good, bad = _record(0.5, cpu=8), _record(2.5, relay=True, cpu=1)
        for _ in range(600):
            brain.learn(good, 0.9, True)
            brain.learn(bad, 0.2, False)
        self.assertGreater(brain.score(good, 0.9), brain.score(bad, 0.2) + 0.3)
        self.assertTrue(brain.trained)
        self.assertGreater(brain.acc_ema, 0.8)

    def test_rank_score_neutral_before_warmup(self):
        brain = FleetBrain(seed=2)
        rec = _record(1.0)
        # untrained: ranking falls back to pure reputation
        self.assertEqual(brain.rank_score(rec, 0.77), 0.77)
        for _ in range(WARMUP_STEPS):
            brain.learn(rec, 0.5, True)
        self.assertNotEqual(brain.rank_score(rec, 0.77), 0.77)

    def test_persistence_roundtrip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "brain.json")
            brain = FleetBrain(path=path, seed=5)
            rec = _record(1.0)
            for _ in range(60):
                brain.learn(rec, 0.8, True)
            brain.save()
            reborn = FleetBrain(path=path)
            self.assertEqual(reborn.net.steps, brain.net.steps)
            self.assertEqual(reborn.score(rec, 0.8), brain.score(rec, 0.8))
            # corrupt file: start fresh instead of crashing
            Path(path).write_text("{broken")
            fresh = FleetBrain(path=path)
            self.assertEqual(fresh.net.steps, 0)

    def test_snapshot_ui_shape(self):
        brain = FleetBrain(seed=9)
        brain.learn(_record(1.0), 0.5, True)
        snap = brain.snapshot_ui()
        self.assertEqual(snap["sizes"][0], len(snap["features"]))
        self.assertEqual(len(snap["weights"]), len(snap["sizes"]) - 1)
        for li, layer in enumerate(snap["weights"]):
            self.assertEqual(len(layer), snap["sizes"][li + 1])
            self.assertEqual(len(layer[0]), snap["sizes"][li])
        self.assertEqual(snap["steps"], 1)
        self.assertFalse(snap["trained"])


if __name__ == "__main__":
    unittest.main()
