"""Ed25519 signatures and canonical-JSON signed envelopes.

Uses PyNaCl (libsodium) when available and falls back to a pure-Python
implementation of RFC 8032 otherwise, so a node can join the swarm with
nothing but the Python standard library. Both backends are interoperable:
signatures produced by one verify under the other.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

try:  # fast path: libsodium via PyNaCl
    import nacl.exceptions
    import nacl.signing

    HAVE_NACL = True
except ImportError:  # pragma: no cover - exercised on stdlib-only hosts
    HAVE_NACL = False


# ---------------------------------------------------------------------------
# Pure-Python Ed25519 (RFC 8032 reference algorithm). Slow but dependency-free.
# ---------------------------------------------------------------------------

_p = 2**255 - 19
_q = 2**252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_I = pow(2, (_p - 1) // 4, _p)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _inv(x: int) -> int:
    return pow(x, _p - 2, _p)


def _edwards_add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _p
    b = (y1 + x1) * (y2 + x2) % _p
    c = t1 * 2 * _d * t2 % _p
    dd = z1 * 2 * z2 % _p
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _p, g * h % _p, f * g % _p, e * h % _p)


def _scalarmult(P, e: int):
    Q = (0, 1, 1, 0)
    while e > 0:
        if e & 1:
            Q = _edwards_add(Q, P)
        P = _edwards_add(P, P)
        e >>= 1
    return Q


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _p:
        return None
    xx = (y * y - 1) * _inv(_d * y * y + 1) % _p
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = x * _I % _p
    if (x * x - xx) % _p != 0:
        return None
    if x & 1 != sign:
        x = _p - x
    return x


_By = 4 * _inv(5) % _p
_Bx = _recover_x(_By, 0)
assert _Bx is not None
_B = (_Bx, _By, 1, _Bx * _By % _p)


def _point_compress(P) -> bytes:
    x, y, z, _ = P
    zinv = _inv(z)
    x, y = x * zinv % _p, y * zinv % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _point_decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _p)


def _point_equal(P, Q) -> bool:
    return (P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _secret_expand(seed: bytes) -> tuple[int, bytes]:
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def _pure_public_key(seed: bytes) -> bytes:
    a, _ = _secret_expand(seed)
    return _point_compress(_scalarmult(_B, a))


def _pure_sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _secret_expand(seed)
    public = _point_compress(_scalarmult(_B, a))
    r = int.from_bytes(_sha512(prefix + message), "little") % _q
    R = _point_compress(_scalarmult(_B, r))
    h = int.from_bytes(_sha512(R + public + message), "little") % _q
    s = (r + h * a) % _q
    return R + int.to_bytes(s, 32, "little")


def _pure_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _point_decompress(public)
    R = _point_decompress(signature[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _q:
        return False
    h = int.from_bytes(_sha512(signature[:32] + public + message), "little") % _q
    return _point_equal(_scalarmult(_B, s), _edwards_add(R, _scalarmult(A, h)))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class SigningKey:
    """An Ed25519 keypair derived from a 32-byte seed."""

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise ValueError("seed must be 32 bytes")
        self._seed = seed
        if HAVE_NACL:
            self._key = nacl.signing.SigningKey(seed)
            self.public_key = bytes(self._key.verify_key)
        else:
            self._key = None
            self.public_key = _pure_public_key(seed)

    @classmethod
    def generate(cls) -> "SigningKey":
        return cls(secrets.token_bytes(32))

    @classmethod
    def from_seed_hex(cls, seed_hex: str) -> "SigningKey":
        return cls(bytes.fromhex(seed_hex))

    @property
    def seed_hex(self) -> str:
        return self._seed.hex()

    @property
    def public_key_hex(self) -> str:
        return self.public_key.hex()

    def sign(self, message: bytes) -> bytes:
        if self._key is not None:
            return self._key.sign(message).signature
        return _pure_sign(self._seed, message)


def verify_signature(public_key: bytes, message: bytes, signature: bytes) -> bool:
    if HAVE_NACL:
        try:
            nacl.signing.VerifyKey(public_key).verify(message, signature)
            return True
        except (nacl.exceptions.CryptoError, ValueError):
            return False
    return _pure_verify(public_key, message, signature)


def canonical(obj: Any) -> bytes:
    """Deterministic JSON encoding used for all signatures and hashes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def sign_envelope(key: SigningKey, payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap ``payload`` in a signed envelope verifiable by anyone."""
    return {
        "payload": payload,
        "pubkey": key.public_key_hex,
        "sig": key.sign(canonical(payload)).hex(),
    }


def open_envelope(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Return the payload if the envelope's signature is valid, else None."""
    try:
        payload = envelope["payload"]
        pubkey = bytes.fromhex(envelope["pubkey"])
        sig = bytes.fromhex(envelope["sig"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if not verify_signature(pubkey, canonical(payload), sig):
        return None
    return payload
