"""Local (subjective) reputation scores for peers.

Each node keeps its own opinion based on first-hand evidence: completed
chunks, failures, redundancy-mismatch losses, and ledger double-spend
flags. Scores are deliberately *not* gossiped - shared reputation is
trivially poisoned; first-hand experience is not. The ledger's
double-spend evidence, in contrast, is objective and travels with the
transactions themselves.

The score is a Beta-prior estimate of "probability the next interaction
is good", with bad events weighted by severity.
"""

from __future__ import annotations

import json
import sqlite3

WEIGHTS = {
    "chunk_ok": (1.0, 0.0),       # (good, bad) increments
    "chunk_fail": (0.0, 1.0),
    "mismatch": (0.0, 5.0),       # caught returning wrong results
    "payment_reneged": (0.0, 5.0),  # kept payment without delivering
    "double_spend": (0.0, 100.0),
}

BAN_THRESHOLD = 0.2
MIN_EVENTS_FOR_BAN = 3


class ReputationStore:
    def __init__(self, db_path: str = ":memory:"):
        self._db = sqlite3.connect(db_path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS reputation ("
            " node_id TEXT PRIMARY KEY,"
            " good REAL NOT NULL DEFAULT 0,"
            " bad REAL NOT NULL DEFAULT 0,"
            " events INTEGER NOT NULL DEFAULT 0,"
            " condemned INTEGER NOT NULL DEFAULT 0)"
        )
        # migrate older stores that predate the condemned column
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(reputation)")}
        if "condemned" not in cols:
            self._db.execute("ALTER TABLE reputation ADD COLUMN "
                             "condemned INTEGER NOT NULL DEFAULT 0")
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def record(self, node_id: str, event: str) -> None:
        good, bad = WEIGHTS[event]
        condemned = 1 if event == "double_spend" else 0
        self._db.execute(
            "INSERT INTO reputation (node_id, good, bad, events, condemned)"
            " VALUES (?, ?, ?, 1, ?)"
            " ON CONFLICT(node_id) DO UPDATE SET"
            " good = good + excluded.good, bad = bad + excluded.bad,"
            " events = events + 1, condemned = max(condemned, excluded.condemned)",
            (node_id, good, bad, condemned),
        )
        self._db.commit()

    def decay(self, factor: float = 0.9) -> None:
        """Fade transient evidence so a node recovers from an old blip over
        time. Double-spend condemnation is objective and never decays."""
        factor = max(0.0, min(1.0, factor))
        self._db.execute("UPDATE reputation SET good = good * ?, bad = bad * ?",
                         (factor, factor))
        self._db.commit()

    def score(self, node_id: str) -> float:
        row = self._db.execute(
            "SELECT good, bad FROM reputation WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return 0.5  # unknown peers start neutral
        good, bad = row
        return (good + 1.0) / (good + bad + 2.0)

    def is_banned(self, node_id: str) -> bool:
        row = self._db.execute(
            "SELECT good, bad, events, condemned FROM reputation WHERE node_id = ?",
            (node_id,)
        ).fetchone()
        if row is None:
            return False
        good, bad, events, condemned = row
        if condemned:                       # double-spend: permanent
            return True
        score = (good + 1.0) / (good + bad + 2.0)
        return events >= MIN_EVENTS_FOR_BAN and score < BAN_THRESHOLD

    def snapshot(self) -> dict[str, dict[str, float]]:
        rows = self._db.execute("SELECT node_id, good, bad, events FROM reputation").fetchall()
        return {
            node_id: {"good": good, "bad": bad, "events": events,
                      "score": round((good + 1.0) / (good + bad + 2.0), 3)}
            for node_id, good, bad, events in rows
        }

    def dump_json(self) -> str:
        return json.dumps(self.snapshot(), indent=2)
