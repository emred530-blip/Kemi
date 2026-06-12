"""Decentralised credit ledger: a gossip-replicated set of signed transfers.

Every node keeps a full replica. The ledger state is a *grow-only set* of
Ed25519-signed transactions, so replicas converge regardless of delivery
order (CRDT semantics) - there is no coordinator and no consensus round.

Rules, applied identically and independently by every replica:

* A transaction is only stored if its signature verifies and the sender's
  node id carries a valid identity proof-of-work.
* Each sender numbers their transfers with a strictly increasing ``seq``.
  Two different transactions with the same (sender, seq) are proof of
  attempted double-spending: both are kept as evidence, only the one with
  the smallest tx id counts towards balances (deterministic tie-break),
  and the sender is permanently *flagged*.
* ``balance = genesis credits + counted incoming - counted outgoing``. A
  negative balance (overspending discovered through gossip) marks the
  account as overdrawn; honest nodes refuse further service to it.

This trades instant finality for coordination-free operation - the same
trade BitTorrent makes - and bounds the damage a cheater can do to one
chunk's price per victim before gossip catches up. (Roadmap: stake-weighted
quorum receipts for hard finality.)
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .crypto import canonical, digest, open_envelope, sign_envelope
from .identity import POW_DIFFICULTY_BITS, Identity, verify_node_id

# Faucet: credits every identity is born with. Minting identities costs
# proof-of-work, which is what makes this faucet expensive to farm.
GENESIS_CREDITS = 100.0

# Reject transactions timestamped too far in the future (clock skew guard).
MAX_CLOCK_SKEW = 300.0


def _round(amount: float) -> float:
    return round(float(amount), 6)


class GossipLedger:
    def __init__(self, db_path: str = ":memory:",
                 difficulty: int = POW_DIFFICULTY_BITS):
        self.difficulty = difficulty
        self._db = sqlite3.connect(db_path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS txs (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_id TEXT UNIQUE NOT NULL,
                sender TEXT NOT NULL,
                seq INTEGER NOT NULL,
                recipient TEXT NOT NULL,
                amount REAL NOT NULL,
                envelope TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_txs_sender ON txs (sender, seq);
            CREATE INDEX IF NOT EXISTS idx_txs_recipient ON txs (recipient);
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # -- creating transactions ----------------------------------------------

    def next_seq(self, sender: str) -> int:
        row = self._db.execute(
            "SELECT MAX(seq) FROM txs WHERE sender = ?", (sender,)
        ).fetchone()
        return (row[0] or 0) + 1

    def reserve_seq(self, node_id: str) -> int:
        """Atomically claim the next seq for our own transfers.

        A claimed seq is *never* reused, even if the transaction it was
        signed into is abandoned: a copy may have reached another peer, and
        signing a second transfer with the same seq would flag us as a
        double-spender. Burned seqs simply leave harmless gaps.
        """
        key = f"reserved_seq:{node_id}"
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        reserved = max(self.next_seq(node_id) - 1, int(row[0]) if row else 0)
        claimed = reserved + 1
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(claimed)),
        )
        self._db.commit()
        return claimed

    def make_tx(self, identity: Identity, recipient: str, amount: float) -> dict[str, Any]:
        """Create (but do not apply) a signed transfer envelope."""
        payload = {
            "kind": "tx",
            "from": identity.node_id,
            "pow_nonce": identity.pow_nonce,
            "to": recipient,
            "amount": _round(amount),
            "seq": self.reserve_seq(identity.node_id),
            "ts": round(time.time(), 3),
        }
        return sign_envelope(identity.key, payload)

    # -- applying transactions ------------------------------------------------

    def add_tx(self, envelope: dict[str, Any]) -> str:
        """Validate and store a transaction envelope.

        Returns ``"new"`` (stored), ``"known"`` (duplicate), ``"conflict"``
        (stored, but exposes a double-spend) or ``"invalid"``.
        """
        payload = open_envelope(envelope)
        if payload is None or payload.get("kind") != "tx":
            return "invalid"
        sender = payload.get("from", "")
        recipient = payload.get("to", "")
        amount = payload.get("amount")
        seq = payload.get("seq")
        if (
            not isinstance(amount, (int, float)) or amount <= 0
            or not isinstance(seq, int) or seq < 1
            or not isinstance(recipient, str) or len(recipient) != 64
            or recipient == sender
            or not isinstance(payload.get("ts"), (int, float))
            or payload["ts"] > time.time() + MAX_CLOCK_SKEW
        ):
            return "invalid"
        # The sender's node id must be backed by the envelope's signing key
        # and carry a valid proof-of-work: nobody can spend from an account
        # they do not hold the key for.
        if not verify_node_id(sender, envelope["pubkey"], payload.get("pow_nonce", -1),
                              self.difficulty):
            return "invalid"

        tx_id = digest(payload)
        if self._db.execute("SELECT 1 FROM txs WHERE tx_id = ?", (tx_id,)).fetchone():
            return "known"
        conflict = self._db.execute(
            "SELECT 1 FROM txs WHERE sender = ? AND seq = ?", (sender, seq)
        ).fetchone() is not None
        self._db.execute(
            "INSERT INTO txs (tx_id, sender, seq, recipient, amount, envelope)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (tx_id, sender, seq, recipient, _round(amount),
             canonical(envelope).decode("utf-8")),
        )
        self._db.commit()
        return "conflict" if conflict else "new"

    # -- queries ---------------------------------------------------------------

    _COUNTED = (
        "SELECT t.sender, t.seq, t.recipient, t.amount FROM txs t"
        " WHERE t.tx_id = (SELECT MIN(tx_id) FROM txs"
        "                  WHERE sender = t.sender AND seq = t.seq)"
    )

    def balance(self, node_id: str) -> float:
        incoming = self._db.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM ({self._COUNTED}) WHERE recipient = ?",
            (node_id,),
        ).fetchone()[0]
        outgoing = self._db.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM ({self._COUNTED}) WHERE sender = ?",
            (node_id,),
        ).fetchone()[0]
        return _round(GENESIS_CREDITS + incoming - outgoing)

    def total_earned(self, node_id: str) -> float:
        """Lifetime counted incoming credits - what ranks are made of."""
        earned = self._db.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM ({self._COUNTED}) WHERE recipient = ?",
            (node_id,),
        ).fetchone()[0]
        return _round(earned)

    def is_flagged(self, node_id: str) -> bool:
        """True if the account has provably double-spent or is overdrawn."""
        double_spend = self._db.execute(
            "SELECT 1 FROM txs WHERE sender = ? GROUP BY seq HAVING COUNT(*) > 1 LIMIT 1",
            (node_id,),
        ).fetchone() is not None
        return double_spend or self.balance(node_id) < 0

    def tx_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM txs").fetchone()[0]

    def recent_txs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent transfer payloads, newest first (for dashboards)."""
        rows = self._db.execute(
            "SELECT envelope FROM txs ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for (envelope_json,) in rows:
            payload = json.loads(envelope_json)["payload"]
            out.append({key: payload[key] for key in ("from", "to", "amount", "seq", "ts")})
        return out

    def double_spenders(self) -> list[str]:
        """Accounts with provable double-spends (conflicting seq evidence)."""
        rows = self._db.execute(
            "SELECT DISTINCT sender FROM ("
            " SELECT sender FROM txs GROUP BY sender, seq HAVING COUNT(*) > 1)"
        ).fetchall()
        return [sender for (sender,) in rows]

    def envelopes_for_seq(self, sender: str, seq: int) -> list[dict[str, Any]]:
        """All stored envelopes for a (sender, seq) - conflict evidence."""
        rows = self._db.execute(
            "SELECT envelope FROM txs WHERE sender = ? AND seq = ?", (sender, seq)
        ).fetchall()
        return [json.loads(envelope) for (envelope,) in rows]

    # -- replication -------------------------------------------------------------

    def txs_after(self, cursor: int, limit: int = 500) -> tuple[list[dict[str, Any]], int]:
        """Anti-entropy pull: envelopes stored after ``cursor`` (a rowid)."""
        rows = self._db.execute(
            "SELECT rowid, envelope FROM txs WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (cursor, limit),
        ).fetchall()
        if not rows:
            return [], cursor
        return [json.loads(envelope) for _, envelope in rows], rows[-1][0]
