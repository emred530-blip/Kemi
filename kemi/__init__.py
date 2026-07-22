"""Kemi: the decentralised P2P compute sharing network.

A fully decentralised peer-to-peer compute sharing network: Kademlia DHT
discovery, Ed25519 proof-of-work identities, a gossip-replicated credit
ledger, redundancy-verified execution and NAT relaying - no tracker, no
servers, no special roles.

Quick start as a library::

    import asyncio, kemi

    async def main():
        async with kemi.connect(peer="HOST:7700") as fleet:
            print(await fleet.run("hash.sha256", ["hello"]))

    asyncio.run(main())
"""

__version__ = "1.8.1"

from .api import (  # noqa: E402,F401  (public API re-exports)
    Fleet,
    Job,
    JobError,
    JobReport,
    PipelineReport,
    PipelineStage,
    connect,
)
