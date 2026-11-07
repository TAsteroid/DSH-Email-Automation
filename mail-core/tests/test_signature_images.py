"""Signature images (schema v14): upload, preview, delete and CID embedding."""

from __future__ import annotations

import base64
import sqlite3
import unittest
from pathlib import Path

from mail_core import sender
from mail_core.protocol import MailCoreError
from mail_core.service import Service
from mail_core.store import MIGRATIONS, SCHEMA_VERSION, Store
from tests.support import workspace_temporary_directory

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

ACCOUNT_ROW = """
INSERT INTO accounts (
    id, address, display_name, imap_host, imap_port, imap_security,
    smtp_host, smtp_port, smtp_security, username, password_blob,
    enabled, created_at, updated_at
) VALUES (
    'acc_old', 'old@b.com', '旧账号', 'imap.old.com', 993, 'ssl',
    '', 465, 'ssl', 'old@b.com', NULL,
    1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
)
"""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class SignatureImageMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

class SignatureImageServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)
        self.account_id = self.service.dispatch(
            "account.add",
            {
                "address": "me@example.com",
                "imapHost": "imap.example.com",
                "username": "me@example.com",
            },
        )["id"]

    def _upload(self, data: bytes = PNG, filename: str = "logo.png") -> dict:
        return self.service.dispatch(
            "account.addSignatureImage",
            {
                "accountId": self.account_id,
                "filename": filename,
                "contentBase64": _b64(data),
            },
        )

    def _images(self) -> list[dict]:
        return self.service.dispatch(
            "account.listSignatureImages", {"accountId": self.account_id}
        )["images"]

    def test_upload_preview_and_deduplicate(self):
        created = self._upload()
        self.assertTrue(created["id"])
        self.assertEqual(created["cid"], f"sig_{created['id']}")
        self.assertEqual(created["filename"], "logo.png")
        self.assertEqual(created["mime"], "image/png")
        self.assertEqual(created["size"], len(PNG))
        self.assertGreaterEqual(self.service.store.schema_version, 14)

        stored = (
            self.service.data_dir
            / "signature-images"
            / f"{created['id']}.png"
        )
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.read_bytes(), PNG)

        images = self._images()
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["cid"], created["cid"])

        preview = self.service.dispatch(
            "account.getSignatureImage", {"id": created["id"]}
        )
        self.assertEqual(preview["id"], created["id"])
        self.assertEqual(preview["filename"], "logo.png")
        self.assertEqual(preview["mime"], "image/png")
        self.assertEqual(base64.b64decode(preview["contentBase64"]), PNG)

        # Same bytes -> same record/id, no second file on disk.
        again = self._upload(filename="copy.png")
        self.assertEqual(again["id"], created["id"])
        self.assertEqual(len(self._images()), 1)
        self.assertEqual(len(list((self.service.data_dir / "signature-images").glob("*.png"))), 1)

        with self.assertRaises(MailCoreError) as context:
            self._upload(PNG + b"\x00" * (512 * 1024))
        self.assertEqual(context.exception.code, "BAD_REQUEST")

        with self.assertRaises(MailCoreError) as context:
            self._upload(b"plain text, not an image", filename="evil.png")
        self.assertEqual(context.exception.code, "BAD_REQUEST")

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "account.addSignatureImage",
                {
                    "accountId": "acc_missing",
                    "filename": "logo.png",
                    "contentBase64": _b64(PNG),
                },
            )
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_delete_removes_row_and_file_and_is_idempotent(self):
        created = self._upload()
        stored = (
            self.service.data_dir
            / "signature-images"
            / f"{created['id']}.png"
        )
        self.assertTrue(stored.is_file())

        result = self.service.dispatch(
            "account.deleteSignatureImage", {"id": created["id"]}
        )
        self.assertEqual(result, {"deleted": True, "id": created["id"]})
        self.assertFalse(stored.exists())
        self.assertEqual(self._images(), [])

        # Already gone -> NOT_FOUND (no side effects).
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "account.deleteSignatureImage", {"id": created["id"]}
            )
        self.assertEqual(context.exception.code, "NOT_FOUND")


class SignatureImageSenderTests(unittest.TestCase):
    def test_signature_referenced_cid_becomes_a_related_part(self):
        signature = "<div>签名<img SRC='cid:sig_x'></div>"
        images = [
            {
                "cid": "sig_x",
                "mime": "image/png",
                "filename": "logo.png",
                "data": PNG,
            }
        ]
        message, _message_id = sender.build_message(
            from_addr="me@example.com",
            to_addr="you@example.com",
            subject="签名图片",
            body="Hello",
            signature_html=signature,
            signature_images=images,
        )

        self.assertEqual(message.get_content_type(), "multipart/related")
        parts = message.get_payload()
        self.assertEqual(parts[0].get_content_type(), "multipart/alternative")
        alternative_parts = parts[0].get_payload()
        self.assertIn("cid:sig_x", alternative_parts[1].get_content())
        self.assertEqual(parts[0].get_payload()[0].get_content().strip(), "Hello")

        self.assertEqual(parts[1].get_content_type(), "image/png")
        self.assertIn("sig_x", str(parts[1]["Content-ID"]))
        self.assertEqual(parts[1].get_payload(decode=True), PNG)

        # A cid with no stored image is ignored: HTML stays, no image part.
        missing, _ = sender.build_message(
            from_addr="me@example.com",
            to_addr="you@example.com",
            subject="未知图片",
            body="Hello",
            signature_html="<img src=\"cid:nope\">",
            signature_images=images,
        )
        self.assertEqual(missing.get_content_type(), "multipart/alternative")
        self.assertEqual(
            [part.get_content_type() for part in missing.get_payload()],
            ["text/plain", "text/html"],
        )


if __name__ == "__main__":
    unittest.main()
