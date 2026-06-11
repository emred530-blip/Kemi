import unittest

from kemi.reputation import ReputationStore


class ReputationTests(unittest.TestCase):
    def setUp(self):
        self.store = ReputationStore(":memory:")
        self.addCleanup(self.store.close)

    def test_unknown_is_neutral(self):
        self.assertEqual(self.store.score("yeni-dugum"), 0.5)
        self.assertFalse(self.store.is_banned("yeni-dugum"))

    def test_good_history_raises_score(self):
        for _ in range(10):
            self.store.record("a", "chunk_ok")
        self.assertGreater(self.store.score("a"), 0.9)
        self.assertFalse(self.store.is_banned("a"))

    def test_mismatches_get_banned(self):
        for _ in range(3):
            self.store.record("hileli", "mismatch")
        self.assertTrue(self.store.is_banned("hileli"))

    def test_double_spend_is_instant_ban(self):
        self.store.record("sahtekar", "double_spend")
        self.assertTrue(self.store.is_banned("sahtekar"))

    def test_occasional_failures_are_tolerated(self):
        for _ in range(20):
            self.store.record("daldan", "chunk_ok")
        self.store.record("daldan", "chunk_fail")
        self.assertFalse(self.store.is_banned("daldan"))
        self.assertGreater(self.store.score("daldan"), 0.8)


class SandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_sandboxed_execution(self):
        import hashlib

        from kemi.sandbox import run_sandboxed

        results = await run_sandboxed("hash.sha256", ["merhaba"], {}, timeout=30.0)
        self.assertEqual(results, [hashlib.sha256(b"merhaba").hexdigest()])

    async def test_sandbox_rejects_unknown_task(self):
        from kemi.sandbox import SandboxError, run_sandboxed

        with self.assertRaises(SandboxError):
            await run_sandboxed("evil.exec", ["x"], {}, timeout=30.0)


if __name__ == "__main__":
    unittest.main()
