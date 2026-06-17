"""Launch a real multi-process Kemi fleet on the local network and drive it.

Unlike `kemi demo` (one process, in-memory), this starts the bootstrap peer
and each provider as a **separate OS process** (`python -m kemi node`) bound
to its own TCP+UDP port on 127.0.0.1, then connects as a consumer over real
sockets: discovery, a paid compute job, and a sharded-model generation that
spans several provider processes — none holding the whole model.

    python3 scripts/lan_demo.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

import kemi
from kemi.model import ModelSpec
from kemi.names import ship_name
from kemi.protocol import NETWORK_ERRORS, request

HOST = "127.0.0.1"
BOOT_PORT = 7700
PROVIDERS = [(7701, 0.5), (7702, 1.0), (7703, 1.5)]


def _spawn_node(workdir: str, port: int, *, provide: bool, price: float = 1.0):
    name = f"node{port}"
    args = [sys.executable, "-m", "kemi", "node", "--host", HOST, "--port", str(port),
            "--identity", os.path.join(workdir, f"{name}.json"),
            "--ledger", os.path.join(workdir, f"{name}.db"),
            "--reputation", os.path.join(workdir, f"{name}-rep.db"),
            "--no-sandbox"]
    if port != BOOT_PORT:
        args += ["--peer", f"{HOST}:{BOOT_PORT}"]
    if provide:
        args += ["--provide", "--price", str(price)]
    log = open(os.path.join(workdir, f"{name}.log"), "w")
    return subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)


async def _wait_until_up(port: int, timeout: float = 30.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            info = await request(HOST, port, {"type": "node.info"}, timeout=2.0)
            if info.get("ok"):
                return info
        except NETWORK_ERRORS:
            pass
        await asyncio.sleep(0.3)
    raise TimeoutError(f"node on port {port} never came up")


async def main() -> int:
    workdir = tempfile.mkdtemp(prefix="kemi-lan-")
    procs = []
    by_port: dict[int, subprocess.Popen] = {}
    print("=== Kemi LAN demo — real separate processes over 127.0.0.1 ===\n")
    try:
        print("[1] launching the bootstrap peer (its own OS process)...")
        boot_proc = _spawn_node(workdir, BOOT_PORT, provide=False)
        procs.append(boot_proc)
        boot = await _wait_until_up(BOOT_PORT)
        print(f"    ⚓ {ship_name(boot['node_id'])} listening on {HOST}:{BOOT_PORT} "
              f"(pid {boot_proc.pid})")

        print("\n[2] launching 3 provider processes, each joins via the bootstrap peer...")
        for port, price in PROVIDERS:
            proc = _spawn_node(workdir, port, provide=True, price=price)
            procs.append(proc)
            by_port[port] = proc
        for port, price in PROVIDERS:
            info = await _wait_until_up(port)
            print(f"    ⚓ {ship_name(info['node_id'])} on :{port} "
                  f"({price} cr/item, pid {by_port[port].pid})")

        print("\n[3] connecting as a consumer over real sockets "
              "(bootstrapping through the DHT)...")
        async with kemi.connect(peer=f"{HOST}:{BOOT_PORT}") as fleet:
            # discovery must converge through the DHT across processes
            providers = []
            for _ in range(20):
                providers = await fleet.providers("hash.sha256")
                if len(providers) >= 3:
                    break
                await asyncio.sleep(0.5)
            print(f"    discovered {len(providers)} providers across the fleet:")
            for p in sorted(providers, key=lambda p: p["price"]):
                print(f"      {ship_name(p['node_id'])}  {p['host']}:{p['port']}  "
                      f"{p['price']:.1f} cr/item")
            print(f"    my ship: {fleet.ship} ({fleet.rank()}), "
                  f"balance {await fleet.balance():.1f} cr")

            print("\n[4] running a real paid compute job across the processes...")
            report = await fleet.run("hash.sha256", [f"block-{i}" for i in range(12)],
                                     chunk_size=2, redundancy=2, full_report=True)
            print(f"    {len(report.results)} results, {report.chunks} chunks, "
                  f"redundancy=2 cross-checked; spent {report.spent:.1f} cr "
                  f"across {len(report.providers_used)} provider process(es)")

            print("\n[5] sharded big-model inference across the provider processes...")
            spec = ModelSpec(n_layers=9)
            shard = await fleet.shard_generate("the fleet says", max_tokens=5, spec=spec)
            print(f"    {spec.n_layers}-layer model spread over {shard.plan.ships} ships:")
            print(f"      {shard.plan.describe()}")
            print(f"    generated {len(shard.tokens)} tokens for {shard.spent:.1f} cr; "
                  f"no single process held all {spec.n_layers} layers")
            print(f"    final balance: {await fleet.balance():.1f} cr")

        print("\n=== success: a real multi-process fleet discovered peers, "
              "settled payments\n    and ran a sharded model — all over live sockets ===")
        return 0
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
