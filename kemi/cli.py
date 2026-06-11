"""Command line interface.

    kemi node --peer HOST:PORT             # join the swarm as a plain peer
    kemi node --provide --peer HOST:PORT   # join and rent out compute
    kemi providers --peer HOST:PORT        # list discoverable providers
    kemi balance --peer HOST:PORT          # show credit balance
    kemi run --peer HOST:PORT --task ...   # submit a job to the swarm
    kemi id                                # show this machine's identity
    kemi demo                              # local decentralised swarm demo

There is no tracker and no server role: any running ``kemi node`` can be
the ``--peer`` another node bootstraps through.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .consumer import Consumer, Job
from .identity import DEFAULT_IDENTITY_PATH, Identity
from .node import PeerNode
from .tasks import TASKS

DEFAULT_LEDGER_PATH = "~/.kemi/ledger.db"
DEFAULT_REPUTATION_PATH = "~/.kemi/reputation.db"


def _parse_endpoint(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    return host, int(port)


def _expand_db(path: str) -> str:
    if path == ":memory:":
        return path
    expanded = Path(path).expanduser()
    expanded.parent.mkdir(parents=True, exist_ok=True)
    return str(expanded)


def _add_common(parser: argparse.ArgumentParser, peers_required: bool = True) -> None:
    parser.add_argument("--peer", type=_parse_endpoint, action="append", default=[],
                        required=peers_required, metavar="HOST:PORT",
                        help="existing peer(s) to bootstrap through (repeatable)")
    parser.add_argument("--identity", default=DEFAULT_IDENTITY_PATH,
                        help="path to identity file (created if missing)")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH,
                        help="path to the local ledger replica")
    parser.add_argument("--reputation", default=DEFAULT_REPUTATION_PATH,
                        help="path to the local reputation store")


def _make_node(args: argparse.Namespace, **overrides: Any) -> PeerNode:
    identity = Identity.load_or_create(args.identity)
    return PeerNode(
        identity=identity,
        bootstrap=args.peer,
        ledger_path=_expand_db(args.ledger),
        reputation_path=_expand_db(args.reputation),
        **overrides,
    )


async def _cmd_node(args: argparse.Namespace) -> int:
    node = _make_node(
        args,
        host=args.host,
        port=args.port,
        advertise_host=args.advertise_host,
        provide=args.provide,
        price=args.price,
        max_workers=args.workers,
        sandbox=not args.no_sandbox,
        ai_backend=None if args.ai_backend == "none" else args.ai_backend,
        force_relay=args.force_relay,
    )
    await node.start()
    role = "provider" if args.provide else "peer"
    print(f"kemi {role} {node.identity.short_id} listening on port {node.port} "
          f"(tcp+udp); bootstrap this node with: --peer <bu-makinenin-ip>:{node.port}")
    if args.provide:
        print(f"  price: {node.price} credits/item, sandbox: {node.sandbox}, "
              f"tasks: {', '.join(node.supported_tasks)}")
        if node.resources.get("gpus"):
            print(f"  gpus: {node.resources['gpus']}")
    try:
        await node.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await node.stop()
    return 0


async def _with_ephemeral_node(args: argparse.Namespace):
    node = _make_node(args, host="0.0.0.0", port=0, provide=False)
    await node.start()
    return node


async def _cmd_providers(args: argparse.Namespace) -> int:
    node = await _with_ephemeral_node(args)
    try:
        consumer = Consumer(node)
        seen: dict[str, dict[str, Any]] = {}
        tasks = [args.task] if args.task else list(TASKS)
        for task in tasks:
            for record in await consumer.list_providers(task):
                seen[record["node_id"]] = record
        if not seen:
            print("no providers discovered")
            return 0
        for record in seen.values():
            res = record.get("resources", {})
            gpus = res.get("gpus") or []
            via = f"relay {record['relay']['host']}:{record['relay']['port']}" \
                if record.get("relay") else f"{record['host']}:{record['port']}"
            print(f"{record['node_id'][:12]}  {via}  {record['price']:.2f} cr/item  "
                  f"rep={node.reputation.score(record['node_id']):.2f}  "
                  f"cpu={res.get('cpu_count', '?')} gpu={len(gpus)}  "
                  f"tasks={','.join(record['tasks'])}")
        return 0
    finally:
        await node.stop()


async def _cmd_balance(args: argparse.Namespace) -> int:
    node = await _with_ephemeral_node(args)
    try:
        print(f"{await Consumer(node).balance():.2f} credits "
              f"(node {node.identity.short_id})")
        return 0
    finally:
        await node.stop()


async def _cmd_run(args: argparse.Namespace) -> int:
    if args.input == "-":
        items = json.load(sys.stdin)
    else:
        items = json.loads(Path(args.input).read_text())
    if not isinstance(items, list):
        print("error: input must be a JSON array of items", file=sys.stderr)
        return 2
    node = await _with_ephemeral_node(args)
    try:
        consumer = Consumer(node)
        report = await consumer.run_job(Job(
            task=args.task,
            items=items,
            params=json.loads(args.params),
            chunk_size=args.chunk_size,
            redundancy=args.redundancy,
        ))
        output = json.dumps(report.results, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(output)
        else:
            print(output)
        print(
            f"\n{len(items)} items in {report.chunks} chunks via "
            f"{len(report.providers_used)} provider(s); spent {report.spent:.2f} "
            f"credits (exposure {report.exposure:.2f})",
            file=sys.stderr,
        )
        # Give fire-and-forget gossip a moment to leave the building.
        await asyncio.sleep(0.5)
        return 0
    finally:
        await node.stop()


async def _cmd_id(args: argparse.Namespace) -> int:
    identity = Identity.load_or_create(args.identity)
    print(f"node_id:  {identity.node_id}")
    print(f"pubkey:   {identity.public_key_hex}")
    print(f"pow:      nonce={identity.pow_nonce}")
    return 0


async def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    return await run_demo()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kemi",
        description="Kemi: merkeziyetsiz P2P islem gucu paylasim agi",
    )
    parser.add_argument("--version", action="version", version=f"kemi {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("node", help="run a peer (optionally providing compute)")
    _add_common(p, peers_required=False)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7700, help="tcp+udp port (0 = random)")
    p.add_argument("--advertise-host", default=None,
                   help="public address to advertise (default: learned from peers)")
    p.add_argument("--provide", action="store_true", help="rent out this machine's compute")
    p.add_argument("--price", type=float, default=1.0, help="credits per work item")
    p.add_argument("--workers", type=int, default=None, help="max concurrent chunks")
    p.add_argument("--ai-backend", default="mock", choices=("mock", "transformers", "none"))
    p.add_argument("--no-sandbox", action="store_true",
                   help="run tasks in-process instead of resource-limited subprocesses")
    p.add_argument("--force-relay", action="store_true",
                   help="always relay through a bootstrap peer (NATed hosts)")
    p.set_defaults(func=_cmd_node)

    p = sub.add_parser("providers", help="list discoverable providers")
    _add_common(p)
    p.add_argument("--task", default=None, help="only providers offering this task")
    p.set_defaults(func=_cmd_providers)

    p = sub.add_parser("balance", help="show credit balance")
    _add_common(p)
    p.set_defaults(func=_cmd_balance)

    p = sub.add_parser("run", help="submit a job to the swarm")
    _add_common(p)
    p.add_argument("--task", required=True, help="task name, e.g. hash.sha256 or ai.generate")
    p.add_argument("--input", required=True, help="JSON array of items, or - for stdin")
    p.add_argument("--params", default="{}", help="task parameters as JSON object")
    p.add_argument("--chunk-size", type=int, default=8)
    p.add_argument("--redundancy", type=int, default=1,
                   help=">1 cross-checks results across distinct providers")
    p.add_argument("--output", default=None, help="write results to this file")
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("id", help="show (or mint) this machine's identity")
    p.add_argument("--identity", default=DEFAULT_IDENTITY_PATH)
    p.set_defaults(func=_cmd_id)

    p = sub.add_parser("demo", help="run a complete local decentralised swarm demo")
    p.set_defaults(func=_cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if not args.verbose:
        logging.getLogger("kemi").setLevel(logging.WARNING)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
