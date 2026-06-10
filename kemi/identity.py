"""Node identity.

A node is identified by ``node_id = sha256(secret_token)``. Any service can
verify ownership of a node id by checking the hash of the presented token,
without keeping a credential database. (Roadmap: replace with Ed25519
keypairs and signed messages.)
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path


def node_id_for_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(node_id: str, token: str) -> bool:
    return secrets.compare_digest(node_id_for_token(token), node_id)


@dataclass(frozen=True)
class Identity:
    node_id: str
    token: str

    @property
    def short_id(self) -> str:
        return self.node_id[:12]

    @classmethod
    def create(cls) -> "Identity":
        token = secrets.token_hex(32)
        return cls(node_id=node_id_for_token(token), token=token)

    @classmethod
    def load_or_create(cls, path: str | Path) -> "Identity":
        path = Path(path).expanduser()
        if path.exists():
            data = json.loads(path.read_text())
            identity = cls(node_id=data["node_id"], token=data["token"])
            if not verify_token(identity.node_id, identity.token):
                raise ValueError(f"corrupt identity file: {path}")
            return identity
        identity = cls.create()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"node_id": identity.node_id, "token": identity.token}, indent=2)
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return identity


DEFAULT_IDENTITY_PATH = "~/.kemi/identity.json"
