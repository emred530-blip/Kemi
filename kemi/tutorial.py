"""Interactive tutorial: learn Kemi by sailing a real (local) fleet.

``kemi learn`` walks a newcomer through the whole idea in ~3 minutes with a
live in-process swarm - identity, discovery, paying for compute, streaming
AI and ranks. ``--fast`` skips the keypresses so the tour can run
unattended (and be tested).
"""

from __future__ import annotations

import asyncio

from .consumer import Consumer, Job
from .identity import Identity
from .invite import make_invite
from .names import BANNER, rank_for, ship_name
from .node import PeerNode


def _say(text: str = "") -> None:
    print(text)


async def _pause(fast: bool) -> None:
    if fast:
        return
    await asyncio.to_thread(input, "\n  [press Enter to continue] ")
    print()


async def run_tutorial(fast: bool = False) -> int:
    _say(BANNER)
    _say("  Welcome aboard! This 5-step tour teaches Kemi on a real, working")
    _say("  fleet - everything happens locally on your machine, right now.")
    await _pause(fast)

    # ------------------------------------------------------------ 1: identity
    _say("STEP 1/5 — Your identity: your ship")
    identity = Identity.create()
    name = ship_name(identity.node_id)
    _say("  An Ed25519 keypair was generated for you, plus a small proof-of-work")
    _say("  (minting identities must not be free - that is the Sybil defence).")
    _say(f"  Your ship:  {name}")
    _say(f"  Your id:    {identity.node_id[:24]}… (the ship name derives from it)")
    _say("  Every new ship launches with 100 credits in its hold.")
    await _pause(fast)

    # ------------------------------------------------------------ 2: fleet
    _say("STEP 2/5 — Launching a fleet (no centre!)")
    bootstrap = PeerNode(Identity.create(), host="127.0.0.1", port=0)
    await bootstrap.start()
    peers = [("127.0.0.1", bootstrap.port)]
    p1 = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                  provide=True, price=0.5)
    p2 = PeerNode(Identity.create(), host="127.0.0.1", port=0, bootstrap=peers,
                  provide=True, price=1.0)
    await p1.start()
    await p2.start()
    my_node = PeerNode(identity, host="127.0.0.1", port=0, bootstrap=peers)
    await my_node.start()
    consumer = Consumer(my_node)
    _say("  3 ships set sail and found each other over the Kademlia DHT:")
    for node, role in ((bootstrap, "ordinary peer"), (p1, "provider, 0.5 cr/item"),
                       (p2, "provider, 1.0 cr/item")):
        _say(f"    ⚓ {ship_name(node.identity.node_id):24s} {role}")
    _say("  None of them is a 'server' - the first ship is merely first.")
    await _pause(fast)

    # ------------------------------------------------------------ 3: market
    _say("STEP 3/5 — The compute market")
    found = await consumer.list_providers("hash.sha256")
    _say("  Your ship asked the DHT: 'who can run hash.sha256?'")
    for record in found:
        _say(f"    {ship_name(record['node_id']):24s} {record['price']:.2f} credits/item"
             f"  [e2e encrypted{', streaming' if record.get('stream') else ''}]")
    _say("  Providers set their own price; good reputation + low price wins.")
    await _pause(fast)

    # ------------------------------------------------------------ 4: job
    _say("STEP 4/5 — Ship your first cargo (and pay for it)")
    items = [f"cargo-{i}" for i in range(8)]
    report = await consumer.run_job(Job(task="hash.sha256", items=items,
                                        chunk_size=2, redundancy=2))
    _say("  An 8-item job was split into 4 chunks; redundancy=2 ran every chunk")
    _say("  on two SEPARATE ships and cross-checked the results (majority wins).")
    _say("  Payment: a per-chunk Ed25519-signed credit transfer - no escrow,")
    _say(f"  no middleman. Spent: {report.spent:.2f} credits"
         f" (all of it end-to-end encrypted: {report.encrypted_chunks} chunks).")
    balance = my_node.ledger.balance(identity.node_id)
    _say(f"  Your new balance: {balance:.2f} credits")
    await _pause(fast)

    # ------------------------------------------------------------ 5: AI + rank
    _say("STEP 5/5 — Live AI streaming + your rank")
    _say("  Now a prompt streams back token by token as it is generated:\n")
    print("    > ", end="", flush=True)
    async for event in consumer.stream_generate(["What is Kemi?"],
                                                {"max_tokens": 10}):
        if event.get("done"):
            print()
            break
        print(event["token"], end="", flush=True)
    _say("\n  (For the same thing with a real model: install Ollama and start")
    _say("   your provider with `--ai-backend ollama --ai-model llama3.2`.)")
    earned_p1 = p1.ledger.total_earned(p1.identity.node_id)
    title, insignia, nxt = rank_for(earned_p1)
    _say(f"\n  Ships climb ranks as they earn: {ship_name(p1.identity.node_id)}")
    _say(f"  is now {insignia} {title} ({earned_p1:.0f} credits earned"
         + (f"; next rank at {nxt:.0f})." if nxt else ")."))
    await _pause(fast)

    # ------------------------------------------------------------ wrap-up
    invite = make_invite([("THIS-MACHINES-IP", 7700)], note="example")
    _say("THAT'S ALL! To set sail for real:")
    _say("  • Start a fleet:    kemi node --port 7700 --lan")
    _say("  • Invite a friend:  kemi invite --peer IP:7700")
    _say(f"      (codes look like: {invite[:40]}…)")
    _say("  • Join with a code: kemi join --invite CODE")
    _say("  • Same Wi-Fi? No code needed: kemi join   (LAN discovery)")
    _say("  • Live dashboard:   kemi node --ui 8080  →  http://127.0.0.1:8080/")

    for node in (my_node, p1, p2, bootstrap):
        await node.stop()
    return 0
