"""Embedded web dashboard: watch and drive the swarm from a browser.

A dependency-free HTTP server (asyncio + stdlib only) that attaches to a
running :class:`~kemi.node.PeerNode` and serves a single-page live view:

* swarm: discovered providers with price, reputation, GPU and relay status,
* wallet: balance and the gossip ledger's latest transfers,
* jobs: submit work from the browser and follow it to completion,
* node health: DHT contacts, ledger size, flagged accounts.

Binds to 127.0.0.1 by default - the dashboard has no authentication, so
only expose it beyond localhost behind a reverse proxy you trust.

    kemi node --peer HOST:PORT --ui 8080
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import secrets
import time
from collections import deque
from typing import Any

from . import __version__
from .consumer import Consumer, Job, JobError
from .names import rank_for, ship_name
from .node import PeerNode
from .tasks import TASKS
from .voice import INTENTS, VoiceBrain

log = logging.getLogger("kemi.webui")

MAX_BODY = 5 * 1024 * 1024
PROVIDER_REFRESH = 5.0
MAX_JOBS_KEPT = 50


def _preview(results: list[Any]) -> list[Any]:
    """First items only - the full set is downloadable per job."""
    return results[:50]


@functools.lru_cache(maxsize=1)
def _icon_png() -> bytes:
    """A 512x512 app icon (dark brand square + a green disc) encoded as PNG in
    pure Python — no Pillow, so it works in a stdlib-only install."""
    import struct
    import zlib

    size, r = 512, 150
    cx = cy = size / 2
    bg, fg = (13, 17, 23), (63, 185, 80)
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # PNG filter type 0 for this scanline
        for x in range(size):
            inside = (x - cx) ** 2 + (y - cy) ** 2 <= r * r
            raw += bytes(fg if inside else bg)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


class WebUI:
    def __init__(self, node: PeerNode, host: str = "127.0.0.1", port: int = 8080):
        self.node = node
        self.consumer = Consumer(node)
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._providers_cache: dict[str, list[dict[str, Any]]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._history: deque[tuple[float, float]] = deque(maxlen=240)
        self._chat: dict[str, Any] = {"log": [], "live": "", "busy": False}
        self._chat_history: list[tuple[str, str]] = []
        self._loops: list[asyncio.Task] = []
        brain_path = getattr(getattr(node, "brain", None), "path", None)
        self._voice = VoiceBrain(
            path=os.path.join(os.path.dirname(brain_path), "voice.json")
            if brain_path else None)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._loops.append(asyncio.create_task(self._providers_loop()))
        log.info("dashboard on http://%s:%s/", self.host, self.port)

    async def stop(self) -> None:
        for task in self._loops:
            task.cancel()
        for task in self._loops:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._loops.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def _prometheus(self) -> str:
        """Prometheus text-format metrics for fleet operators (T13)."""
        node = self.node
        balance = node.ledger.balance(node.identity.node_id)
        earned = node.ledger.total_earned(node.identity.node_id)
        lines = [
            "# HELP kemi_balance Current credit balance",
            "# TYPE kemi_balance gauge",
            f"kemi_balance {balance}",
            "# HELP kemi_total_earned Lifetime credits earned",
            "# TYPE kemi_total_earned counter",
            f"kemi_total_earned {earned}",
            "# HELP kemi_ledger_txs Transactions in the local replica",
            "# TYPE kemi_ledger_txs gauge",
            f"kemi_ledger_txs {node.ledger.tx_count()}",
            "# HELP kemi_dht_contacts Known DHT contacts",
            "# TYPE kemi_dht_contacts gauge",
            f"kemi_dht_contacts {len(node.dht.table)}",
            "# HELP kemi_relay_sessions Relay sessions hosted",
            "# TYPE kemi_relay_sessions gauge",
            f"kemi_relay_sessions {len(node._relay_sessions)}",
        ]
        for key, value in node.metrics.items():
            lines += [f"# TYPE kemi_{key} counter", f"kemi_{key} {value}"]
        return "\n".join(lines) + "\n"

    def _invite_code(self) -> str:
        from .cli import _guess_lan_ip
        from .invite import make_invite
        from .names import ship_name as _ship

        return make_invite([(_guess_lan_ip(), self.node.port)],
                           note=_ship(self.node.identity.node_id))

    # -- background state ------------------------------------------------------

    async def _providers_loop(self) -> None:
        while True:
            for task_name in list(TASKS):
                try:
                    self._providers_cache[task_name] = \
                        await self.consumer.list_providers(task_name)
                except Exception:
                    pass
            try:
                self._history.append(
                    (time.time(),
                     self.node.ledger.balance(self.node.identity.node_id)))
            except Exception:
                pass
            await asyncio.sleep(PROVIDER_REFRESH)

    def _state(self) -> dict[str, Any]:
        node = self.node
        merged: dict[str, dict[str, Any]] = {}
        for task_name, providers in self._providers_cache.items():
            for p in providers:
                entry = merged.setdefault(p["node_id"], {**p, "tasks": []})
                if task_name not in entry["tasks"]:
                    entry["tasks"].append(task_name)
        provider_rows = [
            {
                "id": p["node_id"],
                "name": ship_name(p["node_id"]),
                "price": p["price"],
                "host": p.get("host", ""),
                "port": p.get("port", 0),
                "relay": bool(p.get("relay")),
                "e2e": bool(p.get("e2e")),
                "rep": round(node.reputation.score(p["node_id"]), 3),
                "cpu": (p.get("resources") or {}).get("cpu_count"),
                "gpus": len((p.get("resources") or {}).get("gpus") or []),
                "stream": bool(p.get("stream")),
                "model": p.get("model"),
                "tasks": sorted(p["tasks"]),
            }
            for p in merged.values()
        ]
        provider_rows.sort(key=lambda p: (-p["rep"], p["price"]))
        jobs = [
            {k: v for k, v in j.items() if not k.startswith("_")}
            for j in sorted(self._jobs.values(), key=lambda j: -j["started"])
        ][:MAX_JOBS_KEPT]
        return {
            "version": __version__,
            "now": time.time(),
            "node": {
                "id": node.identity.node_id,
                "short": node.identity.short_id,
                "name": ship_name(node.identity.node_id),
                "rank": "{1} {0}".format(*rank_for(
                    node.ledger.total_earned(node.identity.node_id))),
                "port": node.port,
                "provide": node.provide,
                "price": node.price,
                "invite": self._invite_code(),
                "relayed": node._relay_endpoint is not None,
                "tasks": node.supported_tasks,
            },
            "balance": node.ledger.balance(node.identity.node_id),
            "ledger": {
                "txs": node.ledger.tx_count(),
                "recent": [
                    {**tx, "from_name": ship_name(tx["from"]),
                     "to_name": ship_name(tx["to"])}
                    for tx in node.ledger.recent_txs(15)
                ],
                "flagged": node.ledger.double_spenders(),
            },
            "dht_contacts": len(node.dht.table),
            "known_tasks": sorted(TASKS),
            "providers": provider_rows,
            "reputation": {nid: {**rep, "name": ship_name(nid)}
                           for nid, rep in node.reputation.snapshot().items()},
            "jobs": jobs,
            "history": [[round(ts, 1), round(balance, 4)]
                        for ts, balance in self._history],
            "chat": {"log": self._chat["log"][-30:],
                     "live": self._chat["live"], "busy": self._chat["busy"]},
            "brain": (node.brain.snapshot_ui()
                      if getattr(node, "brain", None) is not None else None),
        }

    # -- job execution -----------------------------------------------------------

    def _submit_job(self, spec: dict[str, Any]) -> dict[str, Any]:
        job = Job(
            task=str(spec.get("task", "")),
            items=spec.get("items"),
            params=dict(spec.get("params") or {}),
            chunk_size=max(1, int(spec.get("chunk_size", 4))),
            redundancy=max(1, int(spec.get("redundancy", 1))),
        )
        if not isinstance(job.items, list) or not job.items:
            raise ValueError("items must be a non-empty JSON array")
        if job.task not in TASKS:
            raise ValueError(f"unknown task: {job.task!r}")
        job_id = secrets.token_hex(4)
        entry = {
            "id": job_id, "task": job.task, "items": len(job.items),
            "redundancy": job.redundancy, "status": "running",
            "started": time.time(), "finished": None,
            "spent": None, "error": None, "results": None,
        }
        self._jobs[job_id] = entry
        while len(self._jobs) > MAX_JOBS_KEPT:
            oldest = min(self._jobs.values(), key=lambda j: j["started"])
            del self._jobs[oldest["id"]]
        asyncio.ensure_future(self._run_job(entry, job))
        return entry

    async def _run_job(self, entry: dict[str, Any], job: Job) -> None:
        try:
            if job.task == "ai.generate" and job.redundancy == 1:
                try:
                    await self._run_streaming(entry, job)
                    return
                except JobError:
                    if entry.get("partial"):
                        raise  # tokens already flowed and were paid for
                    # no streaming provider: fall through to the batch path
            report = await self.consumer.run_job(job)
            entry["_full_results"] = report.results
            entry.update(
                status="done",
                spent=report.spent,
                results=_preview(report.results),
                encrypted=report.encrypted_chunks,
                providers=len(report.providers_used),
            )
        except JobError as exc:
            entry.update(status="failed", error=str(exc))
        except Exception as exc:
            log.exception("dashboard job crashed")
            entry.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            entry["finished"] = time.time()

    async def _run_streaming(self, entry: dict[str, Any], job: Job) -> None:
        """Drive ai.generate through the live token stream so the dashboard
        shows the model's output growing in real time."""
        partials: dict[int, str] = {}
        async for event in self.consumer.stream_generate(job.items, job.params):
            if event.get("done"):
                entry["_full_results"] = event["results"]
                entry.update(
                    status="done",
                    spent=event["spent"],
                    results=_preview(event["results"]),
                    encrypted=len(job.items),
                    providers=1,
                )
                entry.pop("partial", None)
                return
            partials[event["item"]] = partials.get(event["item"], "") + event["token"]
            entry["partial"] = " ⏵ ".join(
                partials[idx] for idx in sorted(partials))[-400:]
        raise JobError("stream ended without a final summary")

    # -- the captain's voice bridge -------------------------------------------

    def _voice_command(self, text: str) -> dict[str, Any]:
        """Understand a spoken order via the voice synapse net and act on it."""
        text = text.strip()
        if not text or len(text) > 500:
            raise ValueError("command must be 1-500 characters")
        got = self._voice.classify(text)
        intent, node = got["intent"], self.node
        say, done = "", True
        if intent == "durum":
            bal = node.ledger.balance(node.identity.node_id)
            earned = node.ledger.total_earned(node.identity.node_id)
            say = (f"Bakiye {bal:.2f} kredi, toplam kazanç {earned:.2f}. "
                   f"Filoda {len(self._merged_providers())} sağlayıcı görünüyor.")
        elif intent == "saglayicilar":
            rows = self._merged_providers()
            if rows:
                top = ", ".join(f"{ship_name(p['node_id'])} ({p['price']:.2f} kredi)"
                                for p in rows[:4])
                say = f"{len(rows)} gemi hazır: {top}."
            else:
                say = "Şu an keşfedilmiş sağlayıcı yok kaptan."
        elif intent == "is_hash":
            try:
                entry = self._submit_job({"task": "hash.sha256",
                                          "items": ["kemi", "filo", "ruzgar"],
                                          "chunk_size": 4, "redundancy": 1})
                say = f"Emredersiniz. Hash işi {entry['id']} filoya gönderildi."
            except (ValueError, JobError) as exc:
                say, done = f"İş gönderilemedi: {exc}", False
        elif intent == "sor":
            question = got["payload"] or text
            try:
                self._submit_chat(question)
                say = "Filoya soruldu, yanıt sohbet panelinde akacak."
            except ValueError as exc:
                say, done = str(exc), False
        elif intent == "dil":
            say = "Dil değiştiriliyor."
        elif intent == "davet":
            say = "Davet komutu panoya kopyalandı, paylaşabilirsiniz."
        elif intent == "beyin":
            b = node.brain.snapshot_ui() if getattr(node, "brain", None) else None
            if b:
                say = (f"Sinaps ağı {b['steps']} adım eğitildi"
                       + (f", isabet yüzde {round(b['accuracy'] * 100)}"
                          if b.get("accuracy") is not None else "")
                       + (". Sağlayıcı seçiminde aktif."
                          if b["trained"] else ". Hâlâ ısınıyor."))
            else:
                say = "Bu düğümde beyin yok."
        elif intent == "yardim":
            say = ("Şunları söyleyebilirsiniz: durum raporu, sağlayıcıları "
                   "göster, hash işi gönder, filoya sor, dili değiştir, "
                   "davet kodu, beyin durumu.")
        else:
            done = False
            say = "Anlayamadım kaptan. Aşağıdan doğru komutu seçerseniz öğrenirim."
        return {"ok": done, "intent": intent, "confidence": got["confidence"],
                "say": say, "intents": INTENTS}

    def _merged_providers(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for providers in self._providers_cache.values():
            for p in providers:
                merged.setdefault(p["node_id"], p)
        return sorted(merged.values(), key=lambda p: p["price"])

    def _submit_chat(self, message: str) -> None:
        if self._chat["busy"]:
            raise ValueError("a reply is already streaming; wait for it")
        message = message.strip()
        if not message or len(message) > 4000:
            raise ValueError("message must be 1-4000 characters")
        self._chat["busy"] = True
        self._chat["live"] = ""
        self._chat["log"].append({"role": "you", "text": message})
        asyncio.ensure_future(self._run_chat_turn(message))

    async def _run_chat_turn(self, message: str) -> None:
        from .chat import chat_once
        from .names import ship_name as _ship

        def on_token(token: str) -> None:
            self._chat["live"] += token

        try:
            reply, spent, provider = await chat_once(
                self.consumer, self._chat_history, message, max_tokens=160,
                on_token=on_token)
            self._chat["log"].append({"role": "fleet", "text": reply,
                                      "cost": spent, "ship": _ship(provider)})
        except JobError as exc:
            self._chat["log"].append({"role": "error", "text": str(exc)})
        except Exception as exc:
            log.exception("dashboard chat turn crashed")
            self._chat["log"].append({"role": "error",
                                      "text": f"{type(exc).__name__}: {exc}"})
        finally:
            self._chat["live"] = ""
            self._chat["busy"] = False
            del self._chat["log"][:-60]

    # -- HTTP plumbing --------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=15.0)
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                raise ValueError("bad request line")
            method, path = parts[0], parts[1]
            content_length = 0
            while True:
                header = await asyncio.wait_for(reader.readline(), timeout=15.0)
                if header in (b"\r\n", b"\n", b""):
                    break
                name, _, value = header.decode("latin-1").partition(":")
                if name.strip().lower() == "content-length":
                    content_length = min(int(value.strip()), MAX_BODY + 1)
            body = await reader.readexactly(content_length) if content_length else b""
            if content_length > MAX_BODY:
                await self._respond(writer, 413, {"error": "body too large"})
                return
            await self._route(writer, method, path, body)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                ConnectionError, ValueError, OSError):
            pass
        except Exception:
            log.exception("dashboard request failed")
        finally:
            writer.close()

    async def _route(self, writer: asyncio.StreamWriter, method: str,
                     path: str, body: bytes) -> None:
        if method == "GET" and path in ("/", "/index.html"):
            await self._respond(writer, 200, _PAGE, content_type="text/html; charset=utf-8")
        elif method == "GET" and path == "/manifest.webmanifest":
            await self._respond(writer, 200, _MANIFEST,
                                content_type="application/manifest+json")
        elif method == "GET" and path == "/sw.js":
            await self._respond(writer, 200, _SERVICE_WORKER,
                                content_type="text/javascript")
        elif method == "GET" and path == "/icon.svg":
            await self._respond(writer, 200, _ICON_SVG,
                                content_type="image/svg+xml")
        elif method == "GET" and path == "/icon.png":
            await self._respond(writer, 200, _icon_png(),
                                content_type="image/png")
        elif method == "GET" and path == "/api/state":
            await self._respond(writer, 200, self._state())
        elif method == "GET" and path == "/metrics":
            await self._respond(writer, 200, self._prometheus(),
                                content_type="text/plain; version=0.0.4")
        elif method == "GET" and path.startswith("/api/job/") and path.endswith("/results"):
            job_id = path[len("/api/job/"):-len("/results")]
            entry = self._jobs.get(job_id)
            if entry is None or entry.get("_full_results") is None:
                await self._respond(writer, 404, {"error": "no results for that job"})
            else:
                await self._respond(
                    writer, 200, entry["_full_results"],
                    extra_headers={"Content-Disposition":
                                   f'attachment; filename="kemi-{job_id}.json"'})
        elif method == "POST" and path == "/api/chat":
            try:
                spec = json.loads(body.decode("utf-8"))
                self._submit_chat(str(spec.get("message", "")))
                await self._respond(writer, 200, {"ok": True})
            except (ValueError, json.JSONDecodeError, TypeError) as exc:
                await self._respond(writer, 400, {"ok": False, "error": str(exc)})
        elif method == "POST" and path == "/api/job":
            try:
                spec = json.loads(body.decode("utf-8"))
                entry = self._submit_job(spec)
                await self._respond(writer, 200, {"ok": True, "job": entry["id"]})
            except (ValueError, json.JSONDecodeError, TypeError) as exc:
                await self._respond(writer, 400, {"ok": False, "error": str(exc)})
        elif method == "POST" and path == "/api/voice":
            try:
                spec = json.loads(body.decode("utf-8"))
                await self._respond(writer, 200,
                                    self._voice_command(str(spec.get("text", ""))))
            except (ValueError, json.JSONDecodeError, TypeError) as exc:
                await self._respond(writer, 400, {"ok": False, "error": str(exc)})
        elif method == "POST" and path == "/api/voice/learn":
            try:
                spec = json.loads(body.decode("utf-8"))
                self._voice.learn(str(spec.get("text", "")),
                                  str(spec.get("intent", "")))
                await self._respond(writer, 200, {"ok": True})
            except (ValueError, json.JSONDecodeError, TypeError) as exc:
                await self._respond(writer, 400, {"ok": False, "error": str(exc)})
        else:
            await self._respond(writer, 404, {"error": "not found"})

    async def _respond(self, writer: asyncio.StreamWriter, status: int,
                       payload: Any, content_type: str = "application/json",
                       extra_headers: dict[str, str] | None = None) -> None:
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = payload
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
                  413: "Payload Too Large"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(data)}\r\n"
        )
        for name, value in (extra_headers or {}).items():
            head += f"{name}: {value}\r\n"
        head += "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
        writer.write(head.encode("latin-1") + data)
        await writer.drain()


_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0d1117">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Kemi">
<meta name="mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<title>kemi — fleet panel</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --line:#21262d; --fg:#e6edf3;
          --dim:#8b949e; --acc:#3fb950; --warn:#f85149; --link:#58a6ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  header { display:flex; align-items:baseline; gap:16px; padding:14px 22px;
           border-bottom:1px solid var(--line); flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; }
  header .balance { margin-left:auto; font-size:22px; color:var(--acc); }
  header .dim, .dim { color:var(--dim); }
  main { display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr));
         gap:14px; padding:14px 22px; }
  @media (max-width:640px) {
    main { grid-template-columns:1fr; padding:10px; gap:10px; }
    header { padding:10px; gap:8px; }
    header h1 { font-size:16px; }
    header .balance { font-size:18px; }
    #welcome { padding:6px 10px; font-size:13px; }
    input, select, textarea, button { font-size:16px; }  /* avoid iOS zoom */
  }
  section { background:var(--card); border:1px solid var(--line);
            border-radius:8px; padding:14px 16px; overflow-x:auto; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em;
       color:var(--dim); margin:0 0 10px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:var(--dim); font-weight:normal;
       border-bottom:1px solid var(--line); padding:3px 8px 3px 0; }
  td { padding:4px 8px 4px 0; border-bottom:1px solid var(--line); }
  tr:last-child td { border-bottom:none; }
  .ok { color:var(--acc); } .bad { color:var(--warn); } .lnk { color:var(--link); }
  .tag { background:var(--line); border-radius:4px; padding:1px 6px;
         font-size:11px; margin-right:4px; white-space:nowrap; }
  form { display:grid; gap:8px; }
  label { color:var(--dim); font-size:12px; }
  input, select, textarea, button {
    background:var(--bg); color:var(--fg); border:1px solid var(--line);
    border-radius:6px; padding:7px 9px; font:inherit; width:100%; }
  textarea { min-height:64px; resize:vertical; }
  .row { display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; }
  button { background:#1f6feb; border:none; cursor:pointer; font-weight:bold; }
  button:hover { filter:brightness(1.15); }
  button.tpl { background:var(--line); color:var(--fg); font-weight:normal;
               width:auto; padding:3px 9px; margin:2px; font-size:12px; }
  #jobmsg { min-height:18px; font-size:12px; }
  footer { padding:8px 22px 20px; color:var(--dim); font-size:12px; }
</style></head><body>
<header>
  <h1>kemi <span class="dim" data-i18n="panel">fleet panel</span></h1>
  <span id="nodeinfo" class="dim"></span>
  <button id="invitebtn" type="button" data-i18n="invite"
          style="width:auto;padding:7px 14px;margin-left:8px">⚓ Invite a friend</button>
  <button id="langbtn" type="button" title="Türkçe / English"
          style="width:auto;padding:7px 12px">TR</button>
  <span class="balance"><span id="balance">…</span>
        <span class="dim" data-i18n="credits">credits</span></span>
</header>
<p id="welcome" style="margin:0;padding:6px 22px;color:var(--dim);border-bottom:1px solid var(--line)">
  <span data-i18n="welcome">Welcome aboard! Ask the AI below, or share an invite so friends pool their computers with yours.</span>
</p>
<main>
  <section>
    <h2 data-i18n="providers">Providers (live)</h2>
    <table><thead><tr>
      <th>ship</th><th>price</th><th>rep</th><th>cpu/gpu</th><th>path</th><th>tasks</th>
    </tr></thead><tbody id="providers"></tbody></table>
  </section>
  <section>
    <h2 data-i18n="submit">Submit a job</h2>
    <div id="templates" style="margin-bottom:10px">
      <span class="dim" style="font-size:12px">templates:</span>
      <button type="button" class="tpl" data-tpl="hash">hash text</button>
      <button type="button" class="tpl" data-tpl="wordcount">word count</button>
      <button type="button" class="tpl" data-tpl="ai">ask AI</button>
      <button type="button" class="tpl" data-tpl="embed">embed (RAG)</button>
      <button type="button" class="tpl" data-tpl="aggregate">aggregate data</button>
    </div>
    <form id="jobform">
      <div class="row">
        <div><label>task</label><select id="task"></select></div>
        <div><label>chunk size</label><input id="chunk" type="number" value="4" min="1"></div>
        <div><label>redundancy</label><input id="redundancy" type="number" value="1" min="1"></div>
      </div>
      <div><label>items (JSON array)</label>
        <textarea id="items">["hello", "world"]</textarea></div>
      <div><label>params (JSON object)</label>
        <input id="params" value="{}"></div>
      <button type="submit" data-i18n="send">send to the fleet</button>
      <div id="jobmsg"></div>
    </form>
    <table><thead><tr>
      <th>job</th><th>task</th><th>items</th><th>status</th><th>cost</th><th>result</th>
    </tr></thead><tbody id="jobs"></tbody></table>
  </section>
  <section>
    <h2 data-i18n="chat">Chat with the fleet</h2>
    <div id="chatlog" style="max-height:260px;overflow-y:auto;margin-bottom:10px"></div>
    <form id="chatform" style="grid-template-columns:1fr auto;display:grid;gap:8px">
      <input id="chatmsg" placeholder="ask the fleet's AI anything…" autocomplete="off">
      <button type="submit" style="width:auto;padding:7px 14px">send</button>
    </form>
  </section>
  <section>
    <h2 data-i18n="voice">Captain's bridge (voice)</h2>
    <div style="display:flex;gap:10px;align-items:center">
      <button id="micbtn" type="button" title="konuş / speak"
              style="width:auto;padding:9px 16px;font-size:18px">🎙</button>
      <span id="voicestatus" class="dim" style="font-size:12px"></span>
    </div>
    <div id="voicelog" style="max-height:170px;overflow-y:auto;margin:10px 0"></div>
    <form id="voiceform" style="grid-template-columns:1fr auto;display:grid;gap:8px">
      <input id="voicetext" autocomplete="off"
             placeholder="…ya da yazın: durum raporu / hash işi gönder">
      <button type="submit" style="width:auto;padding:7px 14px"
              data-i18n="voicesend">send</button>
    </form>
    <div id="voicefix" style="display:none;margin-top:8px">
      <span class="dim" style="font-size:12px" data-i18n="voicefixlabel">I meant:</span>
      <div id="voiceintents" style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px"></div>
    </div>
  </section>
  <section>
    <h2 data-i18n="ledger">Ledger (latest transfers)</h2>
    <svg id="spark" width="100%" height="48" viewBox="0 0 400 48"
         preserveAspectRatio="none" style="display:block;margin-bottom:10px">
      <polyline id="sparkline" fill="none" stroke="#3fb950" stroke-width="1.5"/>
    </svg>
    <table><thead><tr>
      <th>from</th><th>to</th><th>amount</th><th>when</th>
    </tr></thead><tbody id="txs"></tbody></table>
    <div id="flagged"></div>
  </section>
  <section>
    <h2 data-i18n="reputation">Reputation (as this node sees it)</h2>
    <table><thead><tr>
      <th>ship</th><th>score</th><th>good</th><th>bad</th><th>events</th>
    </tr></thead><tbody id="reputation"></tbody></table>
  </section>
  <section>
    <h2 data-i18n="map">Fleet map</h2>
    <svg id="fleetmap" width="100%" height="260" viewBox="0 0 400 260"
         preserveAspectRatio="xMidYMid meet"></svg>
  </section>
  <section id="braincard" style="display:none">
    <h2 data-i18n="brain">Synapse brain (self-training)</h2>
    <svg id="brainviz" width="100%" height="210" viewBox="0 0 400 210"
         preserveAspectRatio="xMidYMid meet"></svg>
    <svg id="brainloss" width="100%" height="34" viewBox="0 0 400 34"
         preserveAspectRatio="none" style="display:block;margin-top:6px">
      <polyline id="brainlosspoly" fill="none" stroke="#f85149" stroke-width="1.2"/>
    </svg>
    <div id="brainstats" class="dim" style="font-size:12px;margin-top:4px"></div>
  </section>
</main>
<footer id="footer"></footer>
<script>
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v).replace(/[&<>"]/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const short = (id) => esc(String(id).slice(0, 12));
const ago = (ts, now) => { const d = Math.max(0, now - ts);
  return d < 60 ? Math.round(d) + 's' : Math.round(d / 60) + 'm'; };

let knownTasks = [];

let _inviteCode = '';
function render(s) {
  $('#balance').textContent = s.balance.toFixed(2);
  _inviteCode = s.node.invite || '';
  $('#nodeinfo').textContent = `⚓ ${s.node.name} · ${s.node.rank} · port ${s.node.port}` +
    (s.node.provide ? ` · provider (${s.node.price} cr/item)` : ' · peer') +
    (s.node.relayed ? ' · via relay' : '');
  $('#footer').textContent = `kemi v${s.version} · ledger: ${s.ledger.txs} txs · ` +
    `DHT: ${s.dht_contacts} contacts · refreshes every 2s`;

  $('#providers').innerHTML = s.providers.map(p => `<tr>
    <td title="${esc(p.id)}">⚓ ${esc(p.name || short(p.id))}</td>
    <td>${p.price.toFixed(2)}</td>
    <td class="${p.rep >= 0.5 ? 'ok' : 'bad'}">${p.rep.toFixed(2)}</td>
    <td>${p.cpu ?? '?'} / ${p.gpus}</td>
    <td>${p.relay ? '<span class="tag">relay</span>' : '<span class="tag">direct</span>'}` +
      `${p.e2e ? '<span class="tag ok">e2e</span>' : ''}` +
      `${p.stream ? '<span class="tag lnk">stream</span>' : ''}</td>
    <td>${p.tasks.map(t => `<span class="tag">${esc(t)}</span>`).join('')}</td>
  </tr>`).join('') || '<tr><td colspan="6" class="dim">no providers discovered yet…</td></tr>';

  if (JSON.stringify(knownTasks) !== JSON.stringify(s.known_tasks)) {
    knownTasks = s.known_tasks;
    $('#task').innerHTML = knownTasks.map(t => `<option>${esc(t)}</option>`).join('');
  }

  $('#jobs').innerHTML = s.jobs.map(j => {
    let detail;
    if (j.status === 'failed') detail = esc(String(j.error).slice(0, 60));
    else if (j.status === 'running' && j.partial)
      detail = `<span class="lnk">⚡ ${esc(j.partial.slice(-70))}</span>`;
    else if (j.results)
      detail = `<a class="lnk" href="/api/job/${esc(j.id)}/results" download>⬇ download</a> ` +
               esc(JSON.stringify(j.results).slice(0, 45)) + '…';
    else detail = '…';
    return `<tr>
      <td>${esc(j.id)}</td><td>${esc(j.task)}</td><td>${j.items}</td>
      <td class="${j.status === 'done' ? 'ok' : j.status === 'failed' ? 'bad' : 'lnk'}">` +
        `${esc(j.status)}</td>
      <td>${j.spent != null ? j.spent.toFixed(2) : '—'}</td>
      <td class="dim" title="${j.error ? esc(j.error) : ''}">${detail}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="dim">no jobs yet</td></tr>';

  const chat = s.chat || {log: [], live: '', busy: false};
  const liveRow = chat.busy
    ? `<div><span class="lnk">fleet ⚡</span> <span class="dim">${esc(chat.live)}▋</span></div>`
    : '';
  $('#chatlog').innerHTML = chat.log.map(m => {
    if (m.role === 'you') return `<div><span class="ok">you ⚓</span> ${esc(m.text)}</div>`;
    if (m.role === 'error') return `<div class="bad">⚠ ${esc(m.text)}</div>`;
    return `<div><span class="lnk">fleet</span> ${esc(m.text)} ` +
           `<span class="dim">[${(m.cost ?? 0).toFixed(2)} cr · ${esc(m.ship || '')}]</span></div>`;
  }).join('') + liveRow ||
    '<div class="dim">no conversation yet - say hello</div>';
  if (chat.busy || chat.log.length !== window._chatLen) {
    window._chatLen = chat.log.length;
    $('#chatlog').scrollTop = $('#chatlog').scrollHeight;
  }
  // In voice mode, read new fleet replies aloud (never the backlog).
  if (window._chatSpoken === undefined) window._chatSpoken = chat.log.length;
  while (window._chatSpoken < chat.log.length) {
    const m = chat.log[window._chatSpoken++];
    if (window._voiceOn && m.role === 'fleet') speak(m.text.slice(0, 220));
  }

  if (s.history.length > 1) {
    const xs = s.history.map(h => h[0]), ys = s.history.map(h => h[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const y0 = Math.min(...ys), y1 = Math.max(...ys);
    const pts = s.history.map(h => {
      const x = x1 > x0 ? (h[0] - x0) / (x1 - x0) * 396 + 2 : 200;
      const y = y1 > y0 ? 44 - (h[1] - y0) / (y1 - y0) * 40 : 24;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    $('#sparkline').setAttribute('points', pts.join(' '));
  }

  // Fleet map: this ship at the centre, providers on a ring around it.
  const map = $('#fleetmap');
  const cx = 200, cy = 130, R = 95;
  const peers = s.providers.slice(0, 16);
  let mapSvg = '';
  peers.forEach((p, i) => {
    const ang = (i / Math.max(1, peers.length)) * 2 * Math.PI - Math.PI / 2;
    const x = cx + R * Math.cos(ang), y = cy + R * Math.sin(ang);
    const colour = p.rep >= 0.5 ? '#3fb950' : '#f85149';
    const dash = p.relay ? ' stroke-dasharray="4 3"' : '';
    mapSvg += `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" `
            + `stroke="#30363d" stroke-width="1"${dash}/>`;
    mapSvg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="7" fill="${colour}">`
            + `<title>${esc(p.name)} · ${p.price.toFixed(2)} cr · rep ${p.rep}`
            + `${p.relay ? ' · relay' : ''}</title></circle>`;
    mapSvg += `<text x="${x.toFixed(1)}" y="${(y + 18).toFixed(1)}" fill="#8b949e" `
            + `font-size="8" text-anchor="middle">${esc((p.name||'').slice(0,12))}</text>`;
  });
  mapSvg += `<circle cx="${cx}" cy="${cy}" r="11" fill="#1f6feb"/>`
          + `<text x="${cx}" y="${cy + 26}" fill="#e6edf3" font-size="9" `
          + `text-anchor="middle">${esc(s.node.name)} (you)</text>`;
  if (!peers.length) {
    mapSvg += `<text x="${cx}" y="${cy + 50}" fill="#8b949e" font-size="10" `
            + `text-anchor="middle">no other ships discovered yet</text>`;
  }
  map.innerHTML = mapSvg;

  $('#txs').innerHTML = s.ledger.recent.map(t => `<tr>
    <td title="${esc(t.from)}">${esc(t.from_name || short(t.from))}</td>` +
    `<td title="${esc(t.to)}">${esc(t.to_name || short(t.to))}</td>
    <td>${t.amount.toFixed(2)}</td><td class="dim">${ago(t.ts, s.now)} ago</td>
  </tr>`).join('') || '<tr><td colspan="4" class="dim">no transfers yet</td></tr>';
  $('#flagged').innerHTML = s.ledger.flagged.length
    ? `<p class="bad">⚑ double-spend evidence: ${s.ledger.flagged.map(short).join(', ')}</p>` : '';

  const reps = Object.entries(s.reputation);
  $('#reputation').innerHTML = reps.map(([id, r]) => `<tr>
    <td title="${esc(id)}">${esc(r.name || short(id))}</td>
    <td class="${r.score >= 0.5 ? 'ok' : 'bad'}">${r.score.toFixed(2)}</td>
    <td>${r.good}</td><td>${r.bad}</td><td>${r.events}</td>
  </tr>`).join('') || '<tr><td colspan="5" class="dim">no interactions yet</td></tr>';

  if (s.brain) renderBrain(s.brain);
}

// The ship's self-training brain: neurons as circles, synapses as lines
// (green = excitatory, red = inhibitory, thickness = |weight|).
function renderBrain(b) {
  $('#braincard').style.display = '';
  const W = 400, H = 210, L = b.sizes.length;
  const xs = b.sizes.map((_, i) => 46 + i * (W - 92) / (L - 1));
  const pos = b.sizes.map((n, i) =>
    Array.from({length: n}, (_, j) => ({x: xs[i], y: H / (n + 1) * (j + 1)})));
  let svg = '';
  b.weights.forEach((layer, li) => layer.forEach((row, k) => row.forEach((w, j) => {
    const a = Math.min(1, Math.abs(w) / 2.5);
    if (a < 0.03) return;
    svg += `<line x1="${pos[li][j].x}" y1="${pos[li][j].y}" ` +
           `x2="${pos[li + 1][k].x}" y2="${pos[li + 1][k].y}" ` +
           `stroke="${w >= 0 ? '#3fb950' : '#f85149'}" ` +
           `stroke-width="${(0.3 + 2.4 * a).toFixed(2)}" ` +
           `opacity="${(0.12 + 0.8 * a).toFixed(2)}"/>`;
  })));
  pos.forEach((layer, i) => layer.forEach((p, j) => {
    svg += `<circle cx="${p.x}" cy="${p.y}" r="6" fill="#1f6feb" stroke="#30363d"/>`;
    if (i === 0) svg += `<text x="${p.x - 10}" y="${p.y + 3}" fill="#8b949e" ` +
      `font-size="8" text-anchor="end">${esc(b.features[j] || '')}</text>`;
  }));
  const o = pos[L - 1][0];
  svg += `<text x="${o.x + 10}" y="${o.y + 3}" fill="#8b949e" font-size="8">success?</text>`;
  $('#brainviz').innerHTML = svg;
  const ys = b.loss_history || [];
  if (ys.length > 1) {
    const top = Math.max(...ys, 0.01);
    $('#brainlosspoly').setAttribute('points', ys.map((v, i) =>
      `${(i / (ys.length - 1) * 396 + 2).toFixed(1)},${(32 - v / top * 28).toFixed(1)}`
    ).join(' '));
  }
  $('#brainstats').textContent =
    `${b.steps} training steps · loss ${b.loss ?? '—'} · ` +
    `accuracy ${b.accuracy != null ? Math.round(b.accuracy * 100) + '%' : '—'} · ` +
    (b.trained ? 'active in provider ranking' : 'warming up');
}

async function refresh() {
  try { render(await (await fetch('/api/state')).json()); }
  catch (e) { $('#footer').textContent = 'node unreachable…'; }
}

$('#jobform').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const msg = $('#jobmsg');
  try {
    const spec = {
      task: $('#task').value,
      items: JSON.parse($('#items').value),
      params: JSON.parse($('#params').value),
      chunk_size: parseInt($('#chunk').value, 10),
      redundancy: parseInt($('#redundancy').value, 10),
    };
    const r = await (await fetch('/api/job',
      { method: 'POST', body: JSON.stringify(spec) })).json();
    msg.className = r.ok ? 'ok' : 'bad';
    msg.textContent = r.ok ? `job ${r.job} sent to the fleet` : `error: ${r.error}`;
    refresh();
  } catch (e) { msg.className = 'bad'; msg.textContent = 'error: ' + e.message; }
});

// ---- the captain's voice bridge ----
const VOICE_LABELS = {durum:'durum raporu', saglayicilar:'sağlayıcılar',
  is_hash:'hash işi', sor:'filoya sor', dil:'dil değiştir',
  davet:'davet kodu', beyin:'beyin durumu', yardim:'yardım'};
let _lastUtterance = '';
window._voiceOn = false;
function speak(text) {
  try {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = 'tr-TR';  // the bridge answers in Turkish
    speechSynthesis.speak(u);
  } catch (e) {}
}
function vlog(who, text, cls) {
  const d = document.createElement('div');
  d.innerHTML = `<span class="${cls || 'lnk'}">${esc(who)}</span> ${esc(text)}`;
  $('#voicelog').appendChild(d);
  $('#voicelog').scrollTop = $('#voicelog').scrollHeight;
}
async function voiceCommand(text) {
  _lastUtterance = text;
  vlog('siz 🎙', text, 'ok');
  $('#voicefix').style.display = 'none';
  try {
    const r = await (await fetch('/api/voice',
      {method: 'POST', body: JSON.stringify({text})})).json();
    const tag = r.intent
      ? ` [${VOICE_LABELS[r.intent] || r.intent} · %${Math.round(r.confidence * 100)}]` : '';
    vlog('köprü ⚓', r.say + tag, r.ok ? 'lnk' : 'bad');
    speak(r.say);
    $('#voiceintents').innerHTML = (r.intents || []).map(i =>
      `<button type="button" class="tpl" data-vi="${i}">${VOICE_LABELS[i] || i}</button>`
    ).join('');
    document.querySelectorAll('#voiceintents button').forEach(b =>
      b.addEventListener('click', async () => {
        await fetch('/api/voice/learn', {method: 'POST',
          body: JSON.stringify({text: _lastUtterance, intent: b.dataset.vi})});
        vlog('köprü ⚓',
             `öğrendim: "${_lastUtterance}" → ${VOICE_LABELS[b.dataset.vi]}`, 'ok');
        $('#voicefix').style.display = 'none';
      }));
    $('#voicefix').style.display = '';
    if (r.intent === 'dil') $('#langbtn').click();
    if (r.intent === 'davet') $('#invitebtn').click();
    refresh();
  } catch (e) { vlog('köprü ⚓', 'düğüme ulaşılamadı', 'bad'); }
}
$('#voiceform').addEventListener('submit', ev => {
  ev.preventDefault();
  const t = $('#voicetext').value.trim();
  if (t) { $('#voicetext').value = ''; voiceCommand(t); }
});
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SR) {
  const recog = new SR();
  recog.lang = 'tr-TR';
  recog.interimResults = false;
  recog.maxAlternatives = 1;
  recog.onresult = ev => voiceCommand(ev.results[ev.results.length - 1][0].transcript);
  recog.onend = () => { if (window._voiceOn) { try { recog.start(); } catch (e) {} } };
  recog.onerror = ev => {
    if (ev.error === 'not-allowed' || ev.error === 'service-not-allowed') {
      window._voiceOn = false;
      $('#micbtn').style.background = '';
      $('#voicestatus').textContent = 'mikrofon izni gerekli';
    }
  };
  $('#micbtn').addEventListener('click', () => {
    window._voiceOn = !window._voiceOn;
    if (window._voiceOn) {
      try { recog.start(); } catch (e) {}
      $('#micbtn').style.background = '#f85149';
      $('#voicestatus').textContent = 'dinliyorum… "durum raporu" deyin';
    } else {
      recog.stop();
      $('#micbtn').style.background = '';
      $('#voicestatus').textContent = 'mikrofon kapalı';
    }
  });
  $('#voicestatus').textContent = 'mikrofona tıklayıp konuşun — ya da yazın';
} else {
  $('#micbtn').disabled = true;
  $('#voicestatus').textContent =
    'bu tarayıcıda ses tanıma yok (Chrome deneyin) — yazarak komut verin';
}

$('#chatform').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const box = $('#chatmsg');
  const message = box.value.trim();
  if (!message) return;
  box.value = '';
  await fetch('/api/chat', { method: 'POST', body: JSON.stringify({ message }) });
  refresh();
});

const TEMPLATES = {
  hash: {task: 'hash.sha256', chunk: 4, redundancy: 1,
         items: '["hello", "world"]', params: '{}'},
  wordcount: {task: 'text.wordcount', chunk: 4, redundancy: 1,
              items: '["the quick brown fox", "lazy dog"]', params: '{}'},
  ai: {task: 'ai.generate', chunk: 1, redundancy: 1,
       items: '["Why do P2P networks matter?"]', params: '{"max_tokens": 32}'},
  embed: {task: 'ai.embed', chunk: 8, redundancy: 1,
          items: '["first document", "second document"]', params: '{}'},
  aggregate: {task: 'data.aggregate', chunk: 1, redundancy: 1,
              items: '[[{"city":"a","n":3},{"city":"b","n":4},{"city":"a","n":1}]]',
              params: '{"group_by": "city", "field": "n", "op": "sum"}'},
};
document.querySelectorAll('.tpl').forEach(btn => btn.addEventListener('click', () => {
  const t = TEMPLATES[btn.dataset.tpl];
  if (!t) return;
  $('#task').value = t.task; $('#chunk').value = t.chunk;
  $('#redundancy').value = t.redundancy; $('#items').value = t.items;
  $('#params').value = t.params;
}));

$('#invitebtn').addEventListener('click', async () => {
  if (!_inviteCode) return;
  const cmd = 'kemi join --invite ' + _inviteCode;
  try { await navigator.clipboard.writeText(cmd); } catch (e) {}
  window.prompt('Send this to a friend — they paste it and run it to join your fleet:', cmd);
});

const I18N = {
  en: {panel:'fleet panel', invite:'⚓ Invite a friend', credits:'credits',
       welcome:'Welcome aboard! Ask the AI below, or share an invite so friends pool their computers with yours.',
       providers:'Providers (live)', submit:'Submit a job',
       chat:'Chat with the fleet', ledger:'Ledger (latest transfers)',
       reputation:'Reputation (as this node sees it)', send:'send to the fleet', map:'Fleet map',
       brain:'Synapse brain (self-training)', voice:"Captain's bridge (voice)",
       voicesend:'send', voicefixlabel:'I meant:'},
  tr: {panel:'filo paneli', invite:'⚓ Arkadaş davet et', credits:'kredi',
       welcome:'Hoş geldin! Aşağıdan yapay zekâya sor ya da bir davet paylaş; arkadaşların bilgisayarlarını seninkiyle birleştirsin.',
       providers:'Sağlayıcılar (canlı)', submit:'İş gönder',
       chat:'Filoyla sohbet et', ledger:'Defter (son transferler)',
       reputation:'İtibar (bu düğümün gözünden)', send:'filoya gönder', map:'Filo haritası',
       brain:'Sinaps ağı (kendi kendini eğitir)', voice:'Kaptan köşkü (sesli komut)',
       voicesend:'gönder', voicefixlabel:'bunu kastetmiştim:'},
};
function applyLang(lang) {
  const dict = I18N[lang] || I18N.en;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const v = dict[el.dataset.i18n];
    if (v) el.textContent = v;
  });
  document.documentElement.lang = lang;
  $('#langbtn').textContent = lang === 'tr' ? 'EN' : 'TR';
  try { localStorage.setItem('kemi-lang', lang); } catch (e) {}
}
$('#langbtn').addEventListener('click', () => {
  const cur = (localStorage.getItem('kemi-lang') || 'en');
  applyLang(cur === 'tr' ? 'en' : 'tr');
});
try { applyLang(localStorage.getItem('kemi-lang') || 'en'); } catch (e) {}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

refresh();
setInterval(refresh, 500);
</script>
</body></html>
"""

_MANIFEST = json.dumps({
    "name": "Kemi — fleet panel",
    "short_name": "Kemi",
    "description": "Decentralised peer-to-peer compute & AI on the fleet",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0d1117",
    "theme_color": "#0d1117",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml",
         "purpose": "any"},
        {"src": "/icon.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any maskable"},
    ],
})

# A tiny offline-tolerant service worker: serve the shell from cache when the
# network is down so the installed app still opens (data still needs a peer).
_SERVICE_WORKER = """
const CACHE = 'kemi-v1';
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(['/'])));
  self.skipWaiting();
});
self.addEventListener('activate', (e) => self.clients.claim());
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) return;  // always live for data
  e.respondWith(
    fetch(e.request).then((r) => {
      const copy = r.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      return r;
    }).catch(() => caches.match(e.request).then((m) => m || caches.match('/')))
  );
});
"""

# Ship icon: an anchor on the brand background. SVG scales everywhere; the
# PNG is rendered from it on demand for platforms that insist on a raster.
_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="96" fill="#0d1117"/>
  <g fill="none" stroke="#3fb950" stroke-width="28" stroke-linecap="round"
     stroke-linejoin="round">
    <circle cx="256" cy="150" r="34"/>
    <line x1="256" y1="184" x2="256" y2="396"/>
    <line x1="168" y1="232" x2="344" y2="232"/>
    <path d="M150 300 a106 106 0 0 0 212 0"/>
  </g>
</svg>"""
