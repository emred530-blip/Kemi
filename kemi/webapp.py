"""``kemi web``: a public harbor — the fleet as a web app.

Anyone opens the harbor's address in a browser, gets a small welcome
allowance of credits, and chats with the fleet — no install, no account.
Every answer is produced by another user's ship (never a central server)
and is stamped with the ship, the model and the credits it cost. The
harbor host's node pays the providers on the fleet ledger and meters each
visitor's guest wallet by exactly what their questions cost.

Decentralisation is preserved one level up: a harbor is just a lens into
the fleet, and anyone can open their own with ``kemi web`` — there can be
as many harbors as there are captains willing to fund one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Any

from .chat import chat_once
from .consumer import Consumer, JobError
from .names import ship_name

log = logging.getLogger("kemi.webapp")

MAX_BODY = 64 * 1024
MAX_GUESTS = 5000            # oldest-idle guests are evicted past this
MAX_MESSAGE = 2000
MAX_LOG = 60                 # chat entries kept per guest
ASK_COOLDOWN = 2.0           # seconds between questions per guest
MAX_CONCURRENT_CHATS = 4     # fleet-bound questions in flight, harbor-wide


def _now() -> float:
    return time.time()


class WebApp:
    """A multi-visitor chat portal backed by one consumer node."""

    def __init__(self, node, host: str = "0.0.0.0", port: int = 8090,
                 faucet: float = 10.0, state_path: str | None = None):
        self.node = node
        self.consumer = Consumer(node)
        self.host = host
        self.port = port
        self.faucet = float(faucet)
        self.state_path = state_path
        self._guests: dict[str, dict[str, Any]] = {}
        self._server: asyncio.Server | None = None
        self._chat_slots = asyncio.Semaphore(MAX_CONCURRENT_CHATS)
        self._providers_cache: list[dict[str, Any]] = []
        self._loops: list[asyncio.Task] = []
        self._load()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._loops.append(asyncio.create_task(self._providers_loop()))
        log.info("harbor open on http://%s:%s/", self.host, self.port)

    async def stop(self) -> None:
        for task in self._loops:
            task.cancel()
        for task in self._loops:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self._save()

    async def _providers_loop(self) -> None:
        while True:
            try:
                self._providers_cache = await self.consumer.list_providers("ai.generate")
            except Exception:
                self._providers_cache = []
            await asyncio.sleep(5.0)

    # -- guest accounts ------------------------------------------------------

    def _load(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            for token, g in saved.get("guests", {}).items():
                self._guests[token] = {
                    "token": token,
                    "name": str(g.get("name", "")),
                    "balance": float(g.get("balance", 0.0)),
                    "created": float(g.get("created", _now())),
                    "seen": float(g.get("seen", _now())),
                    "spent": float(g.get("spent", 0.0)),
                    "asks": int(g.get("asks", 0)),
                    "log": list(g.get("log", []))[-MAX_LOG:],
                    "history": [tuple(x) for x in g.get("history", [])][-8:],
                    "live": "", "busy": False, "last_ask": 0.0,
                }
        except (ValueError, OSError, TypeError):
            log.warning("could not read harbor state at %s; starting fresh",
                        self.state_path)

    def _save(self) -> None:
        if not self.state_path:
            return
        durable = {}
        for token, g in self._guests.items():
            durable[token] = {k: g[k] for k in
                              ("name", "balance", "created", "seen",
                               "spent", "asks", "log", "history")}
        tmp = f"{self.state_path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"guests": durable}, fh, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except OSError:
            log.warning("could not persist harbor state to %s", self.state_path)

    def _evict(self) -> None:
        while len(self._guests) > MAX_GUESTS:
            idle = min(self._guests.values(), key=lambda g: g["seen"])
            del self._guests[idle["token"]]

    def _guest(self, token: str | None) -> dict[str, Any]:
        """Resume a guest by token, or mint a new one with welcome credits."""
        if token and token in self._guests:
            guest = self._guests[token]
            guest["seen"] = _now()
            return guest
        token = secrets.token_urlsafe(18)
        guest = {
            "token": token,
            "name": ship_name(f"guest:{token}"),
            "balance": self.faucet,
            "created": _now(), "seen": _now(),
            "spent": 0.0, "asks": 0,
            "log": [], "history": [],
            "live": "", "busy": False, "last_ask": 0.0,
        }
        self._guests[token] = guest
        self._evict()
        self._save()
        return guest

    def _require_guest(self, token: str) -> dict[str, Any]:
        guest = self._guests.get(token or "")
        if guest is None:
            raise PermissionError("unknown or expired guest token")
        guest["seen"] = _now()
        return guest

    # -- chat ----------------------------------------------------------------

    def _fleet_summary(self) -> dict[str, Any]:
        rows = self._providers_cache
        models = sorted({p.get("model") for p in rows
                         if p.get("model") and p.get("model") != "mock"})
        return {
            "ships": len(rows),
            "models": models,
            "cheapest": min((p["price"] for p in rows), default=None),
        }

    def submit_ask(self, token: str, message: str) -> dict[str, Any]:
        guest = self._require_guest(token)
        message = str(message or "").strip()
        if not message or len(message) > MAX_MESSAGE:
            raise ValueError(f"message must be 1-{MAX_MESSAGE} characters")
        if guest["busy"]:
            raise ValueError("a reply is already streaming; wait for it")
        if _now() - guest["last_ask"] < ASK_COOLDOWN:
            raise ValueError("slow down a little, captain")
        if guest["balance"] <= 0:
            raise ValueError("out of credits — join the fleet with your own "
                             "ship to earn more (kemi app)")
        guest["busy"] = True
        guest["last_ask"] = _now()
        guest["live"] = ""
        guest["log"].append({"role": "you", "text": message, "ts": _now()})
        asyncio.ensure_future(self._run_ask(guest, message))
        return {"ok": True}

    async def _run_ask(self, guest: dict[str, Any], message: str) -> None:
        def on_token(token_text: str) -> None:
            guest["live"] += token_text

        try:
            async with self._chat_slots:
                history = [tuple(x) for x in guest["history"]]
                reply, spent, provider, served_model = await chat_once(
                    self.consumer, history, message, max_tokens=200,
                    on_token=on_token)
            guest["history"] = history[-8:]
            guest["balance"] = round(guest["balance"] - spent, 6)
            guest["spent"] = round(guest["spent"] + spent, 6)
            guest["asks"] += 1
            guest["log"].append({
                "role": "fleet", "text": reply, "ts": _now(),
                "cost": spent, "ship": ship_name(provider),
                "model": served_model,
            })
        except JobError as exc:
            guest["log"].append({"role": "error", "text": str(exc), "ts": _now()})
        except Exception as exc:
            log.exception("harbor ask crashed")
            guest["log"].append({"role": "error",
                                 "text": f"{type(exc).__name__}: {exc}",
                                 "ts": _now()})
        finally:
            guest["live"] = ""
            guest["busy"] = False
            del guest["log"][:-MAX_LOG]
            self._save()

    def chat_state(self, token: str) -> dict[str, Any]:
        guest = self._require_guest(token)
        return {
            "name": guest["name"],
            "balance": round(guest["balance"], 4),
            "spent": round(guest["spent"], 4),
            "busy": guest["busy"],
            "live": guest["live"],
            "log": guest["log"][-30:],
            "fleet": self._fleet_summary(),
        }

    # -- HTTP plumbing -------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
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
            log.exception("harbor request failed")
        finally:
            writer.close()

    async def _route(self, writer: asyncio.StreamWriter, method: str,
                     path: str, body: bytes) -> None:
        path, _, query = path.partition("?")
        params = dict(p.partition("=")[::2] for p in query.split("&") if p)
        token = params.get("t", "")
        try:
            if method == "GET" and path in ("/", "/index.html"):
                await self._respond(writer, 200, _PAGE,
                                    content_type="text/html; charset=utf-8")
            elif method == "POST" and path == "/api/hello":
                spec = json.loads(body.decode("utf-8") or "{}")
                guest = self._guest(str(spec.get("token") or "") or None)
                await self._respond(writer, 200, {
                    "ok": True, "token": guest["token"], "name": guest["name"],
                    "balance": round(guest["balance"], 4),
                    "faucet": self.faucet,
                    "fleet": self._fleet_summary(),
                })
            elif method == "POST" and path == "/api/ask":
                spec = json.loads(body.decode("utf-8"))
                result = self.submit_ask(str(spec.get("token") or ""),
                                         str(spec.get("message") or ""))
                await self._respond(writer, 200, result)
            elif method == "GET" and path == "/api/chat":
                await self._respond(writer, 200, self.chat_state(token))
            else:
                await self._respond(writer, 404, {"error": "not found"})
        except PermissionError as exc:
            await self._respond(writer, 401, {"ok": False, "error": str(exc)})
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            await self._respond(writer, 400, {"ok": False, "error": str(exc)})

    async def _respond(self, writer: asyncio.StreamWriter, status: int,
                       payload: Any,
                       content_type: str = "application/json") -> None:
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            data = payload.encode("utf-8") if isinstance(payload, str) else payload
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
                  404: "Not Found", 413: "Payload Too Large"}.get(status, "OK")
        head = (f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(data)}\r\n"
                "Cache-Control: no-store\r\nConnection: close\r\n\r\n")
        try:
            writer.write(head.encode("latin-1") + data)
            await writer.drain()
        except (ConnectionError, OSError):
            pass


_PAGE = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#060b16">
<title>Kemi Limanı — merkezi olmayan yapay zekâ</title>
<style>
  :root {
    --sea-deep:#060b16; --sea:#0b1424; --hull:#101c31; --hull-2:#0e1930;
    --edge:#1d2c47; --edge-soft:#16233c;
    --foam:#e9eef7; --mist:#97a3be; --faint:#5f6d8c;
    --brass:#e7b75f; --brass-deep:#c9954a; --phosphor:#57d9c0; --flare:#f27d8a;
    --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; margin:0; }
  html { scroll-behavior:smooth; }
  body {
    background:var(--sea-deep); color:var(--foam);
    font:16px/1.55 var(--sans);
    min-height:100dvh; display:flex; flex-direction:column;
  }
  /* the night sea: a horizon glow and a faint phosphorescent drift */
  .sky { position:fixed; inset:0; z-index:-1; pointer-events:none;
    background:
      radial-gradient(90rem 42rem at 50% -18rem, #172747 0%, transparent 60%),
      radial-gradient(50rem 26rem at 82% 108%, rgba(87,217,192,.07) 0%, transparent 65%),
      var(--sea-deep); }
  .sky::after { content:""; position:absolute; left:0; right:0; top:34dvh; height:1px;
    background:linear-gradient(90deg, transparent, rgba(231,183,95,.25), transparent); }

  header { display:flex; align-items:center; gap:12px;
    padding:14px clamp(14px, 4vw, 28px);
    position:sticky; top:0; z-index:10;
    background:color-mix(in srgb, var(--sea-deep) 82%, transparent);
    backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
    border-bottom:1px solid var(--edge-soft); }
  .brand { display:flex; align-items:baseline; gap:9px; }
  .brand .anchor { color:var(--brass); font-size:19px; transform:translateY(1px); }
  .brand .word { font:600 24px/1 var(--serif); letter-spacing:.02em; }
  .brand .port { font:400 13px/1 var(--sans); color:var(--mist);
    letter-spacing:.24em; text-transform:uppercase; }
  .chips { margin-left:auto; display:flex; gap:8px; align-items:center; }
  .pill { border:1px solid var(--edge); background:rgba(16,28,49,.6);
    border-radius:999px; padding:5px 13px; font-size:13px; color:var(--mist);
    white-space:nowrap; font-family:var(--sans); }
  .pill b { color:var(--brass); font-family:var(--mono);
    font-variant-numeric:tabular-nums; font-weight:600; }
  #fleetpill { color:var(--phosphor); }
  #fleetpill::before { content:""; display:inline-block; width:6px; height:6px;
    border-radius:50%; background:var(--phosphor); margin-right:7px;
    vertical-align:1px; animation:beacon 2.6s ease-in-out infinite; }
  button.pill { cursor:pointer; color:var(--foam); }
  button.pill:hover { border-color:var(--brass); }

  main { flex:1; width:100%; max-width:720px; margin:0 auto;
    padding:0 16px 132px; }

  /* hero — the harbor gate. Compacts once the conversation starts. */
  .hero { text-align:center; padding:clamp(30px, 7dvh, 64px) 8px 8px;
    transition:opacity .3s ease; }
  .hero h1 { font:600 clamp(30px, 6vw, 42px)/1.15 var(--serif);
    letter-spacing:.01em; text-wrap:balance; }
  .hero h1 em { font-style:normal; color:var(--brass); }
  .hero > p { margin:14px auto 0; max-width:34em; color:var(--mist);
    font-size:15.5px; text-wrap:pretty; }
  .board { display:flex; justify-content:center; gap:10px; flex-wrap:wrap;
    margin:26px 0 6px; }
  .cell { border:1px solid var(--edge-soft); background:rgba(14,25,48,.55);
    border-radius:12px; padding:10px 18px; min-width:104px; }
  .cell .n { font:600 20px/1.2 var(--mono); color:var(--foam);
    font-variant-numeric:tabular-nums; }
  .cell .n.live { color:var(--phosphor); }
  .cell .n.gold { color:var(--brass); }
  .cell .l { font-size:11px; color:var(--faint); letter-spacing:.14em;
    text-transform:uppercase; margin-top:3px; }
  .starters { display:flex; flex-wrap:wrap; gap:8px; justify-content:center;
    margin-top:26px; }
  .starter { border:1px solid var(--edge); background:transparent;
    color:var(--mist); border-radius:999px; padding:9px 16px; font-size:14px;
    font-family:var(--sans); cursor:pointer;
    transition:border-color .15s ease, color .15s ease, transform .15s ease; }
  .starter:hover { border-color:var(--phosphor); color:var(--foam);
    transform:translateY(-1px); }
  body.sailing .hero { padding:18px 8px 0; }
  body.sailing .hero h1 { font-size:0; }
  body.sailing .hero h1::after { content:""; }
  body.sailing .hero > p, body.sailing .starters, body.sailing .board { display:none; }

  /* the conversation */
  #chatlog { padding-top:14px; }
  .msg { display:flex; gap:10px; margin:14px 0; animation:rise .28s ease both; }
  .msg.you { justify-content:flex-end; }
  .avatar { flex:0 0 30px; width:30px; height:30px; border-radius:50%;
    display:flex; align-items:center; justify-content:center; font-size:14px;
    background:radial-gradient(circle at 32% 28%, #1b3a56, #0e2237);
    border:1px solid var(--edge); color:var(--phosphor); align-self:flex-end; }
  .bubble { max-width:82%; padding:11px 15px; border-radius:18px;
    font-size:15.5px; white-space:pre-wrap; word-break:break-word; }
  .msg.you .bubble { background:linear-gradient(160deg, #223257, #1b2947);
    border:1px solid #2c3d63; border-bottom-right-radius:6px; }
  .msg.fleet .bubble { background:var(--hull);
    border:1px solid var(--edge-soft); border-bottom-left-radius:6px; }
  .msg.error .bubble { background:rgba(242,125,138,.08);
    border:1px solid rgba(242,125,138,.35); color:var(--flare); }
  .stamp { display:flex; align-items:center; gap:6px; margin-top:9px;
    padding-top:8px; border-top:1px dashed var(--edge);
    font:11.5px/1 var(--mono); color:var(--faint); letter-spacing:.03em; }
  .stamp .coin { color:var(--brass); font-size:10px; }
  .stamp b { color:var(--mist); font-weight:500; }
  .typing { color:var(--mist); }
  .cursor { display:inline-block; width:7px; height:15px; margin-left:2px;
    background:var(--phosphor); vertical-align:-2px;
    animation:blink 1s steps(2) infinite; }
  .buoys { display:inline-flex; gap:5px; align-items:center; }
  .buoys i { width:6px; height:6px; border-radius:50%; background:var(--phosphor);
    animation:buoy 1.2s ease-in-out infinite; }
  .buoys i:nth-child(2) { animation-delay:.18s; }
  .buoys i:nth-child(3) { animation-delay:.36s; }

  /* the dock — composer */
  form { position:fixed; bottom:0; left:0; right:0; z-index:10;
    padding:10px 14px calc(12px + env(safe-area-inset-bottom));
    background:linear-gradient(transparent, var(--sea-deep) 34%); }
  .dock { display:flex; gap:8px; max-width:720px; margin:0 auto;
    background:rgba(16,28,49,.88); border:1px solid var(--edge);
    border-radius:999px; padding:6px 6px 6px 20px;
    backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
    transition:border-color .15s ease; }
  .dock:focus-within { border-color:var(--phosphor); }
  input { flex:1; background:transparent; color:var(--foam); border:0;
    font:16px var(--sans); outline:none; min-width:0; }
  input::placeholder { color:var(--faint); }
  #askbtn { flex:0 0 42px; width:42px; height:42px; border-radius:50%;
    border:0; cursor:pointer; display:flex; align-items:center; justify-content:center;
    background:linear-gradient(160deg, var(--brass), var(--brass-deep));
    color:#221604; transition:transform .12s ease, opacity .2s; }
  #askbtn:hover { transform:scale(1.06); }
  #askbtn:active { transform:scale(.94); }
  #askbtn:disabled { opacity:.45; transform:none; cursor:default; }
  .note { max-width:720px; margin:8px auto 0; font-size:11.5px; color:var(--faint);
    text-align:center; font-family:var(--mono); letter-spacing:.02em; }
  .note b { color:var(--mist); font-weight:500; }

  :focus-visible { outline:2px solid var(--phosphor); outline-offset:2px;
    border-radius:6px; }
  @keyframes rise { from { opacity:0; transform:translateY(8px); } }
  @keyframes blink { 50% { opacity:0; } }
  @keyframes buoy { 0%,100% { opacity:.25; transform:translateY(0); }
                    50% { opacity:1; transform:translateY(-3px); } }
  @keyframes beacon { 0%,100% { opacity:.4; } 50% { opacity:1; } }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation:none !important; transition:none !important; }
  }
  @media (max-width:520px) {
    .brand .port { display:none; }
    .bubble { max-width:88%; }
    header { gap:8px; }
    .chips { gap:6px; }
    .pill { padding:4px 10px; font-size:12px; }
    .pill span[data-i18n="credits"] { display:none; }
    #fleetpill { max-width:34vw; overflow:hidden; text-overflow:ellipsis; }
  }
</style>
</head>
<body>
<div class="sky" aria-hidden="true"></div>
<header>
  <div class="brand">
    <span class="anchor" aria-hidden="true">⚓</span>
    <span class="word">Kemi</span>
    <span class="port" data-i18n="port">Limanı</span>
  </div>
  <div class="chips">
    <span class="pill" id="fleetpill" role="status">…</span>
    <span class="pill"><b id="balance">…</b> <span data-i18n="credits">kredi</span></span>
    <button type="button" class="pill" id="langbtn" aria-label="language">EN</button>
  </div>
</header>
<main>
  <section class="hero" id="hero">
    <h1 data-i18n="herohead">Merkezi olmayan <em>yapay zekâ</em></h1>
    <p data-i18n="herobody">Sorunu bir şirketin sunucusu değil, filodaki başka
    insanların gemileri cevaplar. Her cevabın altında hangi geminin, hangi modelle,
    kaç krediye ürettiği yazılıdır — hoş geldin kredin hazır.</p>
    <div class="board" id="board" aria-label="fleet">
      <div class="cell"><div class="n live" id="bships">–</div>
        <div class="l" data-i18n="bships">gemi çevrimiçi</div></div>
      <div class="cell"><div class="n" id="bmodels">–</div>
        <div class="l" data-i18n="bmodels">model</div></div>
      <div class="cell"><div class="n gold" id="bprice">–</div>
        <div class="l" data-i18n="bprice">kredi / soru</div></div>
    </div>
    <div class="starters" id="starters"></div>
  </section>
  <div id="chatlog" aria-live="polite"></div>
</main>
<form id="askform" autocomplete="off">
  <div class="dock">
    <input id="askmsg" data-i18n-ph="askph" placeholder="filoya bir şey sor…"
           maxlength="2000" aria-label="message">
    <button id="askbtn" type="submit" aria-label="send" data-i18n-title="send">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M4 12h14m0 0-6-6m6 6-6 6" stroke="currentColor"
              stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>
  </div>
  <p class="note" data-i18n="note">cevaplar filodan gelir — merkezî sunucu yok ·
    kendi gemin: <b>pip install kemi && kemi app</b></p>
</form>
<script>
const $ = s => document.querySelector(s);
const esc = t => String(t).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const I18N = {
  tr: { port:'Limanı', credits:'kredi',
        herohead:'Merkezi olmayan <em>yapay zekâ</em>',
        herobody:'Sorunu bir şirketin sunucusu değil, filodaki başka insanların gemileri cevaplar. Her cevabın altında hangi geminin, hangi modelle, kaç krediye ürettiği yazılıdır — hoş geldin kredin hazır.',
        bships:'gemi çevrimiçi', bmodels:'model', bprice:'kredi / soru',
        askph:'filoya bir şey sor…', send:'gönder',
        note:'cevaplar filodan gelir — merkezî sunucu yok · kendi gemin: <b>pip install kemi && kemi app</b>',
        ships:n=>`${n} gemi çevrimiçi`, noships:'filo aranıyor…',
        via:(c,s,m)=>`${c} kr — ${s}${m ? ' — ' + m : ''}`,
        thinking:'filo düşünüyor', starters:[
          'Kemi nasıl çalışıyor, kim cevap veriyor?',
          'Bana kısa bir deniz hikâyesi anlat',
          'Kredi sistemi neden adil?' ] },
  en: { port:'Harbor', credits:'credits',
        herohead:'Decentralised <em>intelligence</em>',
        herobody:'Your question is answered by other people’s ships in the fleet, not a company server. Every answer is stamped with the ship, the model and the credits it cost — your welcome credits are ready.',
        bships:'ships online', bmodels:'models', bprice:'credits / ask',
        askph:'ask the fleet anything…', send:'send',
        note:'answers come from the fleet — no central server · your own ship: <b>pip install kemi && kemi app</b>',
        ships:n=>`${n} ships online`, noships:'searching the fleet…',
        via:(c,s,m)=>`${c} cr — ${s}${m ? ' — ' + m : ''}`,
        thinking:'the fleet is thinking', starters:[
          'How does Kemi work — who answers me?',
          'Tell me a short sea story',
          'Why is the credit system fair?' ] },
};
let LANG = localStorage.getItem('kemi-harbor-lang') || 'tr';
function applyLang() {
  const d = I18N[LANG];
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const v = d[el.dataset.i18n];
    if (typeof v === 'string') el.innerHTML = v;
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    const v = d[el.dataset.i18nPh];
    if (typeof v === 'string') el.placeholder = v;
  });
  document.documentElement.lang = LANG;
  $('#langbtn').textContent = LANG === 'tr' ? 'EN' : 'TR';
  $('#starters').innerHTML = d.starters.map(q =>
    `<button type="button" class="starter">${esc(q)}</button>`).join('');
  localStorage.setItem('kemi-harbor-lang', LANG);
}
$('#langbtn').onclick = () => { LANG = LANG === 'tr' ? 'en' : 'tr'; applyLang(); render(); };
document.addEventListener('click', ev => {
  const b = ev.target.closest('.starter');
  if (!b) return;
  $('#askmsg').value = b.textContent;
  $('#askform').requestSubmit();
});

let TOKEN = localStorage.getItem('kemi-harbor-token') || '';
let STATE = null;
async function hello() {
  const r = await fetch('/api/hello', { method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ token: TOKEN }) });
  const j = await r.json();
  TOKEN = j.token;
  localStorage.setItem('kemi-harbor-token', TOKEN);
  $('#balance').textContent = j.balance.toFixed(2);
  fleetBoard(j.fleet);
}
function fleetBoard(f) {
  const d = I18N[LANG];
  $('#fleetpill').textContent = f && f.ships ? d.ships(f.ships) : d.noships;
  $('#bships').textContent = f ? f.ships : '–';
  $('#bmodels').textContent = f && f.models && f.models.length
    ? f.models.length : '–';
  $('#bmodels').title = f && f.models ? f.models.join(', ') : '';
  $('#bprice').textContent = f && f.cheapest != null ? f.cheapest.toFixed(2) : '–';
}
function render() {
  if (!STATE) return;
  const d = I18N[LANG];
  $('#balance').textContent = STATE.balance.toFixed(2);
  fleetBoard(STATE.fleet);
  document.body.classList.toggle('sailing',
    STATE.log.length > 0 || STATE.busy);
  const av = `<div class="avatar" aria-hidden="true">⚓</div>`;
  const rows = STATE.log.map(m => {
    if (m.role === 'you')
      return `<div class="msg you"><div class="bubble">${esc(m.text)}</div></div>`;
    if (m.role === 'error')
      return `<div class="msg error">${av}<div class="bubble">${esc(m.text)}</div></div>`;
    const stamp = d.via((m.cost ?? 0).toFixed(2), esc(m.ship || ''),
                        m.model && m.model !== 'mock' ? esc(m.model) : '');
    return `<div class="msg fleet">${av}<div class="bubble">${esc(m.text)}` +
           `<div class="stamp"><span class="coin">◈</span><b>${stamp}</b></div></div></div>`;
  });
  if (STATE.busy) rows.push(`<div class="msg fleet">${av}<div class="bubble typing">` +
    (STATE.live ? esc(STATE.live) + '<span class="cursor"></span>'
                : d.thinking + ' <span class="buoys"><i></i><i></i><i></i></span>') +
    `</div></div>`);
  const el = $('#chatlog');
  const stick = Math.abs(window.scrollY + innerHeight - document.body.scrollHeight) < 140;
  el.innerHTML = rows.join('');
  $('#askbtn').disabled = STATE.busy;
  if (stick && (STATE.busy || rows.length !== window._n)) {
    window._n = rows.length;
    window.scrollTo(0, document.body.scrollHeight);
  }
}
async function poll() {
  if (!TOKEN) return;
  try {
    const r = await fetch('/api/chat?t=' + encodeURIComponent(TOKEN));
    if (r.status === 401) { TOKEN=''; localStorage.removeItem('kemi-harbor-token');
                            await hello(); return; }
    STATE = await r.json();
    render();
  } catch (e) {}
}
$('#askform').addEventListener('submit', async ev => {
  ev.preventDefault();
  const text = $('#askmsg').value.trim();
  if (!text) return;
  $('#askmsg').value = '';
  try {
    const r = await fetch('/api/ask', { method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ token: TOKEN, message: text }) });
    const j = await r.json();
    if (!j.ok && j.error) alert(j.error);
  } catch (e) {}
  poll();
});
applyLang();
hello().then(poll);
setInterval(poll, 700);
</script>
</body>
</html>
"""
