"""The voice bridge: a synapse network that understands spoken orders.

Jarvis for the fleet. The captain speaks; the browser turns speech into
text (Web Speech API); this module turns text into *intent* — not with
brittle keyword rules but with the same self-training synapse network that
powers provider selection (`kemi.synapse.SynapseNet`).

Utterances are feature-hashed (unigrams + bigrams of accent-normalised
tokens) into a fixed-width binary vector and classified by a small MLP,
pre-seeded with Turkish and English command phrasings. When the captain
corrects a misheard order ("hayır, bakiyeyi sor demiştim"), ``learn()``
adjusts the synapses — the bridge adapts to how *you* speak. The learned
state persists to ``$KEMI_HOME/voice.json``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .synapse import SynapseNet

FEATURES = 256
CONFIDENCE_FLOOR = 0.35   # below this the bridge says "anlamadım"
SEED_EPOCHS = 60

INTENTS = ["durum", "saglayicilar", "is_hash", "sor",
           "dil", "davet", "beyin", "yardim"]

# What each intent means, and the phrasings the bridge is born knowing.
SEED_PHRASES: dict[str, list[str]] = {
    "durum": [
        "durum raporu", "durum nedir", "bakiye ne kadar", "bakiyem ne",
        "ne kadar kredim var", "kac kredim kaldi", "rapor ver", "gemi durumu",
        "status report", "what is my balance", "how many credits",
    ],
    "saglayicilar": [
        "saglayicilari goster", "filoda kim var", "gemileri listele",
        "hangi gemiler var", "saglayici listesi", "kimler calisiyor",
        "list providers", "show the fleet", "who is available",
    ],
    "is_hash": [
        "hash isi gonder", "hash calistir", "bir is gonder", "is calistir",
        "ornek is baslat", "hash hesapla", "test isi gonder",
        "run a hash job", "submit a job", "start a job",
    ],
    "sor": [
        "yapay zekaya sor", "filoya sor", "soyle bakalim", "sana bir sorum var",
        "sor merhaba nasilsin", "yapay zeka cevapla",
        "ask the fleet", "ask the ai", "question for the ai",
    ],
    "dil": [
        "dili degistir", "ingilizceye gec", "turkceye gec", "dil degistir",
        "switch language", "change language", "english please",
    ],
    "davet": [
        "davet kodu", "arkadas davet et", "davet olustur", "davet kodunu goster",
        "invite a friend", "show invite code", "invitation",
    ],
    "beyin": [
        "beyin durumu", "sinaps agi nasil", "ogrenme durumu", "beyin raporu",
        "ne kadar ogrendin", "sinaps raporu", "sinaps agi raporu",
        "sinaps agi ne durumda", "beyin ne durumda",
        "brain status", "how is the brain", "learning progress",
    ],
    "yardim": [
        "yardim", "ne yapabilirsin", "komutlar neler", "beni yonlendir",
        "hangi komutlar var", "nasil kullanirim", "komut listesini goster",
        "secenekleri goster",
        "help", "what can you do", "list commands",
    ],
}

_TR_MAP = str.maketrans("çğıöşüâî", "cgiosuai")


def normalise(text: str) -> list[str]:
    """Casefold, strip Turkish accents and punctuation, tokenize."""
    text = text.casefold().translate(_TR_MAP)
    return re.findall(r"[a-z0-9]+", text)


def grams_of(text: str) -> list[str]:
    # Crude 5-char stemming defuses Turkish suffixes ("komutlar" /
    # "komutlari" / "komutlarim" all become "komut").
    stems = [t[:5] for t in normalise(text)]
    return stems + [f"{a}_{b}" for a, b in zip(stems, stems[1:])]


def _fnv1a(s: str) -> int:
    """FNV-1a 32-bit — deterministic and trivially portable (the dashboard
    demo re-implements it in JavaScript, so the two must agree)."""
    h = 0x811C9DC5
    for byte in s.encode("utf-8"):
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def featurise(text: str, dims: int = FEATURES) -> list[float]:
    """Deterministic feature hashing of unigrams + bigrams."""
    vec = [0.0] * dims
    for gram in grams_of(text):
        vec[_fnv1a(gram) % dims] = 1.0
    return vec


def extract_payload(text: str) -> str:
    """The part of an utterance after an ask-verb: 'filoya sor X' -> 'X'."""
    lowered = " ".join(normalise(text))
    for marker in ("sor ", "soyle ", "ask ", "cevapla "):
        idx = lowered.find(marker)
        if idx != -1 and lowered[idx + len(marker):].strip():
            return lowered[idx + len(marker):].strip()
    return ""


# Seed training is deterministic per RNG seed, so its result is cached
# per-process (a node may build several panels/tests in one run).
_SEED_CACHE: dict[int, tuple[str, list[str]]] = {}


class VoiceBrain:
    """Intent classifier for the captain's spoken orders — a SynapseNet
    seeded with known phrasings, forever correctable."""

    def __init__(self, path: str | None = None, seed: int = 42):
        self.path = path
        self.net: SynapseNet | None = None
        self.vocab: set[str] = set()
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.net = SynapseNet.from_dict(data["net"])
                self.vocab = set(data.get("vocab") or [])
                if self.net.sizes[-1] != len(INTENTS):
                    self.net = None  # intent set changed: retrain from seeds
            except (OSError, ValueError, KeyError, TypeError):
                self.net = None
        if self.net is None and seed in _SEED_CACHE:
            net_json, vocab = _SEED_CACHE[seed]
            self.net = SynapseNet.from_dict(json.loads(net_json))
            self.vocab = set(vocab)
        if self.net is None:
            self.net = SynapseNet([FEATURES, 24, len(INTENTS)], lr=0.5, seed=seed)
            self._seed_training()
            _SEED_CACHE[seed] = (json.dumps(self.net.to_dict()),
                                 sorted(self.vocab))

    def _seed_training(self) -> None:
        examples = []
        for intent, phrases in SEED_PHRASES.items():
            for phrase in phrases:
                self.vocab.update(grams_of(phrase))
                examples.append((featurise(phrase),
                                 [1.0 if i == INTENTS.index(intent) else 0.0
                                  for i in range(len(INTENTS))]))
        for _ in range(SEED_EPOCHS):
            for x, y in examples:
                self.net.train_step(x, y)

    def classify(self, text: str) -> dict[str, Any]:
        """-> {intent, confidence, payload}; intent is None when unsure.

        An utterance whose words the bridge has never been taught is
        rejected outright — a synapse net always outputs *something*, but
        an answer built from zero recognised evidence is a guess, not an
        understanding.
        """
        grams = grams_of(text)
        known = sum(1 for g in grams if g in self.vocab) / len(grams) if grams else 0.0
        out = self.net.predict(featurise(text))
        best = max(range(len(out)), key=lambda i: out[i])
        confidence = out[best]
        intent = INTENTS[best]
        if confidence < CONFIDENCE_FLOOR or known < 0.3:
            intent = None
        return {"intent": intent, "confidence": round(confidence, 3),
                "payload": extract_payload(text) if intent == "sor" else ""}

    def learn(self, text: str, intent: str, repeats: int = 25) -> None:
        """The captain corrects the bridge: teach this phrasing its meaning."""
        if intent not in INTENTS:
            raise ValueError(f"unknown intent: {intent}")
        self.vocab.update(grams_of(text))
        x = featurise(text)
        y = [1.0 if i == INTENTS.index(intent) else 0.0
             for i in range(len(INTENTS))]
        for _ in range(repeats):
            self.net.train_step(x, y)
        self.save()

    def save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"net": self.net.to_dict(),
                           "vocab": sorted(self.vocab)}, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass
