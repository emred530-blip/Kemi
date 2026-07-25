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
import hmac
import json
import logging
import os
import secrets
import time
from collections import deque
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
ASKS_KEPT = 400              # answered-question records kept for the admin panel


def _now() -> float:
    return time.time()


class WebApp:
    """A multi-visitor chat portal backed by one consumer node."""

    def __init__(self, node, host: str = "0.0.0.0", port: int = 8090,
                 faucet: float = 10.0, state_path: str | None = None,
                 admin_key: str | None = None):
        self.node = node
        self.consumer = Consumer(node)
        self.host = host
        self.port = port
        self.faucet = float(faucet)
        self.state_path = state_path
        self.admin_key = admin_key or secrets.token_urlsafe(12)
        self._guests: dict[str, dict[str, Any]] = {}
        self._asks: deque[dict[str, Any]] = deque(maxlen=ASKS_KEPT)
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
                    "banned": bool(g.get("banned", False)),
                    "log": list(g.get("log", []))[-MAX_LOG:],
                    "history": [tuple(x) for x in g.get("history", [])][-8:],
                    "live": "", "busy": False, "last_ask": 0.0,
                }
            if isinstance(saved.get("faucet"), (int, float)):
                self.faucet = float(saved["faucet"])
            self._asks.extend(list(saved.get("asks", []))[-ASKS_KEPT:])
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
                               "spent", "asks", "banned", "log", "history")}
        tmp = f"{self.state_path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"guests": durable, "faucet": self.faucet,
                           "asks": list(self._asks)}, fh, ensure_ascii=False)
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
            "spent": 0.0, "asks": 0, "banned": False,
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
        if guest.get("banned"):
            raise PermissionError("this guest has been banned by the harbor keeper")
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

        started = _now()
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
            self._asks.append({"ts": round(_now(), 1), "cost": spent,
                               "ship": ship_name(provider), "model": served_model,
                               "guest": guest["name"], "ok": True,
                               "dur": round(_now() - started, 2)})
        except JobError as exc:
            guest["log"].append({"role": "error", "text": str(exc), "ts": _now()})
            self._asks.append({"ts": round(_now(), 1), "cost": 0.0, "ship": "",
                               "model": "", "guest": guest["name"], "ok": False,
                               "dur": round(_now() - started, 2)})
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

    # -- the harbor keeper's panel -------------------------------------------

    def _check_admin(self, key: str) -> None:
        if not hmac.compare_digest(str(key or ""), self.admin_key):
            raise PermissionError("invalid admin key")

    def admin_state(self) -> dict[str, Any]:
        """Everything the keeper's panel shows, in scan order: alerts and
        headline tiles first, then economy, guests and fleet quality."""
        node = self.node
        now = _now()
        balance = node.ledger.balance(node.identity.node_id)
        asks = list(self._asks)
        day = [a for a in asks if now - a["ts"] < 24 * 3600]
        day_ok = [a for a in day if a["ok"]]
        spent_day = round(sum(a["cost"] for a in day_ok), 4)
        avg_cost = round(spent_day / len(day_ok), 4) if day_ok else None
        runway = int(balance / avg_cost) if avg_cost else None
        active_guests = sum(1 for g in self._guests.values()
                            if now - g["seen"] < 24 * 3600)
        flagged = node.ledger.double_spenders()

        alerts: list[dict[str, str]] = []
        if not self._providers_cache:
            alerts.append({"level": "critical",
                           "text": "Filoda sağlayıcı görünmüyor — sorular cevapsız kalır."})
        if runway is not None and runway < 25:
            alerts.append({"level": "warning",
                           "text": f"Bakiye yaklaşık {runway} soruluk — kredi kazanmak için "
                                   "--provide açın ya da faucet'i kısın."})
        if flagged:
            alerts.append({"level": "serious",
                           "text": f"{len(flagged)} hesap çifte harcamadan işaretli."})
        if day and (sum(1 for a in day if not a["ok"]) / len(day)) > 0.2:
            alerts.append({"level": "warning",
                           "text": "Son 24 saatte soruların %20'sinden fazlası cevapsız."})

        # hourly spend, oldest→newest, 24 buckets (the economy chart's series)
        buckets = [{"h": h, "spend": 0.0, "asks": 0} for h in range(24)]
        for a in day_ok:
            b = buckets[23 - min(23, int((now - a["ts"]) // 3600))]
            b["spend"] = round(b["spend"] + a["cost"], 4)
            b["asks"] += 1

        rep = node.reputation.snapshot()
        fleet = []
        for p in self._providers_cache:
            r = rep.get(p["node_id"], {})
            fleet.append({
                "ship": ship_name(p["node_id"]),
                "model": p.get("model") or "—",
                "price": p["price"],
                "score": r.get("score"),
                "good": int(r.get("good", 0)),
                "bad": int(r.get("bad", 0)),
                "stream": bool(p.get("stream")),
            })
        fleet.sort(key=lambda f: (-(f["score"] or 0), f["price"]))

        guests = [{
            "token": g["token"], "name": g["name"],
            "balance": round(g["balance"], 2), "spent": round(g["spent"], 2),
            "asks": g["asks"], "banned": g["banned"],
            "seen": round(now - g["seen"]),
        } for g in sorted(self._guests.values(), key=lambda g: -g["seen"])[:200]]

        return {
            "ship": ship_name(node.identity.node_id),
            "alerts": alerts,
            "tiles": {
                "balance": round(balance, 2),
                "spent24": spent_day,
                "asks24": len(day),
                "failed24": sum(1 for a in day if not a["ok"]),
                "guests24": active_guests,
                "guests_total": len(self._guests),
                "runway": runway,
                "avg_cost": avg_cost,
                "faucet": self.faucet,
                "providers": len(self._providers_cache),
                "dht": len(node.dht.table),
            },
            "series": buckets,
            "guests": guests,
            "fleet": fleet,
            "flagged": [ship_name(n) for n in flagged],
        }

    def admin_guest_action(self, token: str, action: str,
                           amount: float = 0.0) -> dict[str, Any]:
        guest = self._guests.get(token or "")
        if guest is None:
            raise ValueError("no such guest")
        if action == "gift":
            amount = float(amount)
            if not 0 < amount <= 1000:
                raise ValueError("gift must be between 0 and 1000 credits")
            guest["balance"] = round(guest["balance"] + amount, 6)
        elif action == "ban":
            guest["banned"] = True
        elif action == "unban":
            guest["banned"] = False
        else:
            raise ValueError(f"unknown action: {action!r}")
        self._save()
        return {"ok": True, "balance": round(guest["balance"], 2),
                "banned": guest["banned"]}

    def admin_set_faucet(self, amount: float) -> dict[str, Any]:
        amount = float(amount)
        if not 0 <= amount <= 1000:
            raise ValueError("faucet must be between 0 and 1000 credits")
        self.faucet = amount
        self._save()
        return {"ok": True, "faucet": self.faucet}

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
            elif method == "GET" and path == "/admin":
                await self._respond(writer, 200, _ADMIN,
                                    content_type="text/html; charset=utf-8")
            elif method == "GET" and path == "/admin/api/state":
                self._check_admin(params.get("key", ""))
                await self._respond(writer, 200, self.admin_state())
            elif method == "POST" and path == "/admin/api/guest":
                spec = json.loads(body.decode("utf-8"))
                self._check_admin(str(spec.get("key") or ""))
                await self._respond(writer, 200, self.admin_guest_action(
                    str(spec.get("token") or ""), str(spec.get("action") or ""),
                    spec.get("amount") or 0.0))
            elif method == "POST" and path == "/admin/api/faucet":
                spec = json.loads(body.decode("utf-8"))
                self._check_admin(str(spec.get("key") or ""))
                await self._respond(writer, 200,
                                    self.admin_set_faucet(spec.get("amount")))
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


_ADMIN = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#060b16">
<title>Kemi Limanı — kaptan köşkü</title>
<style>
  :root {
    --sea-deep:#060b16; --sea:#0b1424; --hull:#101c31; --hull-2:#0e1930;
    --edge:#1d2c47; --edge-soft:#16233c;
    --foam:#e9eef7; --mist:#97a3be; --faint:#5f6d8c;
    --brass:#e7b75f; --brass-deep:#c9954a; --phosphor:#57d9c0;
    --good:#57d9c0; --warn:#f0a64a; --crit:#f27d8a;
    --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; margin:0; }
  body { background:radial-gradient(80rem 36rem at 50% -14rem, #162543 0%, transparent 60%),
         var(--sea-deep); color:var(--foam); font:14px/1.5 var(--sans);
         min-height:100dvh; }
  header { display:flex; align-items:baseline; gap:12px; padding:14px 22px;
    border-bottom:1px solid var(--edge-soft); position:sticky; top:0; z-index:5;
    background:color-mix(in srgb, var(--sea-deep) 84%, transparent);
    backdrop-filter:blur(10px); }
  header .word { font:600 21px/1 var(--serif); }
  header .sub { font:11px var(--mono); color:var(--faint);
    letter-spacing:.2em; text-transform:uppercase; }
  header .ship { margin-left:auto; font:12px var(--mono); color:var(--mist); }
  main { max-width:1180px; margin:0 auto; padding:18px 22px 60px;
    display:flex; flex-direction:column; gap:16px; }
  .gate { max-width:420px; margin:12dvh auto; background:var(--hull);
    border:1px solid var(--edge); border-radius:16px; padding:26px; }
  .gate h1 { font:600 20px var(--serif); margin-bottom:8px; }
  .gate p { color:var(--mist); font-size:13px; margin-bottom:14px; }
  .gate .row { display:flex; gap:8px; }
  .gate input { flex:1; }
  section { background:linear-gradient(170deg, var(--hull), var(--hull-2));
    border:1px solid var(--edge-soft); border-radius:14px; padding:16px 18px;
    overflow-x:auto; }
  h2 { font-size:11px; text-transform:uppercase; letter-spacing:.16em;
    color:var(--faint); margin-bottom:12px; }
  .alerts { display:flex; flex-direction:column; gap:8px; }
  .alert { display:flex; gap:10px; align-items:center; border-radius:10px;
    padding:10px 14px; font-size:13.5px; border:1px solid; }
  .alert.warning { color:var(--warn); border-color:rgba(240,166,74,.35);
    background:rgba(240,166,74,.07); }
  .alert.serious, .alert.critical { color:var(--crit);
    border-color:rgba(242,125,138,.35); background:rgba(242,125,138,.07); }
  .alert.okay { color:var(--good); border-color:rgba(87,217,192,.3);
    background:rgba(87,217,192,.06); }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:10px; }
  .tile { border:1px solid var(--edge-soft); background:rgba(14,25,48,.5);
    border-radius:12px; padding:12px 15px; }
  .tile .n { font:600 22px/1.2 var(--mono); font-variant-numeric:tabular-nums; }
  .tile .n small { font-size:12px; color:var(--faint); font-weight:400; }
  .tile .l { font-size:10.5px; color:var(--faint); letter-spacing:.12em;
    text-transform:uppercase; margin-top:4px; }
  .tile .n.gold { color:var(--brass); } .tile .n.live { color:var(--phosphor); }
  .tile .n.bad { color:var(--crit); }
  .chartwrap { position:relative; }
  #chart { width:100%; height:190px; display:block; }
  #tip { position:absolute; pointer-events:none; display:none;
    background:var(--sea-deep); border:1px solid var(--edge); border-radius:8px;
    padding:6px 10px; font:11.5px var(--mono); color:var(--foam);
    white-space:nowrap; z-index:3; }
  table { width:100%; border-collapse:collapse; font-size:13px;
    font-variant-numeric:tabular-nums; }
  th { text-align:left; color:var(--faint); font-weight:normal; font-size:10.5px;
    text-transform:uppercase; letter-spacing:.1em;
    border-bottom:1px solid var(--edge); padding:3px 10px 6px 0; }
  td { padding:6px 10px 6px 0; border-bottom:1px solid var(--edge-soft); }
  tr:last-child td { border-bottom:none; }
  td.num { font-family:var(--mono); }
  .pill { display:inline-flex; align-items:center; gap:5px;
    border:1px solid var(--edge); border-radius:999px; padding:1px 9px;
    font-size:11px; color:var(--mist); white-space:nowrap; }
  .pill.good { color:var(--good); border-color:rgba(87,217,192,.4); }
  .pill.bad { color:var(--crit); border-color:rgba(242,125,138,.4); }
  .pill.warn { color:var(--warn); border-color:rgba(240,166,74,.4); }
  button { background:linear-gradient(160deg, var(--brass), var(--brass-deep));
    color:#221604; border:0; border-radius:9px; padding:7px 12px;
    font:600 13px var(--sans); cursor:pointer; }
  button:hover { filter:brightness(1.08); }
  button.ghost { background:transparent; border:1px solid var(--edge);
    color:var(--mist); font-weight:400; padding:3px 10px; font-size:12px; }
  button.ghost:hover { border-color:var(--crit); color:var(--crit); }
  button.ghost.ok:hover { border-color:var(--good); color:var(--good); }
  input { background:rgba(10,17,32,.7); color:var(--foam);
    border:1px solid var(--edge); border-radius:9px; padding:8px 10px;
    font:14px var(--mono); }
  input:focus { outline:none; border-color:var(--phosphor); }
  .faucetrow { display:flex; gap:8px; align-items:center; margin-top:10px;
    color:var(--mist); font-size:13px; }
  .faucetrow input { width:90px; }
  :focus-visible { outline:2px solid var(--phosphor); outline-offset:2px; }
  .muted { color:var(--faint); }
  @media (max-width:640px) { main { padding:12px 12px 40px; } th,td { font-size:12px; } }
</style>
</head>
<body>
<header>
  <span class="word">Kemi</span>
  <span class="sub">kaptan köşkü</span>
  <span class="ship" id="shipname"></span>
</header>
<main id="app">
  <div class="gate" id="gate">
    <h1>⚓ Kaptan köşkü</h1>
    <p>Bu panel limanın sahibine aittir. <code>kemi web</code> başlarken
       terminalde yazan yönetici anahtarını girin.</p>
    <div class="row">
      <input id="keyinput" placeholder="yönetici anahtarı" aria-label="admin key">
      <button id="keybtn">giriş</button>
    </div>
  </div>
</main>
<script>
const $ = s => document.querySelector(s);
const esc = t => String(t).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let KEY = localStorage.getItem('kemi-admin-key') || '';
const ICONS = { warning:'▲', serious:'✖', critical:'✖', okay:'●' };
const ago = s => s < 90 ? `${s} sn` : s < 5400 ? `${Math.round(s/60)} dk`
                : s < 129600 ? `${Math.round(s/3600)} sa` : `${Math.round(s/86400)} g`;

function gate() {
  $('#app').innerHTML = document.getElementById('gate')
    ? $('#app').innerHTML : '';
}
$('#keybtn') && ($('#keybtn').onclick = () => {
  KEY = $('#keyinput').value.trim();
  localStorage.setItem('kemi-admin-key', KEY);
  refresh();
});

function tiles(t) {
  const cell = (n, l, cls='', extra='') =>
    `<div class="tile"><div class="n ${cls}">${n}${extra}</div><div class="l">${l}</div></div>`;
  return `<section><h2>komuta özeti</h2><div class="tiles">` +
    cell(t.balance.toFixed(2), 'liman bakiyesi (kredi)', 'gold') +
    cell(t.spent24.toFixed(2), 'filoya ödenen · 24 sa', 'gold') +
    cell(t.asks24 + (t.failed24 ? ` <small>(${t.failed24} cevapsız)</small>` : ''),
         'soru · 24 sa', t.failed24 > 0 ? 'bad' : '') +
    cell(t.guests24 + ` <small>/ ${t.guests_total}</small>`, 'aktif ziyaretçi · 24 sa', 'live') +
    cell(t.runway != null ? '≈' + t.runway : '—', 'kalan soru (tahmin)',
         t.runway != null && t.runway < 25 ? 'bad' : '') +
    cell(t.providers, 'gemi çevrimiçi', t.providers ? 'live' : 'bad') +
    `</div><div class="faucetrow">hoş geldin kredisi (faucet):
      <input id="faucet" type="number" min="0" max="1000" step="0.5" value="${t.faucet}">
      <button id="faucetbtn">kaydet</button>
      <span class="muted">yeni ziyaretçilere verilir · DHT: ${t.dht} bağlantı</span>
    </div></section>`;
}

function alerts(list) {
  if (!list.length)
    list = [{level:'okay', text:'Her şey yolunda — liman sakin, filo cevap veriyor.'}];
  return `<section><h2>durum</h2><div class="alerts">` + list.map(a =>
    `<div class="alert ${a.level}"><span>${ICONS[a.level] || '●'}</span>` +
    `<span>${esc(a.text)}</span></div>`).join('') + `</div></section>`;
}

function chart(series) {
  const W = 1100, H = 190, P = {l:44, r:10, t:14, b:22};
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const max = Math.max(0.001, ...series.map(b => b.spend));
  const bw = iw / 24;
  const y = v => P.t + ih - (v / max) * ih;
  const grid = [0, .5, 1].map(f => {
    const v = max * f, yy = y(v);
    return `<line x1="${P.l}" x2="${W-P.r}" y1="${yy}" y2="${yy}"
              stroke="#1d2c47" stroke-width="1"/>
            <text x="${P.l-8}" y="${yy+4}" text-anchor="end"
              font-size="10" fill="#5f6d8c" font-family="ui-monospace,Menlo">${v.toFixed(1)}</text>`;
  }).join('');
  const bars = series.map((b, i) => {
    const x = P.l + i * bw + 2, w = Math.max(2, bw - 4);
    const h = Math.max(b.spend > 0 ? 3 : 0, (b.spend / max) * ih);
    return `<rect class="bar" data-i="${i}" x="${x}" y="${y(0)-h}" width="${w}" height="${h}"
              rx="3" fill="#e7b75f"/>
            <rect class="hit" data-i="${i}" x="${P.l + i*bw}" y="${P.t}" width="${bw}"
              height="${ih}" fill="transparent"/>`;
  }).join('');
  const now = new Date();
  const labels = [23, 17, 11, 5, 0].map(back => {
    const d = new Date(now.getTime() - back * 3600e3);
    const i = 23 - back;
    return `<text x="${P.l + i*bw + bw/2}" y="${H-6}" text-anchor="middle"
              font-size="10" fill="#5f6d8c" font-family="ui-monospace,Menlo"
              >${String(d.getHours()).padStart(2,'0')}:00</text>`;
  }).join('');
  return `<section><h2>ekonomi — saatlik filo ödemesi (kredi, son 24 sa)</h2>
    <div class="chartwrap">
      <svg id="chart" viewBox="0 0 ${W} ${H}" role="img"
        aria-label="saatlik harcama grafiği">${grid}${bars}${labels}</svg>
      <div id="tip"></div>
    </div></section>`;
}

function guests(rows) {
  const body = rows.length ? rows.map(g => `<tr>
    <td>⚓ ${esc(g.name)} ${g.banned ? '<span class="pill bad">✖ engelli</span>' : ''}</td>
    <td class="num">${g.balance.toFixed(2)}</td>
    <td class="num">${g.spent.toFixed(2)}</td>
    <td class="num">${g.asks}</td>
    <td class="muted">${ago(g.seen)} önce</td>
    <td style="white-space:nowrap">
      <button class="ghost ok" data-act="gift" data-t="${esc(g.token)}">+5 kredi</button>
      <button class="ghost" data-act="${g.banned ? 'unban' : 'ban'}"
        data-t="${esc(g.token)}">${g.banned ? 'engeli kaldır' : 'engelle'}</button>
    </td></tr>`).join('')
    : `<tr><td colspan="6" class="muted">henüz ziyaretçi yok — liman adresini paylaşın</td></tr>`;
  return `<section><h2>ziyaretçiler</h2><table>
    <thead><tr><th>misafir</th><th>bakiye</th><th>harcadı</th><th>soru</th>
    <th>son görülme</th><th>eylem</th></tr></thead>
    <tbody>${body}</tbody></table></section>`;
}

function fleet(rows, flagged) {
  const body = rows.length ? rows.map(f => `<tr>
    <td>⚓ ${esc(f.ship)}</td>
    <td>${f.model !== 'mock' && f.model !== '—'
          ? `<span class="pill good">● ${esc(f.model)}</span>`
          : `<span class="pill">○ ${esc(f.model)}</span>`}</td>
    <td class="num">${f.price.toFixed(2)}</td>
    <td class="num">${f.score != null ? f.score.toFixed(2) : '—'}</td>
    <td class="num">${f.good} <span class="muted">/</span> ${f.bad}</td>
    <td>${f.stream ? '<span class="pill good">● akış</span>'
                   : '<span class="pill warn">▲ akış yok</span>'}</td></tr>`).join('')
    : `<tr><td colspan="6" class="muted">filo boş görünüyor</td></tr>`;
  const flagRow = flagged.length
    ? `<p style="margin-top:10px" class="muted">✖ işaretli hesaplar: ${flagged.map(esc).join(', ')}</p>` : '';
  return `<section><h2>filo kalitesi</h2><table>
    <thead><tr><th>gemi</th><th>model</th><th>kredi/soru</th><th>itibar</th>
    <th>iyi / kötü</th><th>yetenek</th></tr></thead>
    <tbody>${body}</tbody></table>${flagRow}</section>`;
}

let STATE = null;
function render() {
  const s = STATE;
  $('#shipname').textContent = '⚓ ' + s.ship;
  $('#app').innerHTML = alerts(s.alerts) + tiles(s.tiles) + chart(s.series) +
                        guests(s.guests) + fleet(s.fleet, s.flagged);
  $('#faucetbtn').onclick = async () => {
    await post('/admin/api/faucet', { amount: parseFloat($('#faucet').value) });
    refresh();
  };
  document.querySelectorAll('button.ghost').forEach(b => b.onclick = async () => {
    const body = { token: b.dataset.t, action: b.dataset.act };
    if (b.dataset.act === 'gift') body.amount = 5;
    await post('/admin/api/guest', body);
    refresh();
  });
  const tip = $('#tip'), svg = $('#chart');
  svg.addEventListener('mousemove', ev => {
    const t = ev.target.closest('[data-i]');
    if (!t) { tip.style.display = 'none'; return; }
    const b = STATE.series[+t.dataset.i];
    const back = 23 - (+t.dataset.i);
    const d = new Date(Date.now() - back * 3600e3);
    tip.textContent = `${String(d.getHours()).padStart(2,'0')}:00 — ` +
      `${b.spend.toFixed(2)} kr · ${b.asks} soru`;
    const r = svg.getBoundingClientRect();
    tip.style.display = 'block';
    tip.style.left = Math.max(0, Math.min(ev.clientX - r.left + 12, r.width - 190)) + 'px';
    tip.style.top = (ev.clientY - r.top - 34) + 'px';
  });
  svg.addEventListener('mouseleave', () => tip.style.display = 'none');
}

async function post(url, body) {
  body.key = KEY;
  const r = await fetch(url, { method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
  if (!r.ok) { const j = await r.json().catch(() => ({}));
               alert(j.error || 'hata'); }
  return r;
}

async function refresh() {
  if (!KEY) return;
  try {
    const r = await fetch('/admin/api/state?key=' + encodeURIComponent(KEY));
    if (r.status === 401) { localStorage.removeItem('kemi-admin-key'); return; }
    STATE = await r.json();
    render();
  } catch (e) {}
}
if (KEY) refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""
