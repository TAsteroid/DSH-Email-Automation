"""Service tests: handshake, account lifecycle, credential hiding, sync.status."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mail_core.protocol import MailCoreError
from mail_core.service import Service
from mail_core.store import SCHEMA_VERSION
from tests.support import workspace_temporary_directory

PASSWORD = "s3cret-pass"


def _add_params(**overrides):
    params = {
        "address": "a@b.com",
        "displayName": "工作邮箱",
        "imapHost": "imap.b.com",
        "imapPort": 993,
        "imapSecurity": "ssl",
        "username": "a@b.com",
        "password": PASSWORD,
        "smtpHost": "smtp.b.com",
        "smtpPort": 465,
        "smtpSecurity": "ssl",
    }
    params.update(overrides)
    return params


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.service = Service(self.data_dir)
        self.addCleanup(self.service.close)

    def _add(self, **overrides):
        return self.service.dispatch("account.add", _add_params(**overrides))["id"]

    def test_handshake_reports_protocol_and_paths(self):
        result = self.service.dispatch("core.handshake", {})

        self.assertEqual(result["protocol"], 1)
        self.assertIsInstance(result["version"], str)
        self.assertEqual(result["schemaVersion"], SCHEMA_VERSION)
        self.assertEqual(Path(result["dataDir"]), self.data_dir.resolve())
        self.assertTrue(result["dbPath"].endswith("mail.db"))
        self.assertTrue(Path(result["dbPath"]).exists())

    def test_account_add_then_list_hides_credentials(self):
        account_id = self._add()
        self.assertTrue(account_id.startswith("acc_"))

        accounts = self.service.dispatch("account.list", {})["accounts"]
        self.assertEqual(len(accounts), 1)
        account = accounts[0]
        self.assertEqual(account["id"], account_id)
        self.assertEqual(account["address"], "a@b.com")
        self.assertEqual(account["displayName"], "工作邮箱")
        self.assertTrue(account["hasCredential"])
        self.assertFalse(any(key.lower().startswith("password") for key in account))

        serialized = json.dumps(accounts, ensure_ascii=False)
        self.assertNotIn(PASSWORD, serialized)

        # The plaintext password must not reach the database files either.
        for path in (self.data_dir / "mail.db", self.data_dir / "mail.db-wal"):
            if path.exists():
                self.assertNotIn(PASSWORD.encode("utf-8"), path.read_bytes())

    def test_account_add_with_pop3_exposes_protocol_not_password(self):
        account_id = self._add(
            protocol="pop3",
            imapHost="pop.old.com",
            imapPort=995,
            smtpHost="",
        )

        accounts = self.service.dispatch("account.list", {})["accounts"]
        account = next(item for item in accounts if item["id"] == account_id)
        self.assertEqual(account["protocol"], "pop3")
        self.assertTrue(account["hasCredential"])
        self.assertFalse(any(key.lower().startswith("password") for key in account))
        self.assertNotIn(PASSWORD, json.dumps(accounts, ensure_ascii=False))

    def test_port_defaults_follow_protocol_and_security(self):
        # (protocol, security) -> (receive port default, SMTP port default)
        expectations = {
            ("imap", "ssl"): (993, 465),
            ("imap", "starttls"): (143, 587),
            ("imap", "none"): (143, 25),
            ("pop3", "ssl"): (995, 465),
            ("pop3", "starttls"): (110, 587),
            ("pop3", "none"): (110, 25),
        }
        for (protocol, security), (receive_port, smtp_port) in expectations.items():
            with self.subTest(protocol=protocol, security=security):
                account_id = self._add(
                    protocol=protocol,
                    imapSecurity=security,
                    imapPort=None,
                    smtpSecurity=security,
                    smtpPort=None,
                )
                account = next(
                    item
                    for item in self.service.dispatch("account.list", {})["accounts"]
                    if item["id"] == account_id
                )
                self.assertEqual(account["imapPort"], receive_port)
                self.assertEqual(account["smtpPort"], smtp_port)
                self.assertEqual(account["protocol"], protocol)

    def test_account_remove(self):
        account_id = self._add()
        self.assertEqual(
            self.service.dispatch("account.remove", {"id": account_id}),
            {"removed": True},
        )
        self.assertEqual(self.service.dispatch("account.list", {})["accounts"], [])

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("account.remove", {"id": account_id})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_sync_status_shape(self):
        status = self.service.dispatch("sync.status", {})

        self.assertEqual(
            set(status["counters"]),
            {
                "accounts",
                "messages",
                "unread",
                "drafts",
                "pendingApprovals",
                "pendingAi",
            },
        )
        for value in status["counters"].values():
            self.assertIsInstance(value, int)
            self.assertGreaterEqual(value, 0)
        self.assertEqual(status["counters"]["accounts"], 0)
        self.assertEqual(status["lastSyncAt"], "")
        json.dumps(status)

    def test_account_test_requires_credential(self):
        account_id = self._add(password=None)

        self.assertFalse(
            self.service.dispatch("account.list", {})["accounts"][0]["hasCredential"]
        )
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("account.test", {"id": account_id})
        self.assertEqual(context.exception.code, "CREDENTIAL_MISSING")

    def test_account_test_unknown_account(self):
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("account.test", {"id": "acc_missing"})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_account_add_validates_required_fields(self):
        cases = (
            ("missing address", {"address": None}),
            ("missing imapHost", {"imapHost": None}),
            ("missing username", {"username": None}),
            ("blank address", {"address": "   "}),
            ("port range", {"imapPort": 70000}),
            ("security mode", {"imapSecurity": "tls"}),
            ("protocol", {"protocol": "smtp"}),
            ("protocol type", {"protocol": 1}),
            ("password type", {"password": 123}),
        )
        for label, overrides in cases:
            with self.subTest(case=label):
                with self.assertRaises(MailCoreError) as context:
                    self.service.dispatch("account.add", _add_params(**overrides))
                self.assertEqual(context.exception.code, "BAD_REQUEST")

    def test_shutdown_sets_flag(self):
        self.assertEqual(self.service.dispatch("shutdown", {}), {"ok": True})
        self.assertTrue(self.service.shutdown_requested)


if __name__ == "__main__":
    unittest.main()
