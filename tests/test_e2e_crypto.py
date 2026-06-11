import secrets
import unittest

from kemi import e2e
from kemi.crypto import HAVE_NACL, SigningKey
from kemi.e2e import E2EError, derive_box_key, open_sealed, seal


class PurePrimitiveTests(unittest.TestCase):
    """The pure-Python primitives against published test vectors."""

    def test_x25519_rfc7748_vector(self):
        scalar = bytes.fromhex(
            "a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
        point = bytes.fromhex(
            "e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
        expected = bytes.fromhex(
            "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")
        self.assertEqual(e2e._x25519(scalar, point), expected)

    def test_poly1305_rfc8439_vector(self):
        key = bytes.fromhex(
            "85d6be7857556d337f4452fe42d506a80103808afb0db2fd4abff6af4149f51b")
        message = b"Cryptographic Forum Research Group"
        expected = bytes.fromhex("a8061dc1305136c6c22b8baf0c0127a9")
        self.assertEqual(e2e._poly1305(message, key), expected)

    def test_salsa20_core_known_shape(self):
        block = e2e._salsa20_block(b"\x00" * 32, b"\x00" * 8, 0)
        self.assertEqual(len(block), 64)
        # Salsa20 of an all-zero state must not be all zeros
        self.assertNotEqual(block, b"\x00" * 64)


@unittest.skipUnless(HAVE_NACL, "libsodium oracle not available")
class LibsodiumOracleTests(unittest.TestCase):
    """The pure implementation must be byte-identical to libsodium."""

    def test_secretbox_matches_libsodium(self):
        import nacl.bindings as sodium

        key = secrets.token_bytes(32)
        nonce = secrets.token_bytes(24)
        for size in (0, 1, 31, 32, 33, 64, 65, 1000):
            message = secrets.token_bytes(size)
            ours = e2e._secretbox_seal(key, nonce, message)
            theirs = sodium.crypto_secretbox(message, nonce, key)
            self.assertEqual(ours, theirs, f"mismatch at size {size}")
            self.assertEqual(e2e._secretbox_open(key, nonce, theirs), message)

    def test_key_conversion_matches_libsodium(self):
        import nacl.bindings as sodium

        key = SigningKey.generate()
        ours_pk = e2e._ed25519_pk_to_curve25519(key.public_key)
        theirs_pk = sodium.crypto_sign_ed25519_pk_to_curve25519(key.public_key)
        self.assertEqual(ours_pk, theirs_pk)

        ours_sk = e2e._ed25519_seed_to_curve25519(key.seed)
        theirs_sk = sodium.crypto_sign_ed25519_sk_to_curve25519(key.seed + key.public_key)
        self.assertEqual(ours_sk, theirs_sk)

    def test_pure_box_key_matches_libsodium_path(self):
        alice, bob = SigningKey.generate(), SigningKey.generate()
        fast = derive_box_key(alice.seed, bob.public_key)  # libsodium path
        slow_shared = e2e._x25519(e2e._ed25519_seed_to_curve25519(alice.seed),
                                  e2e._ed25519_pk_to_curve25519(bob.public_key))
        slow = e2e._hsalsa20(slow_shared, b"\x00" * 16)
        self.assertEqual(fast, slow)


class BoxApiTests(unittest.TestCase):
    def test_roundtrip_and_symmetry(self):
        alice, bob = SigningKey.generate(), SigningKey.generate()
        key_a = derive_box_key(alice.seed, bob.public_key)
        key_b = derive_box_key(bob.seed, alice.public_key)
        self.assertEqual(key_a, key_b)  # both directions share one key

        message = "gizli görev verisi 🔒".encode()
        sealed = seal(key_a, message)
        self.assertEqual(open_sealed(key_b, sealed), message)

    def test_forgery_is_rejected(self):
        alice, bob = SigningKey.generate(), SigningKey.generate()
        key = derive_box_key(alice.seed, bob.public_key)
        sealed = seal(key, b"deger")
        corrupted = {**sealed, "c": sealed["c"][:-2] + ("00" if sealed["c"][-2:] != "00" else "11")}
        with self.assertRaises(E2EError):
            open_sealed(key, corrupted)

    def test_wrong_key_is_rejected(self):
        alice, bob, eve = (SigningKey.generate() for _ in range(3))
        sealed = seal(derive_box_key(alice.seed, bob.public_key), b"deger")
        eavesdrop_key = derive_box_key(eve.seed, alice.public_key)
        with self.assertRaises(E2EError):
            open_sealed(eavesdrop_key, sealed)


if __name__ == "__main__":
    unittest.main()
