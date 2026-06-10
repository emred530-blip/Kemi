"""Command line interface.

    kemi tracker                       # run a tracker (discovery + ledger)
    kemi provide --tracker HOST:PORT   # rent out this machine's compute
    kemi providers --tracker ...       # list the active swarm
    kemi balance --tracker ...         # show credit balance
    kemi run --tracker ... --task ...  # submit a job to the swarm
    kemi demo                          # full local end-to-end demonstration
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .consumer import Consumer, Job
from .identity import DEFAULT_IDENTITY_PATH, Identity
from .provider import ProviderNode
from .tracker import Tracker


def _parse_endpoint(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    return host, int(port)


def _load_identity(args: argparse.Namespace) -> Identity:
    return Identity.load_or_create(args.identity)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tracker", type=_parse_endpoint, required=True,
                        metavar="HOST:PORT", help="tracker endpoint")
    parser.add_argument("--identity", default=DEFAULT_IDENTITY_PATH,
                        help="path to identity file (created if missing)")


async def _cmd_tracker(args: argparse.Namespace) -> int:
    tracker = Tracker(host=args.host, port=args.port, db_path=args.db)
    await tracker.start()
    print(f"kemi tracker listening on {args.host}:{tracker.port} (ledger: {args.db})")
    try:
        await tracker.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await tracker.stop()
    return 0


async def _cmd_provide(args: argparse.Namespace) -> int:
    identity = _load_identity(args)
    node = ProviderNode(
        identity=identity,
        tracker_host=args.tracker[0],
        tracker_port=args.tracker[1],
        host=args.host,
        port=args.port,
        advertise_host=args.advertise_host,
        price=args.price,
        max_workers=args.workers,
        ai_backend=None if args.ai_backend == "none" else args.ai_backend,
    )
    await node.start()
    print(f"provider {identity.short_id} serving on port {node.port}, "
          f"price {node.price} credits/item, tasks: {', '.join(node.supported_tasks)}")
    try:
        await node.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await node.stop()
    return 0


async def _cmd_providers(args: argparse.Namespace) -> int:
    consumer = Consumer(_load_identity(args), *args.tracker)
    providers = await consumer.list_providers()
    if not providers:
        print("no active providers")
        return 0
    for p in providers:
        res = p.get("resources", {})
        print(f"{p['node_id'][:12]}  {p['host']}:{p['port']}  "
              f"{p['price']:.2f} cr/item  load={p['load']}  "
              f"cpu={res.get('cpu_count', '?')} mem={res.get('mem_total_mb', '?')}MB  "
              f"tasks={','.join(p['tasks'])}")
    return 0


async def _cmd_balance(args: argparse.Namespace) -> int:
    consumer = Consumer(_load_identity(args), *args.tracker)
    print(f"{await consumer.balance():.2f} credits")
    return 0


async def _cmd_run(args: argparse.Namespace) -> int:
    if args.input == "-":
        items = json.load(sys.stdin)
    else:
        items = json.loads(Path(args.input).read_text())
    if not isinstance(items, list):
        print("error: input must be a JSON array of items", file=sys.stderr)
        return 2
    consumer = Consumer(_load_identity(args), *args.tracker)
    job = Job(
        task=args.task,
        items=items,
        params=json.loads(args.params),
        chunk_size=args.chunk_size,
        redundancy=args.redundancy,
    )
    report = await consumer.run_job(job)
    output = json.dumps(report.results, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output)
    else:
        print(output)
    print(
        f"\n{len(items)} items in {report.chunks} chunks via {len(report.providers_used)} "
        f"provider(s); spent {report.spent:.2f} credits (refunded {report.refunded:.2f})",
        file=sys.stderr,
    )
    return 0


async def _cmd_demo(args: argparse.Namespace) -> int:
    """Spin up a tracker and three providers locally and run two jobs."""
    print("=== kemi demo: local swarm ===")
    tracker = Tracker(host="127.0.0.1", port=0)
    await tracker.start()
    print(f"[1/4] tracker started on 127.0.0.1:{tracker.port}")

    providers = []
    for i, price in enumerate((0.5, 1.0, 1.5), start=1):
        node = ProviderNode(
            identity=Identity.create(),
            tracker_host="127.0.0.1",
            tracker_port=tracker.port,
            host="127.0.0.1",
            price=price,
        )
        await node.start()
        providers.append(node)
        print(f"[2/4] provider {i} ({node.identity.short_id}) on port {node.port}, "
              f"{price} cr/item")

    consumer = Consumer(Identity.create(), "127.0.0.1", tracker.port)
    print(f"[3/4] consumer balance: {await consumer.balance():.2f} credits")

    hash_job = Job(
        task="hash.sha256",
        items=[f"blok-{i}" for i in range(24)],
        params={"rounds": 1000},
        chunk_size=4,
        redundancy=2,
    )
    report = await consumer.run_job(hash_job)
    print(f"[4/4] hash.sha256: {len(report.results)} results across "
          f"{report.chunks} chunks, redundancy=2 verified; spent {report.spent:.2f} cr")

    ai_job = Job(
        task="ai.generate",
        items=["P2P aglar neden onemli?", "Veri merkezlerinin gelecegi nedir?"],
        params={"max_tokens": 12},
        chunk_size=1,
    )
    report = await consumer.run_job(ai_job)
    for prompt, completion in zip(ai_job.items, report.results):
        print(f"      ai.generate({prompt!r}) -> {completion!r}")

    print(f"\nfinal consumer balance: {await consumer.balance():.2f} credits")
    for i, node in enumerate(providers, start=1):
        balance_msg = await node._tracker_request({"type": "balance"})
        print(f"final provider {i} balance: {balance_msg['balance']:.2f} credits")

    for node in providers:
        await node.stop()
    await tracker.stop()
    print("\ndemo complete.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kemi",
        description="Kemi: BitTorrent-benzeri P2P islem gucu paylasim agi",
    )
    parser.add_argument("--version", action="version", version=f"kemi {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("tracker", help="run a tracker (peer discovery + credit ledger)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7700)
    p.add_argument("--db", default="~/.kemi/ledger.db",
                   help="SQLite ledger path (use :memory: for ephemeral)")
    p.set_defaults(func=_cmd_tracker)

    p = sub.add_parser("provide", help="rent out this machine's compute")
    _add_common(p)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    p.add_argument("--advertise-host", default=None,
                   help="address other peers should connect to (default: --host)")
    p.add_argument("--price", type=float, default=1.0, help="credits per work item")
    p.add_argument("--workers", type=int, default=None, help="max concurrent chunks")
    p.add_argument("--ai-backend", default="mock", choices=("mock", "transformers", "none"))
    p.set_defaults(func=_cmd_provide)

    p = sub.add_parser("providers", help="list active providers in the swarm")
    _add_common(p)
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

    p = sub.add_parser("demo", help="run a complete local swarm demonstration")
    p.set_defaults(func=_cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if args.command == "tracker":
        args.db = str(Path(args.db).expanduser()) if args.db != ":memory:" else args.db
        if args.db != ":memory:":
            Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
