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
import json
import logging
import secrets
import time
from typing import Any

from . import __version__
from .consumer import Consumer, Job, JobError
from .node import PeerNode
from .tasks import TASKS

log = logging.getLogger("kemi.webui")

MAX_BODY = 5 * 1024 * 1024
PROVIDER_REFRESH = 5.0
MAX_JOBS_KEPT = 50


class WebUI:
    def __init__(self, node: PeerNode, host: str = "127.0.0.1", port: int = 8080):
        self.node = node
        self.consumer = Consumer(node)
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._providers_cache: dict[str, list[dict[str, Any]]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._loops: list[asyncio.Task] = []

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

    # -- background state ------------------------------------------------------

    async def _providers_loop(self) -> None:
        while True:
            for task_name in list(TASKS):
                try:
                    self._providers_cache[task_name] = \
                        await self.consumer.list_providers(task_name)
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
                "price": p["price"],
                "host": p.get("host", ""),
                "port": p.get("port", 0),
                "relay": bool(p.get("relay")),
                "e2e": bool(p.get("e2e")),
                "rep": round(node.reputation.score(p["node_id"]), 3),
                "cpu": (p.get("resources") or {}).get("cpu_count"),
                "gpus": len((p.get("resources") or {}).get("gpus") or []),
                "tasks": sorted(p["tasks"]),
            }
            for p in merged.values()
        ]
        provider_rows.sort(key=lambda p: (-p["rep"], p["price"]))
        jobs = sorted(self._jobs.values(), key=lambda j: -j["started"])[:MAX_JOBS_KEPT]
        return {
            "version": __version__,
            "now": time.time(),
            "node": {
                "id": node.identity.node_id,
                "short": node.identity.short_id,
                "port": node.port,
                "provide": node.provide,
                "price": node.price,
                "relayed": node._relay_endpoint is not None,
                "tasks": node.supported_tasks,
            },
            "balance": node.ledger.balance(node.identity.node_id),
            "ledger": {
                "txs": node.ledger.tx_count(),
                "recent": node.ledger.recent_txs(15),
                "flagged": node.ledger.double_spenders(),
            },
            "dht_contacts": len(node.dht.table),
            "known_tasks": sorted(TASKS),
            "providers": provider_rows,
            "reputation": node.reputation.snapshot(),
            "jobs": jobs,
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
            report = await self.consumer.run_job(job)
            entry.update(
                status="done",
                spent=report.spent,
                results=report.results[:200],
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
        elif method == "GET" and path == "/api/state":
            await self._respond(writer, 200, self._state())
        elif method == "POST" and path == "/api/job":
            try:
                spec = json.loads(body.decode("utf-8"))
                entry = self._submit_job(spec)
                await self._respond(writer, 200, {"ok": True, "job": entry["id"]})
            except (ValueError, json.JSONDecodeError, TypeError) as exc:
                await self._respond(writer, 400, {"ok": False, "error": str(exc)})
        else:
            await self._respond(writer, 404, {"error": "not found"})

    async def _respond(self, writer: asyncio.StreamWriter, status: int,
                       payload: Any, content_type: str = "application/json") -> None:
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
            f"Cache-Control: no-store\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + data)
        await writer.drain()


_PAGE = """<!doctype html>
<html lang="tr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kemi — sürü paneli</title>
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
  #jobmsg { min-height:18px; font-size:12px; }
  footer { padding:8px 22px 20px; color:var(--dim); font-size:12px; }
</style></head><body>
<header>
  <h1>kemi <span class="dim">sürü paneli</span></h1>
  <span id="nodeinfo" class="dim"></span>
  <span class="balance"><span id="balance">…</span> <span class="dim">kredi</span></span>
</header>
<main>
  <section>
    <h2>Sağlayıcılar (canlı)</h2>
    <table><thead><tr>
      <th>düğüm</th><th>fiyat</th><th>itibar</th><th>cpu/gpu</th><th>yol</th><th>görevler</th>
    </tr></thead><tbody id="providers"></tbody></table>
  </section>
  <section>
    <h2>İş gönder</h2>
    <form id="jobform">
      <div class="row">
        <div><label>görev</label><select id="task"></select></div>
        <div><label>parça boyutu</label><input id="chunk" type="number" value="4" min="1"></div>
        <div><label>artıklık</label><input id="redundancy" type="number" value="1" min="1"></div>
      </div>
      <div><label>öğeler (JSON dizisi)</label>
        <textarea id="items">["merhaba", "dünya"]</textarea></div>
      <div><label>parametreler (JSON nesnesi)</label>
        <input id="params" value="{}"></div>
      <button type="submit">sürüye gönder</button>
      <div id="jobmsg"></div>
    </form>
    <table><thead><tr>
      <th>iş</th><th>görev</th><th>öğe</th><th>durum</th><th>maliyet</th><th>sonuç</th>
    </tr></thead><tbody id="jobs"></tbody></table>
  </section>
  <section>
    <h2>Defter (son transferler)</h2>
    <table><thead><tr>
      <th>kimden</th><th>kime</th><th>tutar</th><th>ne zaman</th>
    </tr></thead><tbody id="txs"></tbody></table>
    <div id="flagged"></div>
  </section>
  <section>
    <h2>İtibar (bu düğümün gözünden)</h2>
    <table><thead><tr>
      <th>düğüm</th><th>puan</th><th>iyi</th><th>kötü</th><th>olay</th>
    </tr></thead><tbody id="reputation"></tbody></table>
  </section>
</main>
<footer id="footer"></footer>
<script>
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v).replace(/[&<>"]/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const short = (id) => esc(String(id).slice(0, 12));
const ago = (ts, now) => { const d = Math.max(0, now - ts);
  return d < 60 ? Math.round(d) + ' sn' : Math.round(d / 60) + ' dk'; };

let knownTasks = [];

function render(s) {
  $('#balance').textContent = s.balance.toFixed(2);
  $('#nodeinfo').textContent = `düğüm ${s.node.short} · port ${s.node.port}` +
    (s.node.provide ? ` · sağlayıcı (${s.node.price} cr/öğe)` : ' · eş') +
    (s.node.relayed ? ' · relay üzerinden' : '');
  $('#footer').textContent = `kemi v${s.version} · defter: ${s.ledger.txs} işlem · ` +
    `DHT: ${s.dht_contacts} kontak · her 2 sn'de yenilenir`;

  $('#providers').innerHTML = s.providers.map(p => `<tr>
    <td title="${esc(p.id)}">${short(p.id)}</td>
    <td>${p.price.toFixed(2)}</td>
    <td class="${p.rep >= 0.5 ? 'ok' : 'bad'}">${p.rep.toFixed(2)}</td>
    <td>${p.cpu ?? '?'} / ${p.gpus}</td>
    <td>${p.relay ? '<span class="tag">relay</span>' : '<span class="tag">direkt</span>'}` +
      `${p.e2e ? '<span class="tag ok">e2e</span>' : ''}</td>
    <td>${p.tasks.map(t => `<span class="tag">${esc(t)}</span>`).join('')}</td>
  </tr>`).join('') || '<tr><td colspan="6" class="dim">sağlayıcı keşfedilmedi…</td></tr>';

  if (JSON.stringify(knownTasks) !== JSON.stringify(s.known_tasks)) {
    knownTasks = s.known_tasks;
    $('#task').innerHTML = knownTasks.map(t => `<option>${esc(t)}</option>`).join('');
  }

  $('#jobs').innerHTML = s.jobs.map(j => `<tr>
    <td>${esc(j.id)}</td><td>${esc(j.task)}</td><td>${j.items}</td>
    <td class="${j.status === 'done' ? 'ok' : j.status === 'failed' ? 'bad' : 'lnk'}">` +
      `${esc(j.status)}</td>
    <td>${j.spent != null ? j.spent.toFixed(2) : '—'}</td>
    <td class="dim" title="${j.error ? esc(j.error) : ''}">${
      j.status === 'failed' ? esc(String(j.error).slice(0, 60))
      : j.results ? esc(JSON.stringify(j.results).slice(0, 60)) + '…' : '…'}</td>
  </tr>`).join('') || '<tr><td colspan="6" class="dim">henüz iş yok</td></tr>';

  $('#txs').innerHTML = s.ledger.recent.map(t => `<tr>
    <td>${short(t.from)}</td><td>${short(t.to)}</td>
    <td>${t.amount.toFixed(2)}</td><td class="dim">${ago(t.ts, s.now)} önce</td>
  </tr>`).join('') || '<tr><td colspan="4" class="dim">henüz transfer yok</td></tr>';
  $('#flagged').innerHTML = s.ledger.flagged.length
    ? `<p class="bad">⚑ çift harcama kanıtı: ${s.ledger.flagged.map(short).join(', ')}</p>` : '';

  const reps = Object.entries(s.reputation);
  $('#reputation').innerHTML = reps.map(([id, r]) => `<tr>
    <td>${short(id)}</td>
    <td class="${r.score >= 0.5 ? 'ok' : 'bad'}">${r.score.toFixed(2)}</td>
    <td>${r.good}</td><td>${r.bad}</td><td>${r.events}</td>
  </tr>`).join('') || '<tr><td colspan="5" class="dim">henüz etkileşim yok</td></tr>';
}

async function refresh() {
  try { render(await (await fetch('/api/state')).json()); }
  catch (e) { $('#footer').textContent = 'düğüme ulaşılamıyor…'; }
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
    msg.textContent = r.ok ? `iş ${r.job} sürüye gönderildi` : `hata: ${r.error}`;
    refresh();
  } catch (e) { msg.className = 'bad'; msg.textContent = 'hata: ' + e.message; }
});

refresh();
setInterval(refresh, 2000);
</script>
</body></html>
"""
