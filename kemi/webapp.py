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
<title>Kemi Limanı — filoya sor</title>
<style>
  :root { --bg:#0b1020; --card:#141a30; --edge:#232c4e; --ink:#e8ecff;
          --dim:#8b93b8; --gold:#f5c96b; --teal:#4fd1c5; --bad:#ff7b8a; }
  * { box-sizing:border-box; margin:0; }
  body { background:radial-gradient(1200px 600px at 70% -10%, #1a2247 0%, var(--bg) 55%);
         color:var(--ink); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
         min-height:100dvh; display:flex; flex-direction:column; }
  header { display:flex; align-items:center; gap:10px; padding:14px 16px;
           border-bottom:1px solid var(--edge); position:sticky; top:0;
           background:rgba(11,16,32,.85); backdrop-filter:blur(8px); z-index:5; }
  header h1 { font-size:18px; letter-spacing:.5px; }
  header h1 b { color:var(--gold); }
  .chip { margin-left:auto; display:flex; gap:8px; align-items:center; }
  .pill { border:1px solid var(--edge); background:var(--card); border-radius:999px;
          padding:4px 12px; font-size:13px; color:var(--dim); white-space:nowrap; }
  .pill b { color:var(--teal); font-variant-numeric:tabular-nums; }
  #langbtn { cursor:pointer; color:var(--ink); }
  main { flex:1; width:100%; max-width:760px; margin:0 auto; padding:16px 14px 120px; }
  .hello { background:linear-gradient(160deg,#182042,#12172e); border:1px solid var(--edge);
           border-radius:16px; padding:18px; margin-bottom:16px; }
  .hello h2 { font-size:16px; margin-bottom:6px; color:var(--gold); }
  .hello p { color:var(--dim); font-size:14px; }
  .hello .fleetline { margin-top:10px; font-size:13px; color:var(--teal); }
  .msg { display:flex; margin:10px 0; }
  .msg .bubble { max-width:85%; padding:10px 14px; border-radius:16px; font-size:15px;
                 white-space:pre-wrap; word-break:break-word; }
  .msg.you { justify-content:flex-end; }
  .msg.you .bubble { background:#2a3560; border-bottom-right-radius:4px; }
  .msg.fleet .bubble { background:var(--card); border:1px solid var(--edge);
                       border-bottom-left-radius:4px; }
  .msg.error .bubble { background:#3a1b26; border:1px solid #5c2635; color:var(--bad); }
  .stamp { display:block; margin-top:6px; font-size:12px; color:var(--dim); }
  .stamp b { color:var(--gold); font-weight:600; }
  .typing { color:var(--dim); }
  form { position:fixed; bottom:0; left:0; right:0; padding:12px 14px
         calc(12px + env(safe-area-inset-bottom)); background:rgba(11,16,32,.92);
         backdrop-filter:blur(8px); border-top:1px solid var(--edge); }
  .row { display:flex; gap:8px; max-width:760px; margin:0 auto; }
  input { flex:1; background:var(--card); color:var(--ink); border:1px solid var(--edge);
          border-radius:12px; padding:12px 14px; font-size:16px; outline:none; }
  input:focus { border-color:var(--teal); }
  button { background:var(--gold); color:#1a1405; border:0; border-radius:12px;
           padding:0 18px; font-size:15px; font-weight:700; cursor:pointer; }
  button:disabled { opacity:.5; }
  .note { max-width:760px; margin:6px auto 0; font-size:12px; color:var(--dim);
          text-align:center; }
  a { color:var(--teal); }
</style>
</head>
<body>
<header>
  <h1>⚓ <span data-i18n="title">Kemi <b>Limanı</b></span></h1>
  <div class="chip">
    <span class="pill" id="fleetpill">…</span>
    <span class="pill"><b id="balance">…</b> <span data-i18n="credits">kredi</span></span>
    <span class="pill" id="langbtn">EN</span>
  </div>
</header>
<main>
  <div class="hello" id="hello">
    <h2 data-i18n="hellohead">Hoş geldin kaptan!</h2>
    <p data-i18n="hellobody">Buradaki yapay zekânın merkezi yok: sorunu filodaki başka
    kullanıcıların gemileri cevaplar ve her cevabın altında hangi geminin, hangi modelle,
    kaç krediye ürettiği yazar. Hesabına hoş geldin kredisi tanımlandı — sor bakalım.</p>
    <div class="fleetline" id="fleetline"></div>
  </div>
  <div id="chatlog"></div>
</main>
<form id="askform" autocomplete="off">
  <div class="row">
    <input id="askmsg" data-i18n-ph="askph" placeholder="filoya bir şey sor…" maxlength="2000">
    <button id="askbtn" type="submit" data-i18n="send">gönder</button>
  </div>
  <div class="note" data-i18n="note">cevaplar filodaki gemilerden gelir — merkezî sunucu yok ·
  kendi gemini katmak için: <b>pip install kemi && kemi app</b></div>
</form>
<script>
const $ = s => document.querySelector(s);
const esc = t => String(t).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const I18N = {
  tr: { title:'Kemi <b>Limanı</b>', credits:'kredi', hellohead:'Hoş geldin kaptan!',
        hellobody:'Buradaki yapay zekânın merkezi yok: sorunu filodaki başka kullanıcıların gemileri cevaplar ve her cevabın altında hangi geminin, hangi modelle, kaç krediye ürettiği yazar. Hesabına hoş geldin kredisi tanımlandı — sor bakalım.',
        send:'gönder', askph:'filoya bir şey sor…',
        note:'cevaplar filodaki gemilerden gelir — merkezî sunucu yok · kendi gemini katmak için: pip install kemi && kemi app',
        ships:n=>`${n} gemi çevrimiçi`, noships:'filo aranıyor…',
        via:(c,s,m)=>`${c} kredi · ${s}${m?' · '+m:''}`, thinking:'filo düşünüyor…' },
  en: { title:'Kemi <b>Harbor</b>', credits:'credits', hellohead:'Welcome aboard, captain!',
        hellobody:'This AI has no center: your question is answered by other users\' ships in the fleet, and every answer is stamped with the ship, the model and the credits it cost. Welcome credits are on your account — ask away.',
        send:'send', askph:'ask the fleet anything…',
        note:'answers come from ships in the fleet — no central server · add your own ship: pip install kemi && kemi app',
        ships:n=>`${n} ships online`, noships:'searching the fleet…',
        via:(c,s,m)=>`${c} credits · ${s}${m?' · '+m:''}`, thinking:'the fleet is thinking…' },
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
  localStorage.setItem('kemi-harbor-lang', LANG);
}
$('#langbtn').onclick = () => { LANG = LANG === 'tr' ? 'en' : 'tr'; applyLang(); render(); };

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
  fleetPill(j.fleet);
}
function fleetPill(f) {
  const d = I18N[LANG];
  $('#fleetpill').textContent = f && f.ships ? d.ships(f.ships) : d.noships;
  $('#fleetline').textContent = f && f.models && f.models.length
    ? '🧠 ' + f.models.join(' · ') : '';
}
function render() {
  if (!STATE) return;
  const d = I18N[LANG];
  $('#balance').textContent = STATE.balance.toFixed(2);
  fleetPill(STATE.fleet);
  const rows = STATE.log.map(m => {
    if (m.role === 'you')
      return `<div class="msg you"><div class="bubble">${esc(m.text)}</div></div>`;
    if (m.role === 'error')
      return `<div class="msg error"><div class="bubble">⚠ ${esc(m.text)}</div></div>`;
    const stamp = d.via((m.cost ?? 0).toFixed(2), esc(m.ship || ''),
                        m.model && m.model !== 'mock' ? esc(m.model) : '');
    return `<div class="msg fleet"><div class="bubble">${esc(m.text)}` +
           `<span class="stamp">⚓ <b>${stamp}</b></span></div></div>`;
  });
  if (STATE.busy) rows.push(`<div class="msg fleet"><div class="bubble typing">` +
    (STATE.live ? esc(STATE.live) + '▋' : d.thinking) + `</div></div>`);
  const el = $('#chatlog');
  const stick = Math.abs(window.scrollY + innerHeight - document.body.scrollHeight) < 120;
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
