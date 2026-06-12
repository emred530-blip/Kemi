"""Ship names and ranks: Kemi's character layer.

"Kemi" is an old Turkish word for ship - so every node IS a ship. Instead
of asking humans to recognise hex digests, each node gets a deterministic,
memorable ship name derived from its node id ("swift-gull-42"), and earns
naval ranks as it contributes compute to the fleet. Names and ranks are
pure presentation: the protocol still speaks node ids.
"""

from __future__ import annotations

import hashlib

ADJECTIVES = [
    "swift", "brave", "silent", "rapid", "stubborn", "cunning", "calm", "fierce",
    "fearless", "sleepy", "merry", "rebel", "wise", "restless", "gentle", "rowdy",
    "proud", "shy", "thunder", "mystic", "bold", "free", "cool", "warm",
    "salty", "windy", "starry", "misty", "lunar", "dawn", "night", "storm",
]

ANIMALS = [
    "gull", "dolphin", "bass", "turbot", "tuna", "ray", "octopus", "seahorse",
    "crab", "shrimp", "mussel", "cuttlefish", "seal", "whale", "shark", "coral",
    "albatross", "pelican", "cormorant", "tern", "stork", "swallow", "hawk", "owl",
    "beaver", "marten", "otter", "turtle", "eel", "mullet", "bluefish", "anchovy",
]

# (minimum earned credits, title, insignia)
RANKS = [
    (0.0, "Cabin Boy", "·"),
    (25.0, "Deckhand", "⚓"),
    (100.0, "Helmsman", "⚓⚓"),
    (300.0, "First Mate", "⚓⚓⚓"),
    (1000.0, "Captain", "★"),
    (5000.0, "Admiral", "★★"),
]


def ship_name(node_id: str) -> str:
    """Deterministic, human-memorable name for a node id."""
    h = hashlib.sha256(f"kemi:name:{node_id}".encode("utf-8")).digest()
    adjective = ADJECTIVES[h[0] % len(ADJECTIVES)]
    animal = ANIMALS[h[1] % len(ANIMALS)]
    number = h[2] % 90 + 10
    return f"{adjective}-{animal}-{number}"


def rank_for(earned: float) -> tuple[str, str, float | None]:
    """(title, insignia, credits needed for the next rank or None at the top)."""
    current = RANKS[0]
    next_threshold: float | None = None
    for index, (threshold, title, insignia) in enumerate(RANKS):
        if earned >= threshold:
            current = (threshold, title, insignia)
            next_threshold = (RANKS[index + 1][0]
                              if index + 1 < len(RANKS) else None)
    return current[1], current[2], next_threshold


BANNER = r"""
       ~        ~    K E M I    ~        ~
            __|__    the open sea of compute
        ____\___/____
        \  ⚙  ⚙  ⚙  /
         \__________/
  ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~
"""
