"""``kemi doctor``: diagnose this machine's readiness for the fleet.

Runs a battery of local and network checks and prints a ✓/✗ report with
actionable hints - the first thing to reach for when something feels off.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path
from typing import Any

CRITICAL_FAILURES = {"python", "crypto", "identity", "tasks"}


def _check(name: str, ok: bool, detail: str, hint: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "hint": hint}


async def run_checks(peer: tuple[str, int] | None = None,
                     identity_path: str | None = None) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    # Python version ---------------------------------------------------------
    version_ok = sys.version_info >= (3, 10)
    checks.append(_check(
        "python", version_ok,
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "" if version_ok else "Kemi needs Python 3.10+ - https://python.org/downloads"))

    # Crypto backend -----------------------------------------------------------
    from .crypto import HAVE_NACL, SigningKey, verify_signature

    key = SigningKey.generate()
    signature_ok = verify_signature(key.public_key, b"doctor", key.sign(b"doctor"))
    backend = "libsodium (PyNaCl)" if HAVE_NACL else "pure Python (slow but fine)"
    checks.append(_check(
        "crypto", signature_ok, f"Ed25519 via {backend}",
        "" if HAVE_NACL else "speed it up with: pip install pynacl"))

    # Identity -----------------------------------------------------------------
    from .identity import DEFAULT_IDENTITY_PATH, Identity, verify_node_id
    from .names import ship_name

    path = Path(identity_path or DEFAULT_IDENTITY_PATH).expanduser()
    try:
        if path.exists():
            identity = Identity.load_or_create(path)
            checks.append(_check("identity", True,
                                 f"ship '{ship_name(identity.node_id)}' at {path}"))
        else:
            identity = Identity.create()
            ok = verify_node_id(identity.node_id, identity.public_key_hex,
                                identity.pow_nonce)
            checks.append(_check("identity", ok,
                                 "no identity file yet - minting works "
                                 f"(a new one appears on first 'kemi join')"))
    except (ValueError, OSError) as exc:
        identity = Identity.create()
        checks.append(_check("identity", False, f"identity file problem: {exc}",
                             f"move or delete {path} and re-run kemi join"))

    # Task registry + sandbox -----------------------------------------------------
    from .tasks import TASKS, run_task

    try:
        result = run_task("hash.sha256", ["doctor"], {}, {})
        checks.append(_check("tasks", bool(result),
                             f"{len(TASKS)} task types registered, execution OK"))
    except Exception as exc:
        checks.append(_check("tasks", False, f"task execution failed: {exc}"))

    try:
        from .sandbox import run_sandboxed

        await asyncio.wait_for(run_sandboxed("hash.sha256", ["doctor"], {},
                                             timeout=20.0), timeout=25.0)
        checks.append(_check("sandbox", True, "rlimit subprocess isolation works"))
    except Exception as exc:
        checks.append(_check("sandbox", False, f"sandbox failed: {exc}",
                             "providers can still run with --no-sandbox"))

    # LAN multicast ---------------------------------------------------------------
    from . import lan

    beacon = lan.LanBeacon("f" * 64, tcp_port=1)
    await beacon.start()
    try:
        found = await lan.discover(own_id="0" * 64, timeout=1.0)
    finally:
        await beacon.stop()
    lan_ok = any(port == 1 for _, port in found)
    checks.append(_check(
        "lan", lan_ok,
        "multicast discovery works on this network" if lan_ok
        else "multicast blocked (common on guest Wi-Fi / some VPNs)",
        "" if lan_ok else "use invite codes or --peer instead of LAN discovery"))

    # GPU ----------------------------------------------------------------------------
    from .node import detect_gpus

    gpus = detect_gpus()
    checks.append(_check("gpu", True,
                         f"{len(gpus)} GPU(s): " + ", ".join(g["name"] for g in gpus)
                         if gpus else "no NVIDIA GPU detected (CPU tasks only)"))

    # Ollama -------------------------------------------------------------------------
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2):
            checks.append(_check("ollama", True,
                                 "Ollama server running - real LLM inference available"))
    except Exception:
        checks.append(_check(
            "ollama", True, "Ollama not running (mock AI backend will be used)",
            "for real models: install https://ollama.com and `ollama pull llama3.2`"))

    # ffmpeg-style capability probes --------------------------------------------------
    try:
        import numpy  # noqa: F401

        checks.append(_check("numpy", True, "numpy present - sci.matmul advertised"))
    except ImportError:
        checks.append(_check("numpy", True, "numpy absent (sci.matmul not offered)",
                             "optional: pip install numpy"))

    # Peer reachability ----------------------------------------------------------------
    if peer is not None:
        from .protocol import request

        started = time.monotonic()
        try:
            info = await request(peer[0], peer[1], {"type": "node.info"}, timeout=5.0)
            ms = (time.monotonic() - started) * 1000
            if info.get("ok"):
                checks.append(_check(
                    "peer", True,
                    f"{peer[0]}:{peer[1]} reachable in {ms:.0f} ms - "
                    f"ledger {info.get('ledger_txs')} txs, "
                    f"{info.get('dht_contacts')} DHT contacts"))
            else:
                checks.append(_check("peer", False,
                                     f"{peer[0]}:{peer[1]} answered with an error"))
        except Exception as exc:
            checks.append(_check("peer", False,
                                 f"{peer[0]}:{peer[1]} unreachable: {exc}",
                                 "check the address, the port and any firewall"))
    return checks


def render(checks: list[dict[str, Any]]) -> int:
    width = max(len(c["name"]) for c in checks)
    failed_critical = False
    for c in checks:
        mark = "✓" if c["ok"] else "✗"
        print(f"  {mark} {c['name']:<{width}}  {c['detail']}")
        if c["hint"]:
            print(f"    {'':<{width}}  ↳ {c['hint']}")
        if not c["ok"] and c["name"] in CRITICAL_FAILURES:
            failed_critical = True
    print()
    if failed_critical:
        print("✗ critical problems found - fix the items above before sailing")
        return 1
    if all(c["ok"] for c in checks):
        print("⚓ all clear - this machine is ready to sail")
    else:
        print("⚓ seaworthy with notes - non-critical items above are optional")
    return 0
