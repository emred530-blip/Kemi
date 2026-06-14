"""Local end-to-end demonstration of the decentralised fleet.

Spins up, in one process and with no central component:

* a bootstrap peer (an ordinary node - it also serves as a relay),
* two honest direct providers with different prices,
* one provider "behind NAT" that relays through the bootstrap peer,
* one *dishonest* provider that returns fabricated results,
* a consumer that runs a redundancy-verified hash job, an AI job and a
  pipeline job.

Then it shows that the signed payments propagated by gossip until every
replica agrees on every balance - without any tracker or ledger server.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .consumer import Consumer, Job
from .gossip_ledger import GENESIS_CREDITS
from .identity import Identity
from .names import ship_name
from .node import PeerNode


class DishonestNode(PeerNode):
    """Computes nothing, returns garbage - but still takes the payment."""

    async def _execute_chunk(self, task: str, items: list[Any], params: dict) -> list[Any]:
        return ["bogus-result"] * len(items)


async def _wait_for_convergence(nodes: list[PeerNode], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for node in nodes:
            await node.sync_ledger()
        counts = {node.ledger.tx_count() for node in nodes}
        if len(counts) == 1:
            return True
        await asyncio.sleep(0.5)
    return False


async def run_demo(ui_port: int | None = None) -> int:
    print("=== kemi demo: a decentralised local fleet (no tracker) ===\n")

    print("[1/7] launching the bootstrap peer (an ordinary node; also the relay)...")
    bootstrap = PeerNode(Identity.create(), host="127.0.0.1", port=0)
    await bootstrap.start()
    peers = [("127.0.0.1", bootstrap.port)]
    print(f"      port {bootstrap.port} (tcp+udp), ship {ship_name(bootstrap.identity.node_id)}")

    print("[2/7] providers joining the DHT...")
    p_cheap = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                       provide=True, price=0.5)
    p_mid = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                     provide=True, price=1.0)
    p_nat = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                     provide=True, price=1.2, force_relay=True)
    p_liar = DishonestNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                           provide=True, price=0.1)
    providers = [p_cheap, p_mid, p_nat, p_liar]
    for node in providers:
        await node.start()
    await asyncio.sleep(0.3)  # let the relay session establish
    print(f"      honest: {ship_name(p_cheap.identity.node_id)} (0.5 cr), "
          f"{ship_name(p_mid.identity.node_id)} (1.0 cr)")
    print(f"      behind NAT (via relay): {ship_name(p_nat.identity.node_id)} (1.2 cr)")
    print(f"      DISHONEST (fabricates results): {ship_name(p_liar.identity.node_id)} (0.1 cr)")

    consumer_node = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers)
    await consumer_node.start()
    consumer = Consumer(consumer_node)
    print(f"[3/7] consumer {ship_name(consumer_node.identity.node_id)} joined, "
          f"balance {await consumer.balance():.2f} credits (genesis)")

    print("\n[4/7] hash.sha256 job: 24 items, 6 chunks, redundancy=2 (cross-checked)...")
    report = await consumer.run_job(Job(
        task="hash.sha256",
        items=[f"block-{i}" for i in range(24)],
        params={"rounds": 1000},
        chunk_size=4,
        redundancy=2,
    ))
    liar_paid = consumer_node.ledger.balance(p_liar.identity.node_id) - GENESIS_CREDITS
    print(f"      {len(report.results)} results verified; {report.spent:.2f} credits spent")
    print(f"      all traffic was end-to-end encrypted ({report.encrypted_chunks} chunk "
          f"executions; nobody on the path, relay included, could read it)")
    print(f"      the (bounded) amount the cheat kept for bogus results: {liar_paid:.2f} credits")
    liar_score = consumer_node.reputation.score(p_liar.identity.node_id)
    print(f"      the cheat's reputation in the consumer's eyes: {liar_score:.2f} "
          f"(banned: {consumer_node.reputation.is_banned(p_liar.identity.node_id)})")

    print("\n[5/7] ai.generate job running on the fleet...")
    prompts = ["Why do P2P networks matter?", "What is the future of data centres?"]
    ai_report = await consumer.run_job(Job(
        task="ai.generate",
        items=prompts,
        params={"max_tokens": 12},
        chunk_size=1,
    ))
    for prompt, completion in zip(prompts, ai_report.results):
        print(f"      {prompt!r} -> {completion!r}")

    print("\n[6/7] SHARDED big-model inference: one transformer's layers split")
    print("      across distinct ships — no single machine holds the model...")
    from .model import ModelSpec
    from .sharded import ShardedLLM

    spec = ModelSpec(n_layers=12)
    llm = ShardedLLM(consumer, spec)
    plan = await llm.make_plan()
    print(f"      {spec.n_layers}-layer model spread over {plan.ships} ships:")
    print(f"        {plan.describe()}")
    report = await llm.generate("the fleet says", max_tokens=6)
    print(f"      generated {len(report.tokens)} tokens for {report.spent:.2f} credits; "
          f"no ship ever held all {spec.n_layers} layers")

    print("\n[7/7] waiting for gossip to spread; comparing replicas...")
    everyone = [bootstrap, *providers, consumer_node]
    converged = await _wait_for_convergence(everyone)
    print(f"      all {len(everyone)} replicas converged to the same transaction set: {converged}")

    labels = {
        consumer_node.identity.node_id: "consumer",
        p_cheap.identity.node_id: "provider (0.5 cr)",
        p_mid.identity.node_id: "provider (1.0 cr)",
        p_nat.identity.node_id: "provider (NAT/relay)",
        p_liar.identity.node_id: "provider (dishonest)",
    }
    print(f"\n      balances - read from two DIFFERENT replicas "
          f"(genesis {GENESIS_CREDITS:.0f} credits):")
    print(f"      {'account':24s} {'bootstrap replica':>20s} {'consumer replica':>20s}")
    for node_id, label in labels.items():
        a = bootstrap.ledger.balance(node_id)
        b = consumer_node.ledger.balance(node_id)
        mark = "✓" if abs(a - b) < 1e-6 else "✗"
        print(f"      {label:24s} {a:20.2f} {b:20.2f}  {mark}")

    print("\ndemo complete: discovery via DHT, payment via signed transfers,")
    print("verification via majority voting - no central component anywhere.")

    if ui_port is not None:
        from .webui import WebUI

        ui = WebUI(consumer_node, port=ui_port)
        await ui.start()
        print(f"\nthe fleet keeps sailing - live dashboard: {ui.url}")
        print("(you can submit jobs from the dashboard; Ctrl+C to stop)")
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await ui.stop()

    for node in [*providers, consumer_node, bootstrap]:
        await node.stop()
    return 0
