"""Ship names and ranks: Kemi's character layer.

"Kemi" is an old word for "gemi" (ship) - so every node IS a ship. Instead
of asking humans to recognise hex digests, each node gets a deterministic,
memorable ship name derived from its node id ("çevik-martı-42"), and earns
naval ranks as it contributes compute to the fleet. Names and ranks are
pure presentation: the protocol still speaks node ids.
"""

from __future__ import annotations

import hashlib

ADJECTIVES = [
    "çevik", "cesur", "sessiz", "hızlı", "inatçı", "kurnaz", "sakin", "yaman",
    "gözüpek", "uykucu", "neşeli", "asi", "bilge", "telaşlı", "kibar", "haylaz",
    "mağrur", "utangaç", "şimşek", "gizemli", "dik", "özgür", "serin", "sıcak",
    "tuzlu", "rüzgarlı", "yıldızlı", "puslu", "ay", "şafak", "gece", "fırtına",
]

ANIMALS = [
    "martı", "yunus", "levrek", "kalkan", "orkinos", "vatoz", "ahtapot", "denizatı",
    "yengeç", "karides", "midye", "mürekkepbalığı", "fok", "balina", "köpekbalığı", "mercan",
    "albatros", "pelikan", "karabatak", "sumru", "leylek", "kırlangıç", "şahin", "baykuş",
    "kunduz", "samur", "susamuru", "kaplumbağa", "yılanbalığı", "kefal", "lüfer", "hamsi",
]

# (minimum earned credits, title, insignia)
RANKS = [
    (0.0, "Miço", "·"),
    (25.0, "Tayfa", "⚓"),
    (100.0, "Serdümen", "⚓⚓"),
    (300.0, "Reis", "⚓⚓⚓"),
    (1000.0, "Kaptan", "★"),
    (5000.0, "Amiral", "★★"),
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
       ~        ~    K E M İ    ~        ~
            __|__    işlem gücünün açık denizi
        ____\___/____
        \  ⚙  ⚙  ⚙  /
         \__________/
  ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~
"""
