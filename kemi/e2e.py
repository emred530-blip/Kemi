"""End-to-end encryption of task payloads (NaCl ``crypto_box`` compatible).

Chunk contents and results are sealed between consumer and provider so that
relays - or anyone on the path - see only ciphertext. Keys are derived from
the Ed25519 identities both sides already have (birational map to X25519),
so no extra key exchange or handshake is needed: knowing a peer's node
identity is enough to encrypt to it.

Construction (identical to libsodium's ``crypto_box_easy``):

    shared  = X25519(my_curve_sk, their_curve_pk)
    key     = HSalsa20(shared, 0)            # "beforenm" precomputed key
    sealed  = Poly1305 tag || XSalsa20-encrypted payload, random 24B nonce

When PyNaCl is installed the fast libsodium path is used; otherwise a
pure-Python implementation of X25519 (RFC 7748), Salsa20, and Poly1305
takes over. Both produce byte-identical output, so mixed swarms
interoperate (verified against each other in the test suite). The pure
fallback is not hardened against side channels - install ``kemi[crypto]``
on hosts where that matters.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from .crypto import HAVE_NACL, _inv, _p, _point_decompress

if HAVE_NACL:
    import nacl.bindings as _sodium


class E2EError(Exception):
    pass


# ---------------------------------------------------------------------------
# X25519 (RFC 7748), pure Python
# ---------------------------------------------------------------------------

_A24 = 121665


def _x25519(scalar: bytes, point: bytes) -> bytes:
    k = int.from_bytes(scalar, "little")
    k &= ~7
    k &= (1 << 254) - 1
    k |= 1 << 254
    u = int.from_bytes(point, "little") & ((1 << 255) - 1)
    x1, x2, z2, x3, z3 = u, 1, 0, u, 1
    swap = 0
    for t in reversed(range(255)):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = bit
        a = (x2 + z2) % _p
        aa = a * a % _p
        b = (x2 - z2) % _p
        bb = b * b % _p
        e = (aa - bb) % _p
        c = (x3 + z3) % _p
        d = (x3 - z3) % _p
        da = d * a % _p
        cb = c * b % _p
        x3 = (da + cb) % _p
        x3 = x3 * x3 % _p
        z3 = (da - cb) % _p
        z3 = z3 * z3 % _p
        z3 = z3 * x1 % _p
        x2 = aa * bb % _p
        z2 = e * (aa + _A24 * e) % _p
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return ((x2 * _inv(z2)) % _p).to_bytes(32, "little")


def _ed25519_pk_to_curve25519(ed_pk: bytes) -> bytes:
    """Birational map from the Edwards y-coordinate to the Montgomery u."""
    point = _point_decompress(ed_pk)
    if point is None:
        raise E2EError("invalid Ed25519 public key")
    _, y, _, _ = point
    u = (1 + y) * _inv((1 - y) % _p) % _p
    return u.to_bytes(32, "little")


def _ed25519_seed_to_curve25519(seed: bytes) -> bytes:
    h = bytearray(hashlib.sha512(seed).digest()[:32])
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return bytes(h)


# ---------------------------------------------------------------------------
# Salsa20 / HSalsa20 / XSalsa20 (djb specification), pure Python
# ---------------------------------------------------------------------------

_SIGMA = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
_MASK = 0xFFFFFFFF
_QUARTER_COLUMNS = ((0, 4, 8, 12), (5, 9, 13, 1), (10, 14, 2, 6), (15, 3, 7, 11))
_QUARTER_ROWS = ((0, 1, 2, 3), (5, 6, 7, 4), (10, 11, 8, 9), (15, 12, 13, 14))


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & _MASK


def _salsa20_rounds(state: list[int]) -> list[int]:
    x = list(state)
    for _ in range(10):
        for quarter in (_QUARTER_COLUMNS, _QUARTER_ROWS):
            for a, b, c, d in quarter:
                x[b] ^= _rotl((x[a] + x[d]) & _MASK, 7)
                x[c] ^= _rotl((x[b] + x[a]) & _MASK, 9)
                x[d] ^= _rotl((x[c] + x[b]) & _MASK, 13)
                x[a] ^= _rotl((x[d] + x[c]) & _MASK, 18)
    return x


def _words(data: bytes) -> list[int]:
    return [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]


def _salsa20_block(key: bytes, nonce8: bytes, counter: int) -> bytes:
    k = _words(key)
    n = _words(nonce8 + counter.to_bytes(8, "little"))
    state = [_SIGMA[0], k[0], k[1], k[2], k[3], _SIGMA[1], n[0], n[1],
             n[2], n[3], _SIGMA[2], k[4], k[5], k[6], k[7], _SIGMA[3]]
    mixed = _salsa20_rounds(state)
    return b"".join(((m + s) & _MASK).to_bytes(4, "little")
                    for m, s in zip(mixed, state))


def _hsalsa20(key: bytes, input16: bytes) -> bytes:
    k = _words(key)
    n = _words(input16)
    state = [_SIGMA[0], k[0], k[1], k[2], k[3], _SIGMA[1], n[0], n[1],
             n[2], n[3], _SIGMA[2], k[4], k[5], k[6], k[7], _SIGMA[3]]
    x = _salsa20_rounds(state)  # no final addition for HSalsa20
    return b"".join(x[i].to_bytes(4, "little") for i in (0, 5, 10, 15, 6, 7, 8, 9))


def _xsalsa20_stream(key: bytes, nonce24: bytes, length: int) -> bytes:
    subkey = _hsalsa20(key, nonce24[:16])
    blocks = []
    for counter in range((length + 63) // 64):
        blocks.append(_salsa20_block(subkey, nonce24[16:], counter))
    return b"".join(blocks)[:length]


# ---------------------------------------------------------------------------
# Poly1305, pure Python
# ---------------------------------------------------------------------------

_P1305 = (1 << 130) - 5
_R_CLAMP = 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF


def _poly1305(message: bytes, key32: bytes) -> bytes:
    r = int.from_bytes(key32[:16], "little") & _R_CLAMP
    s = int.from_bytes(key32[16:32], "little")
    acc = 0
    for i in range(0, len(message), 16):
        block = message[i:i + 16] + b"\x01"
        acc = (acc + int.from_bytes(block, "little")) * r % _P1305
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _secretbox_seal(key: bytes, nonce24: bytes, plaintext: bytes) -> bytes:
    stream = _xsalsa20_stream(key, nonce24, 32 + len(plaintext))
    ciphertext = bytes(p ^ k for p, k in zip(plaintext, stream[32:]))
    tag = _poly1305(ciphertext, stream[:32])
    return tag + ciphertext


def _secretbox_open(key: bytes, nonce24: bytes, sealed: bytes) -> bytes:
    if len(sealed) < 16:
        raise E2EError("sealed box too short")
    tag, ciphertext = sealed[:16], sealed[16:]
    stream = _xsalsa20_stream(key, nonce24, 32 + len(ciphertext))
    expected = _poly1305(ciphertext, stream[:32])
    if not secrets.compare_digest(tag, expected):
        raise E2EError("authentication failed: payload forged or wrong key")
    return bytes(c ^ k for c, k in zip(ciphertext, stream[32:]))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_box_key(my_seed: bytes, their_ed25519_pk: bytes) -> bytes:
    """Precompute the shared box key between two Ed25519 identities.

    Symmetric: ``derive_box_key(a_seed, B_pk) == derive_box_key(b_seed, A_pk)``,
    so requests and responses use the same key with fresh nonces.
    """
    if HAVE_NACL:
        curve_sk = _sodium.crypto_sign_ed25519_sk_to_curve25519(
            my_seed + _sodium.crypto_sign_seed_keypair(my_seed)[0]
        )
        curve_pk = _sodium.crypto_sign_ed25519_pk_to_curve25519(their_ed25519_pk)
        return _sodium.crypto_box_beforenm(curve_pk, curve_sk)
    curve_sk = _ed25519_seed_to_curve25519(my_seed)
    curve_pk = _ed25519_pk_to_curve25519(their_ed25519_pk)
    shared = _x25519(curve_sk, curve_pk)
    return _hsalsa20(shared, b"\x00" * 16)


def seal(key: bytes, plaintext: bytes) -> dict[str, str]:
    """Encrypt+authenticate ``plaintext``; returns a JSON-friendly envelope."""
    nonce = secrets.token_bytes(24)
    if HAVE_NACL:
        boxed = _sodium.crypto_secretbox(plaintext, nonce, key)
    else:
        boxed = _secretbox_seal(key, nonce, plaintext)
    return {"n": nonce.hex(), "c": boxed.hex()}


def open_sealed(key: bytes, envelope: dict[str, Any]) -> bytes:
    """Decrypt a :func:`seal` envelope; raises :class:`E2EError` on forgery."""
    try:
        nonce = bytes.fromhex(envelope["n"])
        boxed = bytes.fromhex(envelope["c"])
    except (KeyError, TypeError, ValueError) as exc:
        raise E2EError(f"malformed sealed envelope: {exc}")
    if len(nonce) != 24:
        raise E2EError("bad nonce length")
    if HAVE_NACL:
        try:
            return _sodium.crypto_secretbox_open(boxed, nonce, key)
        except Exception as exc:
            raise E2EError(f"authentication failed: {exc}")
    return _secretbox_open(key, nonce, boxed)
