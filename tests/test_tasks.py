import hashlib
import unittest

from kemi.ai_backends import MockBackend, load_backend
from kemi.tasks import TaskError, run_task


class TaskTests(unittest.TestCase):
    def test_hash_sha256(self):
        results = run_task("hash.sha256", ["merhaba"], {}, {})
        self.assertEqual(results, [hashlib.sha256(b"merhaba").hexdigest()])

    def test_hash_rounds(self):
        once = run_task("hash.sha256", ["x"], {"rounds": 1}, {})
        thrice = run_task("hash.sha256", ["x"], {"rounds": 3}, {})
        self.assertNotEqual(once, thrice)

    def test_wordcount(self):
        results = run_task("text.wordcount", ["bir iki üç"], {}, {})
        self.assertEqual(results[0]["words"], 3)

    def test_matmul_deterministic(self):
        a = run_task("math.matmul", [16], {}, {})
        b = run_task("math.matmul", [16], {}, {})
        self.assertEqual(a, b)

    def test_unknown_task(self):
        with self.assertRaises(TaskError):
            run_task("evil.exec", ["rm -rf /"], {}, {})

    def test_ai_generate_requires_backend(self):
        with self.assertRaises(TaskError):
            run_task("ai.generate", ["selam"], {}, {})

    def test_ai_generate_mock_deterministic(self):
        context = {"ai_backend": MockBackend()}
        first = run_task("ai.generate", ["selam"], {"max_tokens": 8}, context)
        second = run_task("ai.generate", ["selam"], {"max_tokens": 8}, context)
        self.assertEqual(first, second)
        self.assertEqual(len(first[0].split()), 8)

    def test_load_backend(self):
        self.assertEqual(load_backend("mock").name, "mock")
        with self.assertRaises(ValueError):
            load_backend("yok-boyle-bir-sey")


if __name__ == "__main__":
    unittest.main()
