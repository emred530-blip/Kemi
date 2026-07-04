"""The voice bridge: synapse-powered intent understanding."""

import tempfile
import unittest
from pathlib import Path

from kemi.voice import INTENTS, VoiceBrain, extract_payload, featurise, normalise

# Phrasings the seeds do NOT contain — the net must generalise to them.
UNSEEN = {
    "durum": ["bakiye durumu nedir", "kredim ne kadar kaldi"],
    "saglayicilar": ["saglayici gemileri goster", "filoda hangi gemiler calisiyor"],
    "is_hash": ["bir hash isi calistir", "hash isi baslat"],
    "dil": ["dili ingilizceye gec", "dil degistir lutfen"],
    "davet": ["davet kodunu ver", "arkadas davet kodu"],
    "beyin": ["sinaps agi durumu", "beyin ne kadar ogrendi"],
    "yardim": ["komutlari goster", "bana yardim et"],
}


class TextPipelineTests(unittest.TestCase):
    def test_normalise_strips_turkish_accents(self):
        self.assertEqual(normalise("SAĞLAYICIları GÖSTER!"),
                         ["saglayicilari", "goster"])

    def test_featurise_is_deterministic_and_bounded(self):
        a, b = featurise("bakiye ne kadar"), featurise("bakiye ne kadar")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 256)
        self.assertTrue(all(v in (0.0, 1.0) for v in a))
        self.assertNotEqual(a, featurise("tamamen farkli bir cumle"))

    def test_payload_extraction(self):
        self.assertEqual(extract_payload("filoya sor kemi nedir"), "kemi nedir")
        self.assertEqual(extract_payload("ask what is a dht"), "what is a dht")
        self.assertEqual(extract_payload("bakiye ne kadar"), "")


class VoiceBrainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.brain = VoiceBrain(seed=42)  # seed-trained once for the class

    def test_understands_unseen_phrasings(self):
        for intent, phrases in UNSEEN.items():
            for phrase in phrases:
                got = self.brain.classify(phrase)
                self.assertEqual(got["intent"], intent,
                                 f"{phrase!r} -> {got}")

    def test_ask_intent_carries_payload(self):
        got = self.brain.classify("filoya sor merhaba nasilsin")
        self.assertEqual(got["intent"], "sor")
        self.assertEqual(got["payload"], "merhaba nasilsin")

    def test_gibberish_is_rejected_not_guessed(self):
        got = self.brain.classify("xyzzy plugh fnord blorp")
        self.assertIsNone(got["intent"])

    def test_captain_corrections_stick(self):
        brain = VoiceBrain(seed=7)
        phrase = "kaptan koprusune rapor"
        brain.learn(phrase, "durum")
        got = brain.classify(phrase)
        self.assertEqual(got["intent"], "durum")
        self.assertGreater(got["confidence"], 0.6)

    def test_learn_rejects_unknown_intent(self):
        with self.assertRaises(ValueError):
            VoiceBrain(seed=1).learn("test", "no_such_intent")

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "voice.json")
            brain = VoiceBrain(path=path, seed=42)
            brain.learn("tam yol ileri rapor", "durum")
            reborn = VoiceBrain(path=path)
            self.assertEqual(reborn.classify("tam yol ileri rapor")["intent"],
                             "durum")
            self.assertEqual(reborn.net.steps, brain.net.steps)

    def test_all_intents_covered_by_seeds(self):
        from kemi.voice import SEED_PHRASES
        self.assertEqual(set(SEED_PHRASES), set(INTENTS))


if __name__ == "__main__":
    unittest.main()
