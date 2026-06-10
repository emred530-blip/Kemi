"""Credit ledger with escrow support, backed by SQLite.

Credits are the unit of exchange in the network: providers earn them by
executing chunks, consumers spend them to rent compute. Payments flow through
escrows so a consumer can pre-fund a job and providers can redeem their share
per completed chunk without trusting each other.

The ledger currently lives on the tracker (centralised for the MVP); the
interface is deliberately small so it can later be replaced by a distributed
ledger.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass

# Faucet: every new account starts with this many credits so fresh nodes can
# participate immediately (tit-for-tat bootstrap, like BitTorrent's optimistic
# unchoke).
STARTING_BALANCE = 100.0


class LedgerError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


@dataclass(frozen=True)
class Escrow:
    escrow_id: str
    owner: str
    remaining: float
    open: bool


def _round(amount: float) -> float:
    return round(float(amount), 6)


class Ledger:
    def __init__(self, db_path: str = ":memory:"):
        self._db = sqlite3.connect(db_path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                node_id TEXT PRIMARY KEY,
                balance REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS escrows (
                escrow_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                redeem_key_hash TEXT NOT NULL,
                remaining REAL NOT NULL,
                open INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def ensure_account(self, node_id: str) -> float:
        """Create the account with the faucet balance if new; return balance."""
        row = self._db.execute(
            "SELECT balance FROM accounts WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is not None:
            return row[0]
        self._db.execute(
            "INSERT INTO accounts (node_id, balance) VALUES (?, ?)",
            (node_id, STARTING_BALANCE),
        )
        self._db.commit()
        return STARTING_BALANCE

    def balance(self, node_id: str) -> float:
        row = self._db.execute(
            "SELECT balance FROM accounts WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None:
            raise LedgerError("no_account", f"unknown account {node_id[:12]}")
        return row[0]

    def escrow_create(self, owner: str, amount: float) -> tuple[str, str]:
        """Lock ``amount`` of the owner's credits; return (escrow_id, redeem_key).

        The redeem key is handed by the consumer to providers along with each
        chunk; presenting it authorises redeeming part of the escrow.
        """
        amount = _round(amount)
        if amount <= 0:
            raise LedgerError("bad_amount", "escrow amount must be positive")
        balance = self.balance(owner)
        if balance < amount:
            raise LedgerError(
                "insufficient_funds", f"balance {balance} < escrow {amount}"
            )
        escrow_id = secrets.token_hex(8)
        redeem_key = secrets.token_hex(16)
        key_hash = hashlib.sha256(redeem_key.encode()).hexdigest()
        self._db.execute(
            "UPDATE accounts SET balance = balance - ? WHERE node_id = ?",
            (amount, owner),
        )
        self._db.execute(
            "INSERT INTO escrows (escrow_id, owner, redeem_key_hash, remaining, open)"
            " VALUES (?, ?, ?, ?, 1)",
            (escrow_id, owner, key_hash, amount),
        )
        self._db.commit()
        return escrow_id, redeem_key

    def escrow_redeem(self, escrow_id: str, redeem_key: str, payee: str, amount: float) -> float:
        """Move ``amount`` from the escrow to ``payee``. Returns new payee balance."""
        amount = _round(amount)
        if amount <= 0:
            raise LedgerError("bad_amount", "redeem amount must be positive")
        row = self._db.execute(
            "SELECT redeem_key_hash, remaining, open FROM escrows WHERE escrow_id = ?",
            (escrow_id,),
        ).fetchone()
        if row is None:
            raise LedgerError("no_escrow", f"unknown escrow {escrow_id}")
        key_hash, remaining, is_open = row
        if not is_open:
            raise LedgerError("escrow_closed", f"escrow {escrow_id} is closed")
        presented = hashlib.sha256(redeem_key.encode()).hexdigest()
        if not secrets.compare_digest(presented, key_hash):
            raise LedgerError("bad_redeem_key", "redeem key does not match")
        if amount > remaining + 1e-9:
            raise LedgerError(
                "insufficient_escrow", f"escrow remaining {remaining} < {amount}"
            )
        self.ensure_account(payee)
        self._db.execute(
            "UPDATE escrows SET remaining = remaining - ? WHERE escrow_id = ?",
            (amount, escrow_id),
        )
        self._db.execute(
            "UPDATE accounts SET balance = balance + ? WHERE node_id = ?",
            (amount, payee),
        )
        self._db.commit()
        return self.balance(payee)

    def escrow_release(self, escrow_id: str, owner: str) -> float:
        """Close the escrow and refund the unspent remainder to the owner."""
        row = self._db.execute(
            "SELECT owner, remaining, open FROM escrows WHERE escrow_id = ?",
            (escrow_id,),
        ).fetchone()
        if row is None:
            raise LedgerError("no_escrow", f"unknown escrow {escrow_id}")
        escrow_owner, remaining, is_open = row
        if escrow_owner != owner:
            raise LedgerError("not_owner", "only the escrow owner can release it")
        if not is_open:
            raise LedgerError("escrow_closed", f"escrow {escrow_id} is closed")
        self._db.execute(
            "UPDATE escrows SET remaining = 0, open = 0 WHERE escrow_id = ?",
            (escrow_id,),
        )
        self._db.execute(
            "UPDATE accounts SET balance = balance + ? WHERE node_id = ?",
            (remaining, owner),
        )
        self._db.commit()
        return remaining
