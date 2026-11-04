"""SQLite store tests: schema creation, idempotent migration, account CRUD."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from mail_core.store import MIGRATIONS, SCHEMA_VERSION, Store
from tests.support import workspace_temporary_directory


def _account(**overrides):
    base = dict(
        account_id="acc_1",
        address="a@b.com",
        display_name="工作邮箱",
        imap_host="imap.b.com",
        imap_port=993,
        imap_security="ssl",
        smtp_host="smtp.b.com",
        smtp_port=465,
        smtp_security="ssl",
        username="a@b.com",
        password_blob=None,
    )
    base.update(overrides)
    return base


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "mail.db"
        self.store = Store(self.db_path)
        self.addCleanup(self.store.close)

    def test_creates_schema_and_migration_is_repeatable(self):
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)
        self.store.migrate()
        self.store.migrate()
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)

        tables = {
            row["name"]
            for row in self.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({"accounts", "folders", "sync_state", "meta"} <= tables)
        journal_mode = self.store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(journal_mode.lower(), "wal")

    def test_reopening_existing_database_does_not_fail(self):
        self.store.add_account(**_account())
        self.store.close()

        reopened = Store(self.db_path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version, SCHEMA_VERSION)
        self.assertEqual(len(reopened.list_accounts()), 1)

    def test_v1_database_upgrades_to_v2_with_imap_default(self):
        """An existing v1 database keeps its accounts and defaults to imap."""
        v1_path = Path(self._tmp.name) / "legacy.db"
        connection = sqlite3.connect(str(v1_path))
        try:
            connection.executescript(MIGRATIONS[1])
            connection.execute("PRAGMA user_version=1")
            connection.execute(
                """
                INSERT INTO accounts (
                    id, address, display_name, imap_host, imap_port,
                    imap_security, smtp_host, smtp_port, smtp_security,
                    username, password_blob, enabled, created_at, updated_at
                ) VALUES (
                    'acc_old', 'old@b.com', '旧账号', 'imap.old.com', 993,
                    'ssl', '', 465, 'ssl', 'old@b.com', NULL, 1,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

        store = Store(v1_path)
        self.addCleanup(store.close)

        self.assertEqual(store.schema_version, 2)
        account = store.get_account("acc_old")
        self.assertIsNotNone(account)
        self.assertEqual(account["protocol"], "imap")
        self.assertEqual(account["address"], "old@b.com")

        # Re-running the step (and the whole migrate) must not raise.
        self.assertEqual(store.migrate(), 2)
        MIGRATIONS[2](store._conn)
        self.assertEqual(store.get_account("acc_old")["protocol"], "imap")

    def test_add_and_list_account_without_exposing_secret(self):
        self.store.add_account(**_account(password_blob="ZHVtbXktYmxvYg=="))

        accounts = self.store.list_accounts()
        self.assertEqual(len(accounts), 1)
        account = accounts[0]
        self.assertEqual(account["id"], "acc_1")
        self.assertEqual(account["address"], "a@b.com")
        self.assertEqual(account["protocol"], "imap")
        self.assertEqual(account["imapPort"], 993)
        self.assertTrue(account["hasCredential"])
        self.assertNotIn("password_blob", account)
        self.assertFalse(any(key.lower().startswith("password") for key in account))
        self.assertEqual(self.store.get_credential("acc_1"), "ZHVtbXktYmxvYg==")

    def test_account_without_credential(self):
        self.store.add_account(**_account(password_blob=None))
        account = self.store.get_account("acc_1")
        self.assertFalse(account["hasCredential"])
        self.assertIsNone(self.store.get_credential("acc_1"))
        self.assertFalse(self.store.has_credential("acc_1"))

        # An empty blob must count as "no credential", not "has one".
        self.store.add_account(**_account(account_id="acc_2", password_blob=""))
        self.assertFalse(self.store.get_account("acc_2")["hasCredential"])

    def test_remove_account_cascades_folders(self):
        self.store.add_account(**_account())
        self.store.replace_folders("acc_1", [{"name": "INBOX", "delimiter": "/", "flags": []}])
        self.assertTrue(self.store.remove_account("acc_1"))
        self.assertEqual(self.store.list_accounts(), [])
        self.assertIsNone(self.store.get_account("acc_1"))
        self.assertEqual(self.store.list_folders("acc_1"), [])
        self.assertFalse(self.store.remove_account("acc_1"))

    def test_folders_are_replaced_not_appended(self):
        self.store.add_account(**_account())
        self.store.replace_folders(
            "acc_1",
            [
                {"name": "INBOX", "delimiter": "/", "flags": [r"\HasNoChildren"]},
                {"name": "Sent", "delimiter": "/", "flags": []},
            ],
        )
        folders = self.store.list_folders("acc_1")
        self.assertEqual([folder["name"] for folder in folders], ["INBOX", "Sent"])
        self.assertEqual(folders[0]["flags"], [r"\HasNoChildren"])
        self.assertEqual(folders[1]["delimiter"], "/")

        self.store.replace_folders("acc_1", [{"name": "INBOX", "delimiter": "/", "flags": []}])
        self.assertEqual([f["name"] for f in self.store.list_folders("acc_1")], ["INBOX"])

    def test_sync_state_upsert_and_latest_timestamp(self):
        self.store.add_account(**_account())
        self.assertIsNone(self.store.get_sync_state("acc_1", "INBOX"))

        self.store.set_sync_state(
            "acc_1",
            "INBOX",
            uidvalidity=42,
            last_uid=7,
            last_sync_at="2026-11-04T12:00:00Z",
            status="idle",
        )
        state = self.store.get_sync_state("acc_1", "INBOX")
        self.assertEqual(state["uidvalidity"], 42)
        self.assertEqual(state["lastUid"], 7)
        self.assertEqual(state["lastSyncAt"], "2026-11-04T12:00:00Z")

        self.store.set_sync_state(
            "acc_1",
            "INBOX",
            uidvalidity=42,
            last_uid=9,
            last_sync_at="2026-11-04T13:00:00Z",
            status="error",
            error="boom",
        )
        state = self.store.get_sync_state("acc_1", "INBOX")
        self.assertEqual(state["lastUid"], 9)
        self.assertEqual(state["status"], "error")
        self.assertEqual(self.store.latest_sync_at(), "2026-11-04T13:00:00Z")

    def test_meta_roundtrip(self):
        self.assertIsNone(self.store.get_meta("missing"))
        self.assertEqual(self.store.get_meta("missing", "fallback"), "fallback")
        self.store.set_meta("lastSyncAt", "2026-11-04T12:00:00Z")
        self.assertEqual(self.store.get_meta("lastSyncAt"), "2026-11-04T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
