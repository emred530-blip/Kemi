"""Invite codes: joining the fleet with one copy-paste.

An invite packs one or more bootstrap peers (and an optional note) into a
short, URL-safe code:

    kemi1-mjqxgzlnnfuxizltfqqgs3djga2tqmrygaxxezltfqqgs3dj...

Anyone can mint one (`kemi davet`), anyone can join with one
(`kemi katil --davet KOD`). The code carries connectivity only - no
secrets, no economy - so sharing it publicly is safe.
"""

from __future__ import annotations

import base64
import json

PREFIX = "kemi1-"


class InviteError(Exception):
    pass


def make_invite(peers: list[tuple[str, int]], note: str = "") -> str:
    if not peers:
        raise InviteError("an invite needs at least one peer")
    payload = {"v": 1, "p": [[host, int(port)] for host, port in peers]}
    if note:
        payload["n"] = note[:80]
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    code = base64.b32encode(raw).decode("ascii").rstrip("=").lower()
    return PREFIX + code


def parse_invite(code: str) -> dict:
    """Returns {"peers": [(host, port), ...], "note": str}. Tolerant of
    case, surrounding whitespace and cosmetic dashes."""
    code = code.strip().lower()
    if code.startswith(PREFIX):
        code = code[len(PREFIX):]
    code = code.replace("-", "").replace(" ", "").upper()
    padding = "=" * (-len(code) % 8)
    try:
        raw = base64.b32decode(code + padding)
        payload = json.loads(raw.decode("utf-8"))
        peers = [(str(host), int(port)) for host, port in payload["p"]]
        if not peers:
            raise ValueError("empty peer list")
        for _, port in peers:
            if not (0 < port < 65536):
                raise ValueError("bad port")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InviteError(f"geçersiz davet kodu: {exc}")
    return {"peers": peers, "note": str(payload.get("n", ""))}
