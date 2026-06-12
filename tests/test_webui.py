"""Web dashboard tests: live state API and browser-submitted jobs."""

import asyncio
import hashlib
import json
import unittest
import urllib.error
import urllib.request

from kemi.identity import Identity
from kemi.node import PeerNode
from kemi.webui import WebUI

DIFF = 4


def _http(method: str, url: str, body: bytes | None = None):
    req = urllib.request.Request(url, data=body, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


class WebUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes: list[PeerNode] = []
        self.bootstrap = await self._spawn()
        self.peers = [("127.0.0.1", self.bootstrap.port)]
        self.provider = await self._spawn(provide=True, price=0.5)
        self.viewer = await self._spawn()
        self.ui = WebUI(self.viewer, host="127.0.0.1", port=0)
        await self.ui.start()
        self.base = f"http://127.0.0.1:{self.ui.port}"

    async def asyncTearDown(self):
        await self.ui.stop()
        for node in self.nodes:
            await node.stop()

    async def _spawn(self, **kwargs) -> PeerNode:
        node = PeerNode(
            Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
            bootstrap=getattr(self, "peers", None) or [],
            difficulty=DIFF, sandbox=False, **kwargs,
        )
        await node.start()
        self.nodes.append(node)
        return node

    async def test_serves_dashboard_page(self):
        status, ctype, body = await asyncio.to_thread(_http, "GET", self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"fleet panel", body)

    async def test_state_api(self):
        # wait for the provider cache to fill
        for _ in range(40):
            _, _, body = await asyncio.to_thread(_http, "GET", self.base + "/api/state")
            state = json.loads(body)
            if state["providers"]:
                break
            await asyncio.sleep(0.25)
        self.assertEqual(state["node"]["id"], self.viewer.identity.node_id)
        self.assertEqual(state["balance"], 100.0)
        provider_ids = [p["id"] for p in state["providers"]]
        self.assertIn(self.provider.identity.node_id, provider_ids)
        listed = state["providers"][provider_ids.index(self.provider.identity.node_id)]
        self.assertTrue(listed["e2e"])
        self.assertIn("hash.sha256", listed["tasks"])
        self.assertIn("hash.sha256", state["known_tasks"])

    async def test_job_submission_roundtrip(self):
        items = ["panel-a", "panel-b", "panel-c"]
        spec = json.dumps({"task": "hash.sha256", "items": items,
                           "chunk_size": 2}).encode()
        status, _, body = await asyncio.to_thread(
            _http, "POST", self.base + "/api/job", spec)
        self.assertEqual(status, 200)
        job_id = json.loads(body)["job"]

        job = None
        for _ in range(80):
            _, _, body = await asyncio.to_thread(_http, "GET", self.base + "/api/state")
            jobs = {j["id"]: j for j in json.loads(body)["jobs"]}
            job = jobs.get(job_id)
            if job and job["status"] != "running":
                break
            await asyncio.sleep(0.25)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(job["results"],
                         [hashlib.sha256(i.encode()).hexdigest() for i in items])
        self.assertGreater(job["spent"], 0)

    async def test_bad_job_is_rejected(self):
        for bad in (b"not-json", b'{"task":"evil.exec","items":["x"]}',
                    b'{"task":"hash.sha256","items":[]}'):
            try:
                status, _, body = await asyncio.to_thread(
                    _http, "POST", self.base + "/api/job", bad)
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
            self.assertEqual(status, 400)
            self.assertFalse(json.loads(body)["ok"])

    async def test_chat_roundtrip(self):
        spec = json.dumps({"message": "hello fleet"}).encode()
        status, _, body = await asyncio.to_thread(
            _http, "POST", self.base + "/api/chat", spec)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        reply = None
        for _ in range(80):
            _, _, body = await asyncio.to_thread(_http, "GET", self.base + "/api/state")
            chat = json.loads(body)["chat"]
            fleet_msgs = [m for m in chat["log"] if m["role"] == "fleet"]
            if fleet_msgs and not chat["busy"]:
                reply = fleet_msgs[-1]
                break
            await asyncio.sleep(0.25)
        self.assertIsNotNone(reply, "no fleet reply arrived")
        self.assertTrue(reply["text"])
        self.assertGreater(reply["cost"], 0)
        self.assertIn("-", reply["ship"])  # ship name, not hex

        # a second message while idle is accepted; empty ones are not
        try:
            status, _, body = await asyncio.to_thread(
                _http, "POST", self.base + "/api/chat", b'{"message": "  "}')
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        self.assertEqual(status, 400)

    async def test_unknown_path_is_404(self):
        try:
            status, _, _ = await asyncio.to_thread(_http, "GET", self.base + "/yok")
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
