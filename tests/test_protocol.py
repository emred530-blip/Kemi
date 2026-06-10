import asyncio
import unittest

from kemi.protocol import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    read_message,
    request,
    send_message,
)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip(self):
        received = {}

        async def handler(reader, writer):
            received.update(await read_message(reader))
            await send_message(writer, {"ok": True, "echo": received})
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            message = {"type": "test", "data": ["ü", "çok", 42, {"iç": None}]}
            response = await request("127.0.0.1", port, message)
            self.assertEqual(received, message)
            self.assertEqual(response["echo"], message)
        finally:
            server.close()
            await server.wait_closed()

    async def test_oversize_message_rejected(self):
        class FakeWriter:
            def write(self, data):
                raise AssertionError("should not write")

            async def drain(self):
                pass

        with self.assertRaises(ProtocolError):
            await send_message(FakeWriter(), {"data": "x" * (MAX_MESSAGE_BYTES + 1)})


class IdentityTests(unittest.TestCase):
    def test_token_verification(self):
        from kemi.identity import Identity, verify_token

        identity = Identity.create()
        self.assertTrue(verify_token(identity.node_id, identity.token))
        self.assertFalse(verify_token(identity.node_id, "wrong-token"))


if __name__ == "__main__":
    unittest.main()
