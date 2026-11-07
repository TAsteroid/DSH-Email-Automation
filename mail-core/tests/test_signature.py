"""HTML account signature: schema v13 storage, sanitising and send MIME."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from mail_core import sender
from mail_core.protocol import MailCoreError
from mail_core.service import Service
from mail_core.store import MIGRATIONS, SCHEMA_VERSION, Store
from tests.support import workspace_temporary_directory

DANGEROUS_SIGNATURE = (
    '<div>签名<br>张三</div>'
    "<script>alert(1)</script>"
    '<iframe src="https://evil.example"></iframe>'
    '<a href="javascript:void(0)" onclick="evil()">link</a>'
)

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


class SignatureMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _make_v12_database(self, path: Path) -> None:
        connection = sqlite3.connect(str(path))
        try:
            connection.executescript(MIGRATIONS[1])
            for target in range(2, 13):
                migration = MIGRATIONS[target]
                if callable(migration):
                    migration(connection)
                else:
                    connection.executescript(migration)
            connection.execute("PRAGMA user_version=12")
            connection.execute(ACCOUNT_ROW)
            connection.commit()
        finally:
            connection.close()

    def test_v12_database_upgrades_to_v13_and_keeps_accounts(self):
        path = self.tmp / "legacy-v12.db"
        self._make_v12_database(path)

        store = Store(path)
        self.addCleanup(store.close)

        self.assertGreaterEqual(SCHEMA_VERSION, 13)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        account = store.get_account("acc_old")
        self.assertEqual(account["address"], "old@b.com")
        self.assertEqual(account["signatureHtml"], "")

        self.assertTrue(
            store.set_account_signature("acc_old", "<b>签名</b>")
        )
        self.assertEqual(store.get_account("acc_old")["signatureHtml"], "<b>签名</b>")

        # Repeatable: re-running v13 (and the whole migration) is a no-op.
        self.assertEqual(store.migrate(), SCHEMA_VERSION)
        MIGRATIONS[13](store._conn)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        self.assertEqual(
            store.get_account("acc_old")["signatureHtml"], "<b>签名</b>"
        )


class SignatureServiceTests(unittest.TestCase):
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

    def _set(self, html: str) -> dict:
        return self.service.dispatch(
            "account.setSignature",
            {"accountId": self.account_id, "html": html},
        )

    def test_set_signature_sanitises_clears_and_validates(self):
        result = self._set(DANGEROUS_SIGNATURE)
        cleaned = result["signatureHtml"]
        self.assertEqual(result["accountId"], self.account_id)
        self.assertIn("<div>签名<br>张三</div>", cleaned)
        self.assertNotIn("script", cleaned.lower())
        self.assertNotIn("iframe", cleaned.lower())
        self.assertNotIn("onclick", cleaned.lower())
        self.assertNotIn("javascript:", cleaned.lower())

        accounts = self.service.dispatch("account.list", {})["accounts"]
        self.assertEqual(accounts[0]["signatureHtml"], cleaned)

        cleared = self._set("")
        self.assertEqual(cleared["signatureHtml"], "")
        accounts = self.service.dispatch("account.list", {})["accounts"]
        self.assertEqual(accounts[0]["signatureHtml"], "")

        with self.assertRaises(MailCoreError) as context:
            self._set("x" * 20001)
        self.assertEqual(context.exception.code, "BAD_REQUEST")

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "account.setSignature",
                {"accountId": "acc_missing", "html": "<b>x</b>"},
            )
        self.assertEqual(context.exception.code, "NOT_FOUND")


class SignatureSenderTests(unittest.TestCase):
    def _message(self, *, body: str, signature_html: str):
        return sender.build_message(
            from_addr="me@example.com",
            to_addr="you@example.com",
            subject="签名测试",
            body=body,
            signature_html=signature_html,
        )[0]

    def _text_part(self, message) -> str:
        return message.get_body(preferencelist=("plain",)).get_content()

    def _html_part(self, message) -> str:
        return message.get_body(preferencelist=("html",)).get_content()

    def test_signature_becomes_html_alternative_and_is_not_duplicated(self):
        signature_html = "<div>签名</div><div>张三</div>"

        message = self._message(body="Hello\nWorld", signature_html=signature_html)
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        self.assertEqual(self._text_part(message).strip(), "Hello\nWorld")
        html_body = self._html_part(message)
        self.assertIn("Hello<br>", html_body)
        self.assertIn("World", html_body)
        self.assertIn(signature_html, html_body)
        # Without a quote divider the signature is still appended at the end.
        self.assertTrue(html_body.rstrip().endswith(signature_html))

        # The composer showed the plain-text signature at the end of the body:
        # it is removed from text/plain and kept once as HTML.
        already_shown = self._message(
            body="Hello\nWorld\n签名\n张三", signature_html=signature_html
        )
        self.assertEqual(self._text_part(already_shown).strip(), "Hello\nWorld")
        html_body = self._html_part(already_shown)
        self.assertEqual(html_body.count("签名"), 1)
        self.assertIn(signature_html, html_body)

        # No signature at all keeps the old single-part text/plain message.
        plain = self._message(body="Hello\nWorld", signature_html="")
        self.assertFalse(plain.is_multipart())
        self.assertEqual(plain.get_content_type(), "text/plain")
        self.assertEqual(plain.get_content().strip(), "Hello\nWorld")

    def test_reply_signature_is_html_before_quoted_text(self):
        signature_html = "<div>签名</div><div>张三</div>"
        body = (
            "回复内容\n"
            "签名\n"
            "张三\n"
            "------------------ 原邮件 ------------------\n"
            "> 原邮件内容"
        )
        message = self._message(body=body, signature_html=signature_html)

        # The text part keeps exactly what the client produced.
        self.assertEqual(self._text_part(message).strip(), body)

        html_body = self._html_part(message)
        self.assertIn("回复内容", html_body)
        self.assertIn("原邮件内容", html_body)
        self.assertLess(
            html_body.index(signature_html),
            html_body.index("原邮件内容"),
            "signature HTML must come before the quoted message",
        )
        self.assertEqual(html_body.count("签名"), 1)
        self.assertNotIn("------------------ 原邮件 ------------------", html_body)


if __name__ == "__main__":
    unittest.main()
