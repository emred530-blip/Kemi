import unittest

from kemi import crypto
from kemi.crypto import (
    SigningKey,
    canonical,
    open_envelope,
    sign_envelope,
    verify_signature,
)
from kemi.identity import Identity, verify_node_id


class CryptoTests(unittest.TestCase):
    def test_sign_verify_roundtrip(self):
        key = SigningKey.generate()
        message = "merhaba dünya".encode()
        signature = key.sign(message)
        self.assertTrue(verify_signature(key.public_key, message, signature))
        self.assertFalse(verify_signature(key.public_key, message + b"!", signature))
        self.assertFalse(verify_signature(key.public_key, message, b"\x00" * 64))

    def test_pure_python_backend_interoperates(self):
        """The stdlib-only fallback must agree with the primary backend."""
        key = SigningKey.generate()
        message = b"cross-backend test"
        # pure implementation derives the same public key from the same seed
        self.assertEqual(crypto._pure_public_key(key._seed), key.public_key)
        # pure-signed messages verify under the primary backend and vice versa
        pure_sig = crypto._pure_sign(key._seed, message)
        self.assertTrue(verify_signature(key.public_key, message, pure_sig))
        self.assertTrue(crypto._pure_verify(key.public_key, message, key.sign(message)))

    def test_envelope_roundtrip_and_tamper_detection(self):
        key = SigningKey.generate()
        payload = {"kind": "test", "değer": 42}
        envelope = sign_envelope(key, payload)
        self.assertEqual(open_envelope(envelope), payload)
        tampered = {**envelope, "payload": {"kind": "test", "değer": 43}}
        self.assertIsNone(open_envelope(tampered))
        self.assertIsNone(open_envelope({"payload": payload}))

    def test_canonical_is_deterministic(self):
        self.assertEqual(canonical({"b": 1, "a": 2}), canonical({"a": 2, "b": 1}))


class IdentityTests(unittest.TestCase):
    def test_identity_pow(self):
        identity = Identity.create(difficulty=8)
        self.assertTrue(verify_node_id(identity.node_id, identity.public_key_hex,
                                       identity.pow_nonce, difficulty=8))
        # wrong nonce, wrong key, or higher difficulty all fail
        self.assertFalse(verify_node_id(identity.node_id, identity.public_key_hex,
                                        identity.pow_nonce + 1, difficulty=8))
        other = Identity.create(difficulty=4)
        self.assertFalse(verify_node_id(identity.node_id, other.public_key_hex,
                                        identity.pow_nonce, difficulty=8))
        self.assertFalse(verify_node_id(identity.node_id, identity.public_key_hex,
                                        identity.pow_nonce, difficulty=255))

    def test_identity_persistence(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "identity.json"
            first = Identity.load_or_create(path, difficulty=4)
            second = Identity.load_or_create(path, difficulty=4)
            self.assertEqual(first.node_id, second.node_id)
            self.assertEqual(first.key.seed_hex, second.key.seed_hex)


if __name__ == "__main__":
    unittest.main()
