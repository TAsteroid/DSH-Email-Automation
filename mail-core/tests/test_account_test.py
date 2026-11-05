"""``account.test`` dispatch: IMAP vs POP3 receive probe plus optional SMTP.

POP3 and SMTP are exercised against loopback fake servers (see
``tests/fake_servers.py``); the IMAP branch is stubbed because its client is
already covered elsewhere and the fake IMAP server would add no coverage of
the dispatch logic this module is about.
"""

from __future__ import annotations

import json
import socket
import unittest
from pathlib import Path
from unittest import mock

from mail_core import imap_client
from mail_core.protocol import MailCoreError
from mail_core.service import Service
from tests.fake_servers import FakePop3Server, FakeSmtpServer
from tests.support import workspace_temporary_directory

PASSWORD = "s3cret-pass"


def _closed_port() -> int:
    """Return a loopback port that nothing is listening on."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


class AccountTestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)

    def _add(self, **overrides) -> str:
        params = {
            "address": "a@b.com",
            "imapHost": "127.0.0.1",
            "imapPort": 993,
            "imapSecurity": "none",
            "username": "a@b.com",
            "password": PASSWORD,
            "smtpHost": "",
        }
        params.update(overrides)
        return self.service.dispatch("account.add", params)["id"]

    def test_pop3_account_reports_message_count(self):
        with FakePop3Server(messages=7) as server:
            account_id = self._add(protocol="pop3", imapPort=server.port)
            result = self.service.dispatch("account.test", {"id": account_id})

        self.assertEqual(
            result, {"ok": True, "protocol": "pop3", "messages": 7}
        )
        self.assertNotIn("folders", result)
        self.assertNotIn("smtp", result)        # The stored DPAPI blob was decrypted and used for the POP3 login.
        self.assertEqual(server.credentials, [("a@b.com", PASSWORD)])
        self.assertNotIn(PASSWORD, json.dumps(result))

    def test_pop3_failure_uses_imap_error_code_with_protocol_prefix(self):
        account_id = self._add(protocol="pop3", imapPort=_closed_port())

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("account.test", {"id": account_id})
        self.assertEqual(context.exception.code, "IMAP_ERROR")
        self.assertTrue(context.exception.message.startswith("pop3:"))
        self.assertNotIn(PASSWORD, context.exception.message)

    def test_imap_account_reports_folders_and_saves_them(self):
        folders = [{"name": "INBOX", "delimiter": "/", "flags": [r"\HasNoChildren"]}]
        account_id = self._add(protocol="imap")

        with mock.patch.object(
            imap_client, "test_connection", return_value=folders
        ) as probe:
            result = self.service.dispatch("account.test", {"id": account_id})

        self.assertEqual(result, {"ok": True, "protocol": "imap", "folders": folders})
        probe.assert_called_once()
        self.assertEqual(self.service.store.list_folders(account_id), folders)

    def test_smtp_failure_does_not_fail_the_account_test(self):
        with FakePop3Server(messages=2) as pop3, FakeSmtpServer(reject=True) as smtp:
            account_id = self._add(
                protocol="pop3",
                imapPort=pop3.port,
                smtpHost=smtp.host,
                smtpPort=smtp.port,
                smtpSecurity="none",
            )
            result = self.service.dispatch("account.test", {"id": account_id})

        self.assertTrue(result["ok"])
        self.assertEqual(result["protocol"], "pop3")
        self.assertEqual(result["messages"], 2)
        self.assertFalse(result["smtp"]["ok"])
        self.assertTrue(result["smtp"]["detail"])
        self.assertNotIn(PASSWORD, json.dumps(result))

    def test_smtp_probe_does_not_send_mail(self):
        with FakePop3Server(messages=1) as pop3, FakeSmtpServer() as smtp:
            account_id = self._add(
                protocol="pop3",
                imapPort=pop3.port,
                smtpHost=smtp.host,
                smtpPort=smtp.port,
                smtpSecurity="none",
            )
            result = self.service.dispatch("account.test", {"id": account_id})

        self.assertTrue(result["ok"])
        self.assertEqual(result["smtp"], {"ok": True, "detail": ""})
        commands = [command.upper() for command in smtp.commands]
        self.assertTrue(any(command.startswith("EHLO") for command in commands))
        self.assertIn("QUIT", commands)
        self.assertFalse(
            any(command.startswith(("MAIL", "RCPT", "DATA")) for command in commands)
        )

    def test_update_credential_replaces_the_stored_password(self):
        new_password = "new-pass-2026"
        with FakePop3Server(messages=1) as pop3:
            account_id = self._add(protocol="pop3", imapPort=pop3.port)

            result = self.service.dispatch(
                "account.updateCredential",
                {"id": account_id, "password": new_password},
            )
            self.assertEqual(result["id"], account_id)
            self.assertTrue(result["updatedAt"].endswith("Z"))

            tested = self.service.dispatch("account.test", {"id": account_id})
            self.assertTrue(tested["ok"])

        self.assertEqual(pop3.credentials, [("a@b.com", new_password)])
        # The new plaintext must not reach the database files either.
        for name in ("mail.db", "mail.db-wal"):
            path = self.service.data_dir / name
            if path.exists():
                self.assertNotIn(new_password.encode("utf-8"), path.read_bytes())

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "account.updateCredential",
                {"id": "acc_missing", "password": new_password},
            )
        self.assertEqual(context.exception.code, "NOT_FOUND")

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "account.updateCredential", {"id": account_id, "password": "   "}
            )
        self.assertEqual(context.exception.code, "BAD_REQUEST")


if __name__ == "__main__":
    unittest.main()
