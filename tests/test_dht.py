import time
import unittest

from kemi.crypto import sign_envelope
from kemi.dht import DHTNode, RoutingTable, Contact
from kemi.identity import Identity

DIFF = 4


class RoutingTableTests(unittest.TestCase):
    def test_closest_ordering(self):
        identity = Identity.create(difficulty=DIFF)
        table = RoutingTable(identity.node_id)
        # few enough contacts that no k-bucket can overflow and evict
        contacts = [Contact(Identity.create(difficulty=DIFF).node_id, "127.0.0.1", 1000 + i)
                    for i in range(6)]
        for contact in contacts:
            table.add(contact)
        target = contacts[0].node_id
        closest = table.closest(target, count=len(contacts))
        expected = sorted(contacts, key=lambda c: int(c.node_id, 16) ^ int(target, 16))
        self.assertEqual([c.node_id for c in closest], [c.node_id for c in expected])
        self.assertEqual(closest[0].node_id, target)

    def test_own_id_never_added(self):
        identity = Identity.create(difficulty=DIFF)
        table = RoutingTable(identity.node_id)
        table.add(Contact(identity.node_id, "127.0.0.1", 1234))
        self.assertEqual(len(table), 0)


class DHTNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.nodes: list[DHTNode] = []

    async def asyncTearDown(self):
        for node in self.nodes:
            node.stop()

    async def _spawn(self, bootstrap: list[tuple[str, int]] | None = None) -> DHTNode:
        node = DHTNode(Identity.create(difficulty=DIFF), host="127.0.0.1", port=0,
                       difficulty=DIFF)
        await node.start()
        if bootstrap:
            await node.bootstrap(bootstrap)
        self.nodes.append(node)
        return node

    async def test_store_and_find_across_swarm(self):
        first = await self._spawn()
        seed = [("127.0.0.1", first.port)]
        for _ in range(4):
            await self._spawn(bootstrap=seed)
        publisher = self.nodes[2]
        reader = self.nodes[4]

        key = "ab" * 32
        envelope = sign_envelope(publisher.identity.key,
                                 {"kind": "test", "data": "merhaba", "ts": time.time()})
        stored_on = await publisher.put(key, envelope)
        self.assertGreaterEqual(stored_on, 1)

        values = await reader.get(key)
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["payload"]["data"], "merhaba")

    async def test_unsigned_values_are_refused(self):
        first = await self._spawn()
        second = await self._spawn(bootstrap=[("127.0.0.1", first.port)])
        key = "cd" * 32
        stored = await second.put(key, {"payload": {"ts": time.time()}, "pubkey": "", "sig": ""})
        self.assertEqual(stored, 0)
        self.assertEqual(await first.get(key), [])

    async def test_stale_records_are_refused(self):
        first = await self._spawn()
        second = await self._spawn(bootstrap=[("127.0.0.1", first.port)])
        old = sign_envelope(second.identity.key,
                            {"kind": "test", "ts": time.time() - 3600})
        self.assertEqual(await second.put("ef" * 32, old), 0)

    async def test_observed_endpoint(self):
        first = await self._spawn()
        second = await self._spawn(bootstrap=[("127.0.0.1", first.port)])
        observed = second.observed_endpoint
        self.assertIsNotNone(observed)
        self.assertEqual(observed, ("127.0.0.1", second.port))


if __name__ == "__main__":
    unittest.main()
