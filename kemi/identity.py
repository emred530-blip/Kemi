"""Node identity: Ed25519 keypair + proof-of-work node id.

``node_id = sha256(public_key || pow_nonce)`` and must start with
``POW_DIFFICULTY_BITS`` zero bits. Minting an identity therefore costs CPU
work, which raises the price of Sybil attacks against the genesis-credit
faucet and the DHT. Every peer independently verifies the proof before
trusting a node id.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .crypto import SigningKey

# Network-wide constant: leading zero bits required of a node id. Raising it
# makes identities more expensive to mint (each bit doubles the work).
POW_DIFFICULTY_BITS = 12


def _node_id(public_key: bytes, nonce: int) -> str:
    return hashlib.sha256(public_key + nonce.to_bytes(8, "big")).hexdigest()


def _leading_zero_bits(hex_digest: str) -> int:
    value = int(hex_digest, 16)
    return 256 - value.bit_length()


def verify_node_id(node_id: str, public_key_hex: str, pow_nonce: int,
                   difficulty: int = POW_DIFFICULTY_BITS) -> bool:
    """Check that a node id is correctly derived and meets the PoW target."""
    try:
        public_key = bytes.fromhex(public_key_hex)
    except ValueError:
        return False
    if len(public_key) != 32 or not isinstance(pow_nonce, int) or pow_nonce < 0:
        return False
    derived = _node_id(public_key, pow_nonce)
    return derived == node_id and _leading_zero_bits(derived) >= difficulty


@dataclass(frozen=True)
class Identity:
    key: SigningKey
    pow_nonce: int
    node_id: str

    @property
    def short_id(self) -> str:
        return self.node_id[:12]

    @property
    def public_key_hex(self) -> str:
        return self.key.public_key_hex

    @classmethod
    def create(cls, difficulty: int = POW_DIFFICULTY_BITS) -> "Identity":
        key = SigningKey.generate()
        nonce = 0
        while True:
            node_id = _node_id(key.public_key, nonce)
            if _leading_zero_bits(node_id) >= difficulty:
                return cls(key=key, pow_nonce=nonce, node_id=node_id)
            nonce += 1

    @classmethod
    def load_or_create(cls, path: str | Path,
                       difficulty: int = POW_DIFFICULTY_BITS) -> "Identity":
        path = Path(path).expanduser()
        if path.exists():
            data = json.loads(path.read_text())
            if "seed" not in data:
                raise ValueError(
                    f"{path} is a legacy (v1) identity; delete it to mint a new "
                    "Ed25519 identity"
                )
            key = SigningKey.from_seed_hex(data["seed"])
            identity = cls(key=key, pow_nonce=int(data["pow_nonce"]),
                           node_id=data["node_id"])
            if not verify_node_id(identity.node_id, identity.public_key_hex,
                                  identity.pow_nonce, difficulty):
                raise ValueError(f"corrupt or under-difficulty identity file: {path}")
            return identity
        identity = cls.create(difficulty)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 2,
            "seed": identity.key.seed_hex,
            "pow_nonce": identity.pow_nonce,
            "node_id": identity.node_id,
        }, indent=2))
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return identity


DEFAULT_IDENTITY_PATH = "~/.kemi/identity.json"
