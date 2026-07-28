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
import os
import sys
from pathlib import Path
from typing import Any

import socket

from . import __version__
from .consumer import Consumer, Job, PipelineStage
from .identity import Identity
from .invite import InviteError, make_invite, parse_invite
from .names import BANNER, rank_for, ship_name
from .node import PeerNode
from .protocol import request
from .tasks import TASKS


def _guess_lan_ip() -> str:
    """Best-effort local address for invites (no packet is actually sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("203.0.113.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()

# All node state lives under $KEMI_HOME (default ~/.kemi) so one directory
# holds the identity, ledger and reputation — handy for Docker volumes and
# the service installer.
KEMI_HOME = os.environ.get("KEMI_HOME") or os.path.join(os.path.expanduser("~"), ".kemi")
DEFAULT_IDENTITY_PATH = os.path.join(KEMI_HOME, "identity.json")
DEFAULT_LEDGER_PATH = os.path.join(KEMI_HOME, "ledger.db")
DEFAULT_REPUTATION_PATH = os.path.join(KEMI_HOME, "reputation.db")
DEFAULT_BRAIN_PATH = os.path.join(KEMI_HOME, "brain.json")


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
        brain_path=DEFAULT_BRAIN_PATH,
        **overrides,
    )


async def _cmd_node(args: argparse.Namespace) -> int:
    ai_backend = None if args.ai_backend == "none" else args.ai_backend
    if isinstance(ai_backend, str) and args.ai_model:
        from .ai_backends import load_backend

        ai_backend = load_backend(ai_backend, model=args.ai_model)
    node = _make_node(
        args,
        host=args.host,
        port=args.port,
        advertise_host=args.advertise_host,
        provide=args.provide,
        price=args.price,
        max_workers=args.workers,
        sandbox=not args.no_sandbox,
        ai_backend=ai_backend,
        force_relay=args.force_relay,
        lan=args.lan,
        dynamic_price=args.dynamic_price,
    )
    await node.start()
    role = "provider" if args.provide else "peer"
    name = ship_name(node.identity.node_id)
    print(f"Node '{name}' online — role: {role}, port {node.port} (tcp+udp)")
    print(f"  invite code: {make_invite([(_guess_lan_ip(), node.port)], note=name)}")
    print("  join with:   kemi join --invite CODE   (or on the same network: kemi join)")
    if args.provide:
        print(f"  price: {node.price} credits/item, sandbox: {node.sandbox}, "
              f"tasks: {', '.join(node.supported_tasks)}")
        from .ai_backends import backend_model

        model = backend_model(node._task_context.get("ai_backend"))
        if model:
            print(f"  model: {model}")
        if node.resources.get("gpus"):
            print(f"  gpus: {node.resources['gpus']}")
    ui = None
    if args.ui is not None:
        from .webui import WebUI

        ui = WebUI(node, host=args.ui_host, port=args.ui)
        await ui.start()
        print(f"  dashboard: {ui.url}")
        if args.open and args.ui_host in ("127.0.0.1", "localhost"):
            import webbrowser
            try:
                webbrowser.open(ui.url)
            except Exception:
                pass
    try:
        await node.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        if ui is not None:
            await ui.stop()
        await node.stop()
    return 0


async def _cmd_app(args: argparse.Namespace) -> int:
    """The no-terminal-after-this experience: one command starts a sharing
    node and opens the dashboard in the browser. Everything else is clicks."""
    import webbrowser

    from .webui import WebUI

    print(BANNER)
    print("  Starting Kemi — connecting this machine to the network.")
    node = _make_node(
        args, host="0.0.0.0", port=args.port, provide=not args.watch,
        price=args.price, lan=True,
        ai_backend=None if args.ai_backend == "none" else args.ai_backend,
    )
    if args.peer:
        node.bootstrap_peers.extend(p for p in args.peer if p not in node.bootstrap_peers)
    await node.start()
    name = ship_name(node.identity.node_id)
    # --phone binds the dashboard to the LAN so a phone on the same Wi-Fi can
    # open it and install the PWA; default stays localhost-only for safety.
    ui_host = "0.0.0.0" if args.phone else "127.0.0.1"
    ui = WebUI(node, host=ui_host, port=args.ui)
    await ui.start()
    print(f"\n  Node '{name}' is online.")
    print(f"  Dashboard:  http://127.0.0.1:{ui.port}/")
    if args.phone:
        print(f"  On your phone (same Wi-Fi):  http://{_guess_lan_ip()}:{ui.port}/")
        print("    use the browser's “Add to Home Screen” to install the app.")
    print("  From there: query the AI, invite peers and monitor the network.")
    print("  Keep this window open. Press Ctrl+C to stop.\n")
    try:
        webbrowser.open(ui.url)
    except Exception:
        pass
    try:
        await node.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await ui.stop()
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
            model = f" model={record['model']}" if record.get("model") else ""
            print(f"{ship_name(record['node_id'])}  {via}  {record['price']:.2f} cr/item  "
                  f"rep={node.reputation.score(record['node_id']):.2f}  "
                  f"cpu={res.get('cpu_count', '?')} gpu={len(gpus)}{model}  "
                  f"tasks={','.join(record['tasks'])}")
        return 0
    finally:
        await node.stop()


async def _cmd_models(args: argparse.Namespace) -> int:
    node = await _with_ephemeral_node(args)
    try:
        models = await Consumer(node).models(args.task)
        if not models:
            print(f"no AI models advertised for {args.task}")
        for model in models:
            print(model)
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


async def _cmd_wallet(args: argparse.Namespace) -> int:
    """Show this ship's balance, rank and recent transactions."""
    from .names import rank_for, ship_name

    node = await _with_ephemeral_node(args)
    try:
        await node.sync_ledger()
        me = node.identity.node_id
        earned = node.ledger.total_earned(me)
        title, insignia, nxt = rank_for(earned)
        print(f"{ship_name(me)}   tier: {insignia} {title}")
        print(f"  balance: {node.ledger.balance(me):.2f} credits   "
              f"earned: {earned:.2f}" + (f"   next tier at {nxt:.0f}" if nxt else ""))
        history = node.ledger.history(me, limit=args.limit)
        if not history:
            print("  no transactions yet")
        for tx in history:
            sign = "−" if tx["direction"] == "sent" else "+"
            other = ship_name(tx["counterparty"])
            verb = "to  " if tx["direction"] == "sent" else "from"
            print(f"  {sign}{tx['amount']:.2f}  {verb} {other}")
        return 0
    finally:
        await node.stop()


async def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the OpenAI-compatible gateway so any AI tool can use the fleet."""
    from .openai_gateway import OpenAIGateway

    node = _make_node(args, host="0.0.0.0", port=args.port, provide=False)
    await node.start()
    gw = OpenAIGateway(node, host=args.host, port=args.serve_port)
    await gw.start()
    print(f"OpenAI-compatible gateway: {gw.base_url}")
    print("  Point any OpenAI client at it (no real key needed):")
    print(f"    export OPENAI_BASE_URL={gw.base_url}")
    print("    export OPENAI_API_KEY=kemi")
    print("  Endpoints: /v1/chat/completions  /v1/embeddings  /v1/models")
    print("  Ctrl+C to stop.")
    try:
        await node.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await gw.stop()
        await node.stop()
    return 0


async def _cmd_web(args: argparse.Namespace) -> int:
    """Run a public access portal: a web app for querying the network."""
    from .webapp import WebApp

    node = _make_node(args, host="0.0.0.0", port=args.port,
                      provide=args.provide, price=args.price)
    await node.start()
    state_path = os.path.join(KEMI_HOME, "portal.json")
    app = WebApp(node, host=args.web_host, port=args.web_port,
                 faucet=args.faucet, state_path=state_path,
                 admin_key=args.admin_key, trust_proxy=args.trust_proxy,
                 guests_per_ip=args.guests_per_ip)
    await app.start()
    print(BANNER)
    print(f"Access portal:    http://{args.web_host}:{app.port}/")
    print(f"Operator console: http://{args.web_host}:{app.port}/admin")
    print(f"Health check:     http://{args.web_host}:{app.port}/healthz")
    print(f"Admin key:        {app.admin_key}   (store securely)")
    print(f"  Each new visitor receives {args.faucet:g} starting credits. Their queries")
    print("  are processed by provider nodes and settled from this node's balance.")
    print(f"  Node balance: "
          f"{node.ledger.balance(node.identity.node_id):.2f} credits"
          + (" (also providing compute)" if args.provide else ""))
    print("  Ctrl+C to stop the portal.")
    try:
        await node.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await app.stop()
        await node.stop()
    return 0


async def _cmd_search(args: argparse.Namespace) -> int:
    """RAG over the fleet: rank documents (one per non-empty line) against a
    query by embedding both and cosine-ranking — all on the swarm."""
    from .api import Fleet

    text = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
    docs = [line.strip() for line in text.splitlines() if line.strip()]
    if not docs:
        print("error: no documents (one per non-empty line)", file=sys.stderr)
        return 2
    node = await _with_ephemeral_node(args)
    try:
        hits = await Fleet(node).rag_search(args.query, docs, top_k=args.top_k,
                                            model=args.model)
        for hit in hits:
            print(f"{hit['score']:.3f}  {hit['document']}")
        await asyncio.sleep(0.5)
        return 0
    finally:
        await node.stop()


async def _cmd_run(args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
    if args.lines:
        items: Any = [line for line in text.splitlines() if line.strip()]
        if not items:
            print("error: input has no non-empty lines", file=sys.stderr)
            return 2
    else:
        items = json.loads(text)
        if not isinstance(items, list):
            print("error: input must be a JSON array of items (or use --lines)",
                  file=sys.stderr)
            return 2
    node = await _with_ephemeral_node(args)
    try:
        consumer = Consumer(node)
        if args.stream:
            if args.task != "ai.generate":
                print("error: --stream only works with --task ai.generate",
                      file=sys.stderr)
                return 2
            current = -1
            async for event in consumer.stream_generate(
                    items, json.loads(args.params), model=args.model):
                if event.get("done"):
                    print()
                    print(f"\n{len(items)} prompt(s) streamed; {event['spent']:.2f} "
                          f"credits via {ship_name(event['provider'])}", file=sys.stderr)
                    break
                if event["item"] != current:
                    if current >= 0:
                        print()
                    current = event["item"]
                    print(f"--- prompt {current + 1} ---")
                print(event["token"], end="", flush=True)
            await asyncio.sleep(0.5)
            return 0
        report = await consumer.run_job(Job(
            task=args.task,
            items=items,
            params=json.loads(args.params),
            chunk_size=args.chunk_size,
            redundancy=args.redundancy,
            model=args.model,
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


async def _cmd_shard(args: argparse.Namespace) -> int:
    from .model import ModelSpec
    from .sharded import ShardedLLM

    spec = ModelSpec(n_layers=args.layers)
    node = await _with_ephemeral_node(args)
    try:
        llm = ShardedLLM(Consumer(node), spec)
        try:
            plan = await llm.make_plan(redundancy=args.redundancy)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"sharding {spec.name} ({spec.n_layers} layers) across "
              f"{plan.ships} ship(s):", file=sys.stderr)
        print(f"  {plan.describe()}", file=sys.stderr)
        report = await llm.generate(args.prompt, max_tokens=args.max_tokens,
                                    redundancy=args.redundancy, plan=plan)
        print(report.text)
        verified = (f", {report.verified_stages} stages cross-checked"
                    if args.redundancy > 1 else "")
        print(f"\n{len(report.tokens)} tokens; {report.spent:.2f} credits; "
              f"no single ship held all {spec.n_layers} layers{verified}",
              file=sys.stderr)
        await asyncio.sleep(0.5)
        return 0
    finally:
        await node.stop()


async def _cmd_pipeline(args: argparse.Namespace) -> int:
    if args.input == "-":
        items = json.load(sys.stdin)
    else:
        items = json.loads(Path(args.input).read_text())
    stage_specs = json.loads(args.stages)
    if not isinstance(items, list) or not isinstance(stage_specs, list):
        print("error: input and --stages must be JSON arrays", file=sys.stderr)
        return 2
    stages = [PipelineStage(task=s["task"], params=s.get("params", {}),
                            chunk_size=int(s.get("chunk_size", 8)),
                            redundancy=int(s.get("redundancy", 1)))
              for s in stage_specs]
    node = await _with_ephemeral_node(args)
    try:
        report = await Consumer(node).run_pipeline(stages, items)
        output = json.dumps(report.results, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(output)
        else:
            print(output)
        print(f"\n{len(stages)} stages over {len(items)} items; "
              f"total spent {report.spent:.2f} credits", file=sys.stderr)
        await asyncio.sleep(0.5)
        return 0
    finally:
        await node.stop()


async def _cmd_status(args: argparse.Namespace) -> int:
    for host, port in args.peer:
        try:
            info = await request(host, port, {"type": "node.info"}, timeout=10.0)
        except Exception as exc:
            print(f"{host}:{port}  UNREACHABLE ({exc})")
            continue
        if not info.get("ok"):
            print(f"{host}:{port}  ERROR {info.get('error')}")
            continue
        res = info.get("resources", {})
        role = "provider" if info.get("provide") else "peer"
        print(f"{host}:{port}  {role} {info.get('node_id', '')[:12]}  "
              f"ledger={info.get('ledger_txs')} txs  dht={info.get('dht_contacts')} peers  "
              f"relayed={info.get('relayed')}")
        if info.get("provide"):
            print(f"  price={info.get('price')} cr/item  cpu={res.get('cpu_count')}  "
                  f"gpu={len(res.get('gpus') or [])}  tasks={','.join(info.get('tasks', []))}")
    return 0


async def _cmd_id(args: argparse.Namespace) -> int:
    identity = Identity.load_or_create(args.identity)
    print(f"node:     {ship_name(identity.node_id)}")
    earned = 0.0
    ledger_path = Path(DEFAULT_LEDGER_PATH).expanduser()
    if ledger_path.exists():
        from .gossip_ledger import GossipLedger

        ledger = GossipLedger(str(ledger_path))
        earned = ledger.total_earned(identity.node_id)
        ledger.close()
    title, insignia, nxt = rank_for(earned)
    progress = f" - next tier at {nxt:.0f}" if nxt is not None else ""
    print(f"tier:     {insignia} {title} ({earned:.2f} credits earned{progress})")
    print(f"node_id:  {identity.node_id}")
    print(f"pubkey:   {identity.public_key_hex}")
    print(f"pow:      nonce={identity.pow_nonce}")
    return 0


async def _cmd_invite(args: argparse.Namespace) -> int:
    code = make_invite(args.peer, note=args.note or "")
    print("Your invite code is ready - safe to share anywhere (contains no secrets):\n")
    print(f"  {code}\n")
    print("Another machine joins your network with a single command:")
    print(f"  kemi join --invite {code[:24]}…")
    return 0


async def _cmd_join(args: argparse.Namespace) -> int:
    """The 60-second onboarding wizard."""
    print(BANNER)
    identity = Identity.load_or_create(args.identity)
    name = ship_name(identity.node_id)
    print(f"  Node name: {name}   (identity {identity.short_id}…)")

    peers: list[tuple[str, int]] = list(args.peer or [])
    if args.invite:
        try:
            info = parse_invite(args.invite)
        except InviteError as exc:
            print(f"  error: {exc}", file=sys.stderr)
            return 2
        peers.extend(info["peers"])
        if info["note"]:
            print(f"  Invited to network: '{info['note']}'")
    if not peers:
        from . import lan

        print("  Scanning the local network for peers (LAN discovery)…")
        peers = await lan.discover(identity.node_id, timeout=2.0)
        if peers:
            print(f"  Found {len(peers)} node(s) - joining.")
        else:
            print("  No peers found nearby - starting a new network as its first node…")

    provide = args.share
    if not provide and not args.watch and sys.stdin.isatty():
        answer = await asyncio.to_thread(
            input, "  Share your compute and earn credits? [Y/n] ")
        provide = answer.strip().lower() in ("", "e", "evet", "y", "yes")

    node = _make_node(args, host="0.0.0.0", port=args.port, provide=provide,
                      price=args.price, lan=True)
    node.bootstrap_peers.extend(p for p in peers if p not in node.bootstrap_peers)
    await node.start()

    earned = node.ledger.total_earned(identity.node_id)
    title, insignia, _ = rank_for(earned)
    balance = node.ledger.balance(identity.node_id)
    print(f"\n  Node '{name}' is online — tier: {insignia} {title}, "
          f"balance: {balance:.2f} credits, port: {node.port}")
    if provide:
        print(f"  Serving compute at {node.price} credits/item "
              f"({', '.join(node.supported_tasks)})")
    print(f"  Invite code: {make_invite([(_guess_lan_ip(), node.port)], note=name)}")

    ui = None
    if args.ui:
        from .webui import WebUI

        ui = WebUI(node, port=args.ui)
        await ui.start()
        print(f"  Live dashboard: {ui.url}")
    print("\n  (Ctrl+C to stop)")
    try:
        await node.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        if ui is not None:
            await ui.stop()
        await node.stop()
    return 0


async def _cmd_learn(args: argparse.Namespace) -> int:
    from .tutorial import run_tutorial

    return await run_tutorial(fast=args.fast)


async def _cmd_chat(args: argparse.Namespace) -> int:
    from .chat import run_chat

    node = await _with_ephemeral_node(args)
    try:
        return await run_chat(Consumer(node), max_tokens=args.max_tokens,
                              model=args.model)
    finally:
        await node.stop()


async def _cmd_economy(args: argparse.Namespace) -> int:
    from .economy import render, simulate

    print(render(simulate(identities=args.identities, providers=args.providers,
                          rounds=args.rounds)))
    return 0


async def _cmd_service(args: argparse.Namespace) -> int:
    from .service import install

    extra = []
    if args.peer:
        for host, port in args.peer:
            extra += ["--peer", f"{host}:{port}"]
    if args.price != 1.0:
        extra += ["--price", str(args.price)]
    if args.ai_backend != "mock":
        extra += ["--ai-backend", args.ai_backend]
    try:
        path, content, hint = install(extra, write=not args.dry_run)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(content)
        return 0
    print(f"✓ service unit written to {path}")
    print(f"  activate it with:\n    {hint}")
    print("  this node will now rejoin the network automatically after reboot.")
    return 0


async def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import render, run_checks

    print("kemi doctor - checking this machine...\n")
    peer = args.peer[0] if args.peer else None
    checks = await run_checks(peer=peer, identity_path=args.identity)
    return render(checks)


async def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    return await run_demo(ui_port=args.ui)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kemi",
        description="Kemi: the decentralised peer-to-peer compute sharing network",
    )
    parser.add_argument("--version", action="version", version=f"kemi {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("join", aliases=["katil", "katıl"],
                       help="join a fleet in 60 seconds (wizard)")
    p.add_argument("--invite", "--davet", dest="invite", default=None,
                   metavar="CODE", help="a friend's invite code")
    p.add_argument("--peer", type=_parse_endpoint, action="append", default=[],
                   metavar="HOST:PORT", help="a known peer (instead of an invite)")
    p.add_argument("--share", "--paylas", dest="share", action="store_true",
                   help="share compute without being asked")
    p.add_argument("--watch", "--izle", dest="watch", action="store_true",
                   help="don't share; observe/consume only")
    p.add_argument("--price", "--fiyat", dest="price", type=float, default=1.0,
                   help="your credits-per-item price")
    p.add_argument("--port", type=int, default=7700)
    p.add_argument("--ui", type=int, default=8080, metavar="PORT",
                   help="live dashboard port (0 = no dashboard)")
    p.add_argument("--identity", default=DEFAULT_IDENTITY_PATH)
    p.add_argument("--ledger", default=DEFAULT_LEDGER_PATH)
    p.add_argument("--reputation", default=DEFAULT_REPUTATION_PATH)
    p.set_defaults(func=_cmd_join)

    p = sub.add_parser("learn", aliases=["ogren", "öğren"],
                       help="3-minute interactive tour on a live fleet")
    p.add_argument("--fast", "--hizli", dest="fast", action="store_true",
                   help="run start to finish without pausing")
    p.set_defaults(func=_cmd_learn)

    p = sub.add_parser("chat", aliases=["sohbet"],
                       help="talk to the fleet's AI (live streaming REPL)")
    _add_common(p)
    p.add_argument("--max-tokens", type=int, default=128,
                   help="token budget per reply")
    p.add_argument("--model", default=None, help="pin a specific AI model")
    p.set_defaults(func=_cmd_chat)

    p = sub.add_parser("models", aliases=["modeller"],
                       help="list AI models advertised by the fleet")
    _add_common(p)
    p.add_argument("--task", default="ai.generate",
                   help="task to list models for (ai.generate or ai.embed)")
    p.set_defaults(func=_cmd_models)

    p = sub.add_parser("serve", aliases=["gateway"],
                       help="run an OpenAI-compatible API gateway backed by the fleet")
    p.add_argument("--peer", type=_parse_endpoint, action="append", default=[],
                   required=True, metavar="HOST:PORT")
    p.add_argument("--host", default="127.0.0.1", help="gateway bind address")
    p.add_argument("--serve-port", type=int, default=11434, help="gateway port")
    p.add_argument("--port", type=int, default=0, help="this node's p2p port")
    p.add_argument("--identity", default=DEFAULT_IDENTITY_PATH)
    p.add_argument("--ledger", default=DEFAULT_LEDGER_PATH)
    p.add_argument("--reputation", default=DEFAULT_REPUTATION_PATH)
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("web", aliases=["portal"],
                       help="run a public access portal: a web app for querying "
                            "the network from any browser")
    p.add_argument("--peer", type=_parse_endpoint, action="append", default=[],
                   metavar="HOST:PORT", help="fleet peer(s) to join (optional "
                   "when other ships are on this LAN)")
    p.add_argument("--web-host", default="0.0.0.0", help="harbor bind address")
    p.add_argument("--web-port", type=int, default=8090, help="harbor port")
    p.add_argument("--port", type=int, default=0, help="this node's p2p port")
    p.add_argument("--faucet", type=float, default=10.0,
                   help="starting credits per new visitor (funded by this node)")
    p.add_argument("--admin-key", default=None,
                   help="operator console key (default: generated and printed)")
    p.add_argument("--trust-proxy", action="store_true",
                   help="read the visitor's address from X-Forwarded-For / "
                        "X-Real-IP; set this only when a reverse proxy you "
                        "control is the only way in")
    p.add_argument("--guests-per-ip", type=int, default=5,
                   help="starting allowances one address may claim per hour "
                        "(0 disables the cap)")
    p.add_argument("--provide", action="store_true",
                   help="also share this machine's compute with the fleet")
    p.add_argument("--price", type=float, default=1.0)
    p.add_argument("--identity", default=DEFAULT_IDENTITY_PATH)
    p.add_argument("--ledger", default=DEFAULT_LEDGER_PATH)
    p.add_argument("--reputation", default=DEFAULT_REPUTATION_PATH)
    p.set_defaults(func=_cmd_web)

    p = sub.add_parser("search", aliases=["ara"],
                       help="RAG: rank documents against a query over the fleet")
    _add_common(p)
    p.add_argument("--query", required=True, help="the search query")
    p.add_argument("--input", required=True, help="documents file (one per line), or -")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--model", default=None, help="pin an embedding model")
    p.set_defaults(func=_cmd_search)

    p = sub.add_parser("economy", aliases=["ekonomi"],
                       help="simulate and report on the credit economy")
    p.add_argument("--identities", type=int, default=100)
    p.add_argument("--providers", type=int, default=30)
    p.add_argument("--rounds", type=int, default=2000)
    p.set_defaults(func=_cmd_economy)

    p = sub.add_parser("service", aliases=["servis"],
                       help="install an auto-start service (systemd/launchd)")
    p.add_argument("--peer", type=_parse_endpoint, action="append", default=[],
                   metavar="HOST:PORT", help="peer(s) the service should join")
    p.add_argument("--price", type=float, default=1.0)
    p.add_argument("--ai-backend", default="mock",
                   choices=("mock", "ollama", "transformers", "none"))
    p.add_argument("--dry-run", action="store_true",
                   help="print the unit instead of installing it")
    p.set_defaults(func=_cmd_service)

    p = sub.add_parser("doctor", aliases=["tani"],
                       help="diagnose this machine's fleet readiness")
    p.add_argument("--peer", type=_parse_endpoint, action="append", default=[],
                   metavar="HOST:PORT", help="also test reachability of this peer")
    p.add_argument("--identity", default=DEFAULT_IDENTITY_PATH)
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser("invite", aliases=["davet"],
                       help="mint an invite code for your fleet")
    p.add_argument("--peer", type=_parse_endpoint, action="append", required=True,
                   metavar="HOST:PORT", help="peer(s) invitees will connect through")
    p.add_argument("--note", default=None,
                   help="short note for the invite (e.g. fleet name)")
    p.set_defaults(func=_cmd_invite)

    p = sub.add_parser("node", help="run a peer (optionally providing compute)")
    _add_common(p, peers_required=False)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7700, help="tcp+udp port (0 = random)")
    p.add_argument("--advertise-host", default=None,
                   help="public address to advertise (default: learned from peers)")
    p.add_argument("--provide", action="store_true", help="rent out this machine's compute")
    p.add_argument("--price", type=float, default=1.0, help="credits per work item")
    p.add_argument("--workers", type=int, default=None, help="max concurrent chunks")
    p.add_argument("--ai-backend", default="mock",
                   choices=("mock", "ollama", "transformers", "none"))
    p.add_argument("--ai-model", default=None,
                   help="model name for ollama/transformers (e.g. llama3.2)")
    p.add_argument("--no-sandbox", action="store_true",
                   help="run tasks in-process instead of resource-limited subprocesses")
    p.add_argument("--force-relay", action="store_true",
                   help="always relay through a bootstrap peer (NATed hosts)")
    p.add_argument("--ui", type=int, default=None, metavar="PORT",
                   help="serve the live web dashboard on this port")
    p.add_argument("--ui-host", default="127.0.0.1",
                   help="dashboard bind address (default: localhost only)")
    p.add_argument("--lan", action="store_true",
                   help="auto-discover ships on the local network")
    p.add_argument("--open", action="store_true",
                   help="open the dashboard in your browser automatically")
    p.add_argument("--dynamic-price", action="store_true",
                   help="surge the advertised price with load (base price stays the floor)")
    p.set_defaults(func=_cmd_node)

    p = sub.add_parser("app", aliases=["uygulama"],
                       help="the easy mode: start sharing and open the dashboard (no coding)")
    p.add_argument("--peer", type=_parse_endpoint, action="append", default=[],
                   metavar="HOST:PORT", help="a friend's peer to join (optional)")
    p.add_argument("--watch", action="store_true",
                   help="don't share compute; just use the fleet")
    p.add_argument("--price", type=float, default=1.0)
    p.add_argument("--port", type=int, default=7700)
    p.add_argument("--ui", type=int, default=8080, metavar="PORT")
    p.add_argument("--phone", action="store_true",
                   help="expose the dashboard on your LAN so a phone can install the PWA")
    p.add_argument("--ai-backend", default="mock", choices=("mock", "ollama", "transformers", "none"))
    p.add_argument("--identity", default=DEFAULT_IDENTITY_PATH)
    p.add_argument("--ledger", default=DEFAULT_LEDGER_PATH)
    p.add_argument("--reputation", default=DEFAULT_REPUTATION_PATH)
    p.set_defaults(func=_cmd_app)

    p = sub.add_parser("providers", aliases=["nodes", "dugumler", "fleet", "filo"],
                       help="list discoverable provider nodes")
    _add_common(p)
    p.add_argument("--task", default=None, help="only providers offering this task")
    p.set_defaults(func=_cmd_providers)

    p = sub.add_parser("wallet", aliases=["cuzdan"],
                       help="show balance, rank and recent transactions")
    _add_common(p)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_cmd_wallet)

    p = sub.add_parser("balance", aliases=["bakiye"], help="show credit balance")
    _add_common(p)
    p.set_defaults(func=_cmd_balance)

    p = sub.add_parser("run", aliases=["calistir"], help="submit a job to the fleet")
    _add_common(p)
    p.add_argument("--task", required=True, help="task name, e.g. hash.sha256 or ai.generate")
    p.add_argument("--input", required=True, help="JSON array of items, or - for stdin")
    p.add_argument("--params", default="{}", help="task parameters as JSON object")
    p.add_argument("--chunk-size", type=int, default=8)
    p.add_argument("--redundancy", type=int, default=1,
                   help=">1 cross-checks results across distinct providers")
    p.add_argument("--output", default=None, help="write results to this file")
    p.add_argument("--stream", action="store_true",
                   help="ai.generate: print tokens live as the model produces them")
    p.add_argument("--lines", action="store_true",
                   help="treat input as plain text: one item per non-empty line")
    p.add_argument("--model", default=None,
                   help="require providers advertising this AI model")
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("pipeline", help="run a multi-stage pipeline job over the swarm")
    _add_common(p)
    p.add_argument("--stages", required=True,
                   help='JSON array of stages, e.g. \'[{"task":"ai.layer","params":{"layer":0}}]\'')
    p.add_argument("--input", required=True, help="JSON array of items, or - for stdin")
    p.add_argument("--output", default=None, help="write results to this file")
    p.set_defaults(func=_cmd_pipeline)

    p = sub.add_parser("shard", aliases=["parcala"],
                       help="run a transformer sharded across the fleet (no ship holds it all)")
    _add_common(p)
    p.add_argument("--prompt", required=True, help="text prompt to continue")
    p.add_argument("--layers", type=int, default=6, help="model depth to shard")
    p.add_argument("--max-tokens", type=int, default=16)
    p.add_argument("--redundancy", type=int, default=1,
                   help=">1 cross-checks each layer shard on distinct ships")
    p.set_defaults(func=_cmd_shard)

    p = sub.add_parser("status", aliases=["durum"], help="query the health of one or more peers")
    p.add_argument("--peer", type=_parse_endpoint, action="append", required=True,
                   metavar="HOST:PORT")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("id", aliases=["ship", "kimlik"],
                       help="show (or mint) this machine's ship identity")
    p.add_argument("--identity", default=DEFAULT_IDENTITY_PATH)
    p.set_defaults(func=_cmd_id)

    p = sub.add_parser("demo", help="run a complete local decentralised swarm demo")
    p.add_argument("--ui", type=int, default=None, metavar="PORT",
                   help="keep the swarm running afterwards with a live dashboard")
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
