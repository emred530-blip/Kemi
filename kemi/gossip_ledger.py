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
            CREATE TABLE IF NOT EXISTS base (
                node_id TEXT PRIMARY KEY,
                balance REAL NOT NULL,
                base_seq INTEGER NOT NULL,
                earned REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS flagged_base (
                node_id TEXT PRIMARY KEY
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
        return max(row[0] or 0, self._base_seq(sender)) + 1

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
        # Pre-checkpoint transfers are already folded into the baseline:
        # replaying them must not double-count (and cannot conflict).
        if seq <= self._base_seq(sender):
            return "stale"
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

    def _base_row(self, node_id: str) -> tuple[float, int, float]:
        row = self._db.execute(
            "SELECT balance, base_seq, earned FROM base WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        return row if row else (GENESIS_CREDITS, 0, 0.0)

    def _base_seq(self, node_id: str) -> int:
        return self._base_row(node_id)[1]

    def has_base(self) -> bool:
        return self._db.execute("SELECT 1 FROM base LIMIT 1").fetchone() is not None

    def balance(self, node_id: str) -> float:
        base_balance, _, _ = self._base_row(node_id)
        incoming = self._db.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM ({self._COUNTED}) WHERE recipient = ?",
            (node_id,),
        ).fetchone()[0]
        outgoing = self._db.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM ({self._COUNTED}) WHERE sender = ?",
            (node_id,),
        ).fetchone()[0]
        return _round(base_balance + incoming - outgoing)

    def total_earned(self, node_id: str) -> float:
        """Lifetime counted incoming credits - what ranks are made of."""
        _, _, base_earned = self._base_row(node_id)
        earned = self._db.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM ({self._COUNTED}) WHERE recipient = ?",
            (node_id,),
        ).fetchone()[0]
        return _round(base_earned + earned)

    def is_flagged(self, node_id: str) -> bool:
        """True if the account has provably double-spent or is overdrawn."""
        if self._db.execute("SELECT 1 FROM flagged_base WHERE node_id = ?",
                            (node_id,)).fetchone():
            return True
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

    # -- checkpointing ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """A deterministic, content-only summary of the ledger state.

        Replicas that have converged on the same transaction set produce a
        byte-identical snapshot, so its hash can be compared across multiple
        independent sources before a new ship adopts it (fast bootstrap)
        and old transactions can be pruned without losing balances, spend
        sequences, lifetime earnings or double-spend verdicts.
        """
        accounts: dict[str, list[float | int]] = {}

        def slot(node_id: str) -> list[float | int]:
            if node_id not in accounts:
                balance, seq, earned = self._base_row(node_id)
                accounts[node_id] = [float(balance), int(seq), float(earned)]
            return accounts[node_id]

        for node_id, in self._db.execute("SELECT node_id FROM base"):
            slot(node_id)
        # Deterministic accumulation order: sorted by tx id, never SQL SUM.
        rows = self._db.execute(
            f"SELECT t2.tx_id, t2.sender, t2.seq, t2.recipient, t2.amount "
            f"FROM txs t2 WHERE t2.tx_id IN (SELECT MIN(tx_id) FROM txs"
            f"  GROUP BY sender, seq) ORDER BY t2.tx_id"
        ).fetchall()
        for _tx_id, sender, seq, recipient, amount in rows:
            s = slot(sender)
            s[0] -= amount
            s[1] = max(s[1], seq)
            r = slot(recipient)
            r[0] += amount
            r[2] += amount
        flagged = {node_id for (node_id,) in
                   self._db.execute("SELECT node_id FROM flagged_base")}
        flagged.update(self.double_spenders())
        return {
            "v": 1,
            "accounts": {nid: [_round(b), int(q), _round(e)]
                         for nid, (b, q, e) in sorted(accounts.items())},
            "flagged": sorted(flagged),
        }

    def adopt_snapshot(self, snap: dict[str, Any]) -> None:
        """Fast bootstrap: install a snapshot as this empty replica's base."""
        if self.tx_count() > 0 or self.has_base():
            raise ValueError("can only adopt a snapshot into an empty ledger")
        if snap.get("v") != 1 or not isinstance(snap.get("accounts"), dict):
            raise ValueError("malformed snapshot")
        for node_id, values in snap["accounts"].items():
            balance, seq, earned = float(values[0]), int(values[1]), float(values[2])
            self._db.execute(
                "INSERT INTO base (node_id, balance, base_seq, earned)"
                " VALUES (?, ?, ?, ?)", (node_id, balance, seq, earned))
        for node_id in snap.get("flagged", []):
            self._db.execute(
                "INSERT OR IGNORE INTO flagged_base (node_id) VALUES (?)",
                (str(node_id),))
        self._db.commit()

    def prune(self) -> int:
        """Fold all transactions into the baseline and drop their bodies.
        Balances, seqs, earnings and flags survive; history does not -
        peers that still need it fetch the snapshot instead. Returns the
        number of pruned transactions."""
        snap = self.snapshot()
        pruned = self.tx_count()
        self._db.execute("DELETE FROM base")
        self._db.execute("DELETE FROM flagged_base")
        self._db.execute("DELETE FROM txs")
        self._db.commit()
        # reuse the adoption path for consistency
        for node_id, values in snap["accounts"].items():
            self._db.execute(
                "INSERT INTO base (node_id, balance, base_seq, earned)"
                " VALUES (?, ?, ?, ?)",
                (node_id, float(values[0]), int(values[1]), float(values[2])))
        for node_id in snap["flagged"]:
            self._db.execute(
                "INSERT OR IGNORE INTO flagged_base (node_id) VALUES (?)", (node_id,))
        self._db.commit()
        return pruned
