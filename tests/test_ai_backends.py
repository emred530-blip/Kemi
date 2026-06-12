import http.server
import json
import threading
import unittest

from kemi.ai_backends import (
    AIBackendError,
    MockBackend,
    OllamaBackend,
    load_backend,
    supports_streaming,
)
from kemi.tasks import TaskError, run_task


class MockBackendTests(unittest.TestCase):
    def test_stream_matches_generate(self):
        backend = MockBackend()
        full = backend.generate("merhaba", max_tokens=10)
        streamed = "".join(backend.stream("merhaba", max_tokens=10))
        self.assertEqual(full, streamed)
        self.assertEqual(len(full.split()), 10)

    def test_supports_streaming(self):
        self.assertTrue(supports_streaming(MockBackend()))
        self.assertTrue(supports_streaming(OllamaBackend()))
        self.assertFalse(supports_streaming(None))

        class NoStream:
            def generate(self, prompt, max_tokens=64):
                return ""

        self.assertFalse(supports_streaming(NoStream()))


class _FakeOllamaHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        if body.get("model") == "yok-model":
            self.wfile.write(json.dumps({"error": "model not found"}).encode() + b"\n")
            return
        for token in ("Mer", "ha", "ba!"):
            self.wfile.write(json.dumps({"response": token, "done": False}).encode() + b"\n")
        self.wfile.write(json.dumps({"response": "", "done": True}).encode() + b"\n")

    def log_message(self, *args):  # keep test output quiet
        pass


class OllamaBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_stream_and_generate(self):
        backend = OllamaBackend(model="llama3.2", url=self.url)
        self.assertEqual(list(backend.stream("selam", 16)), ["Mer", "ha", "ba!"])
        self.assertEqual(backend.generate("selam", 16), "Merhaba!")

    def test_model_error_is_surfaced(self):
        backend = OllamaBackend(model="yok-model", url=self.url)
        with self.assertRaises(AIBackendError):
            backend.generate("selam")

    def test_unreachable_server(self):
        backend = OllamaBackend(url="http://127.0.0.1:1", timeout=2)
        with self.assertRaises(AIBackendError):
            backend.generate("selam")


class NewTaskTests(unittest.TestCase):
    def test_data_aggregate(self):
        records = [
            {"sehir": "ankara", "satis": 10},
            {"sehir": "izmir", "satis": 4},
            {"sehir": "ankara", "satis": 6},
        ]
        out = run_task("data.aggregate", [records],
                       {"group_by": "sehir", "field": "satis", "op": "sum"}, {})
        self.assertEqual(out, [{"ankara": 16.0, "izmir": 4.0}])
        avg = run_task("data.aggregate", [records],
                       {"group_by": "sehir", "field": "satis", "op": "avg"}, {})
        self.assertEqual(avg[0]["ankara"], 8.0)
        count = run_task("data.aggregate", [records],
                         {"group_by": "sehir", "op": "count"}, {})
        self.assertEqual(count[0], {"ankara": 2.0, "izmir": 1.0})
        with self.assertRaises(TaskError):
            run_task("data.aggregate", [records], {"op": "yok"}, {})

    def test_crypto_pbkdf2(self):
        import hashlib

        out = run_task("crypto.pbkdf2", ["parola"],
                       {"iterations": 1000, "salt": "tuz"}, {})
        expected = hashlib.pbkdf2_hmac("sha256", b"parola", b"tuz", 1000).hex()
        self.assertEqual(out, [expected])

    def test_compress_gzip_roundtrip(self):
        import base64
        import gzip

        text = "kemi " * 100
        out = run_task("compress.gzip", [text], {}, {})
        self.assertLess(out[0]["compressed"], out[0]["original"])
        restored = gzip.decompress(base64.b64decode(out[0]["data"])).decode()
        self.assertEqual(restored, text)

    def test_load_backend_names(self):
        self.assertEqual(load_backend("mock").name, "mock")
        self.assertEqual(load_backend("ollama", model="m").name, "ollama")
        with self.assertRaises(ValueError):
            load_backend("yok")


if __name__ == "__main__":
    unittest.main()
