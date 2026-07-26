"""Human-readable node names and contribution tiers.

Recognising 64-character hex digests is impractical, so every node also
gets a deterministic, memorable name derived from its id
("swift-gull-42"), and a contribution tier that reflects how much compute
it has served the network. Names and tiers are presentation only: the
protocol always speaks node ids.
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

# Contribution tiers: (minimum credits earned, title, marker). A node's tier
# is a plain record of how much compute it has actually served the network.
RANKS = [
    (0.0, "Unranked", "·"),
    (25.0, "Contributor", "▪"),
    (100.0, "Established", "▪▪"),
    (300.0, "Trusted", "▪▪▪"),
    (1000.0, "Principal", "◆"),
    (5000.0, "Core", "◆◆"),
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
  ██  K E M I
  ██  decentralised compute and AI network
"""
