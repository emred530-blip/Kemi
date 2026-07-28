"""Kemi: a decentralised peer-to-peer compute and AI network.

Kademlia DHT discovery, Ed25519 proof-of-work identities, a
gossip-replicated credit ledger, redundancy-verified execution and NAT
relaying — no tracker, no servers, no privileged roles.

Quick start as a library::

    import asyncio, kemi

    async def main():
        async with kemi.connect(peer="HOST:7700") as net:
            print(await net.run("hash.sha256", ["hello"]))

    asyncio.run(main())
"""

__version__ = "1.11.1"

from .api import (  # noqa: E402,F401  (public API re-exports)
    Fleet,
    Job,
    Network,
    JobError,
    JobReport,
    PipelineReport,
    PipelineStage,
    connect,
)
