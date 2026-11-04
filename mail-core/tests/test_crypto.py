"""DPAPI tests: real Windows round-trip, ciphertext differs, tampering rejected."""

from __future__ import annotations

import base64
import sys
import unittest

from mail_core import crypto


@unittest.skipUnless(sys.platform == "win32", "DPAPI is only available on Windows")
class DpapiTests(unittest.TestCase):
    PASSWORD = "p@ssw0rd-邮件-123"

    def test_roundtrip_and_ciphertext_is_not_plaintext(self):
        token = crypto.encrypt_password(self.PASSWORD)

        self.assertIsInstance(token, str)
        self.assertNotEqual(token, self.PASSWORD)
        self.assertNotIn(self.PASSWORD, token)
        self.assertEqual(crypto.decrypt_password(token), self.PASSWORD)

        raw = base64.b64decode(token)
        self.assertNotIn(self.PASSWORD.encode("utf-8"), raw)
        self.assertGreater(len(raw), len(self.PASSWORD))

    def test_roundtrip_of_raw_bytes(self):
        blob = crypto.protect(b"hello dpapi")
        self.assertNotEqual(blob, b"hello dpapi")
        self.assertEqual(crypto.unprotect(blob), b"hello dpapi")

    def test_tampered_ciphertext_is_rejected(self):
        token = crypto.encrypt_password("hunter2")
        blob = bytearray(base64.b64decode(token))
        self.assertGreater(len(blob), 16)

        blob[len(blob) // 2] ^= 0x01
        tampered = base64.b64encode(bytes(blob)).decode("ascii")

        with self.assertRaises(crypto.DpapiError) as context:
            crypto.decrypt_password(tampered)
        self.assertEqual(context.exception.code, "DPAPI_ERROR")

    def test_garbage_token_is_rejected(self):
        for bad in ("not-base64!!!", "", "%%%%"):
            with self.subTest(token=bad):
                with self.assertRaises(crypto.DpapiError) as context:
                    crypto.decrypt_password(bad)
                self.assertEqual(context.exception.code, "DPAPI_ERROR")


if __name__ == "__main__":
    unittest.main()
