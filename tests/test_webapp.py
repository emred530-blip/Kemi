"""The public harbor (`kemi web`): visitors chat with the fleet from a browser."""

import asyncio
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request

from kemi.identity import Identity
from kemi.node import PeerNode
from kemi.webapp import WebApp

DIFF = 4


def _post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read())


def _get(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        body = r.read()
        try:
            return r.status, json.loads(body)
        except json.JSONDecodeError:
            return r.status, body.decode()


class HarborTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes: list[PeerNode] = []
        self.bootstrap = await self._spawn()
        self.peers = [("127.0.0.1", self.bootstrap.port)]
        await self._spawn(provide=True, price=0.5)
        self.host_node = await self._spawn()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "harbor.json")
        self.app = WebApp(self.host_node, host="127.0.0.1", port=0,
                          faucet=5.0, state_path=self.state_path)
        await self.app.start()
        self.base = f"http://127.0.0.1:{self.app.port}"

    async def asyncTearDown(self):
        await self.app.stop()
        for node in self.nodes:
            await node.stop()
        self.tmp.cleanup()

    async def _spawn(self, **kwargs) -> PeerNode:
        node = PeerNode(Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
                        bootstrap=getattr(self, "peers", None) or [],
                        difficulty=DIFF, sandbox=False, **kwargs)
        await node.start()
        self.nodes.append(node)
        return node

    async def _ask_and_wait(self, token, message, timeout=20.0):
        status, body = await asyncio.to_thread(
            _post, self.base + "/api/ask", {"token": token, "message": message})
        self.assertEqual(status, 200)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            _, state = await asyncio.to_thread(
                _get, self.base + "/api/chat?t=" + token)
            if not state["busy"]:
                return state
            await asyncio.sleep(0.2)
        self.fail("harbor reply never finished")

    async def test_page_and_hello_mint_a_funded_guest(self):
        status, page = await asyncio.to_thread(_get, self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("Kemi", page)
        status, guest = await asyncio.to_thread(_post, self.base + "/api/hello", {})
        self.assertEqual(status, 200)
        self.assertTrue(guest["token"])
        self.assertIn("-", guest["name"])
        self.assertEqual(guest["balance"], 5.0)
        # same token resumes the same guest instead of minting a new one
        _, again = await asyncio.to_thread(
            _post, self.base + "/api/hello", {"token": guest["token"]})
        self.assertEqual(again["token"], guest["token"])
        self.assertEqual(again["name"], guest["name"])

    async def test_ask_answers_from_the_fleet_and_debits_the_guest(self):
        _, guest = await asyncio.to_thread(_post, self.base + "/api/hello", {})
        state = await self._ask_and_wait(guest["token"], "merhaba filo")
        reply = state["log"][-1]
        self.assertEqual(reply["role"], "fleet")
        self.assertTrue(reply["text"])
        self.assertIn("-", reply["ship"])
        self.assertEqual(reply["model"], "mock")
        self.assertEqual(reply["cost"], 0.5)
        self.assertEqual(state["balance"], 4.5)
        self.assertEqual(state["spent"], 0.5)

    async def test_broke_guest_is_refused_with_a_helpful_error(self):
        _, guest = await asyncio.to_thread(_post, self.base + "/api/hello", {})
        self.app._guests[guest["token"]]["balance"] = 0.0
        try:
            status, body = await asyncio.to_thread(
                _post, self.base + "/api/ask",
                {"token": guest["token"], "message": "hi"})
        except urllib.error.HTTPError as exc:
            status, body = exc.code, json.loads(exc.read())
        self.assertEqual(status, 400)
        self.assertIn("credits", body["error"])

    async def test_bad_token_and_bad_message_are_rejected(self):
        try:
            status, _ = await asyncio.to_thread(
                _get, self.base + "/api/chat?t=nope")
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 401)
        _, guest = await asyncio.to_thread(_post, self.base + "/api/hello", {})
        try:
            status, _ = await asyncio.to_thread(
                _post, self.base + "/api/ask",
                {"token": guest["token"], "message": "x" * 3000})
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 400)

    async def test_guests_survive_a_harbor_restart(self):
        _, guest = await asyncio.to_thread(_post, self.base + "/api/hello", {})
        state = await self._ask_and_wait(guest["token"], "kalici misin?")
        self.assertEqual(state["balance"], 4.5)
        await self.app.stop()
        self.app = WebApp(self.host_node, host="127.0.0.1", port=0,
                          faucet=5.0, state_path=self.state_path)
        await self.app.start()
        self.base = f"http://127.0.0.1:{self.app.port}"
        _, revived = await asyncio.to_thread(
            _post, self.base + "/api/hello", {"token": guest["token"]})
        self.assertEqual(revived["token"], guest["token"])
        self.assertEqual(revived["balance"], 4.5)
        _, state = await asyncio.to_thread(
            _get, self.base + "/api/chat?t=" + guest["token"])
        self.assertEqual(state["log"][-1]["role"], "fleet")


if __name__ == "__main__":
    unittest.main()
