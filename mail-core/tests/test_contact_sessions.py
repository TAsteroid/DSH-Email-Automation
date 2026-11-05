"""Contact-session mapping (schema v10): mailbox address -> DSH session.

The mapping survives DSH restarts, so the same sender reuses one session.
``contactSession.set`` upserts on ``(account_id, email)`` with the email
always stripped and lowercased.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from mail_core.protocol import MailCoreError
from mail_core.service import Service
from mail_core.store import MIGRATIONS, SCHEMA_VERSION, Store
from tests.support import workspace_temporary_directory

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


class SchemaV10MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _make_v9_database(self, path: Path) -> None:
        connection = sqlite3.connect(str(path))
        try:
            connection.executescript(MIGRATIONS[1])
            for target in range(2, 10):
                migration = MIGRATIONS[target]
                if callable(migration):
                    migration(connection)
                else:
                    connection.executescript(migration)
            connection.execute("PRAGMA user_version=9")
            connection.execute(ACCOUNT_ROW)
            connection.execute(
                """
                INSERT INTO contacts (id, name, email, created_at, updated_at)
                VALUES ('con_old', '旧联系人', 'old@example.com',
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """
            )
            connection.commit()
        finally:
            connection.close()

    def test_v9_database_upgrades_to_v10_and_keeps_data(self):
        path = self.tmp / "legacy-v9.db"
        self._make_v9_database(path)

        store = Store(path)
        self.addCleanup(store.close)

        self.assertEqual(SCHEMA_VERSION, 10)
        self.assertEqual(store.schema_version, 10)

        tables = {
            row["name"]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("contact_sessions", tables)
        indexes = {
            row["name"]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        self.assertIn("idx_contact_sessions_email", indexes)

        # Old data survives the upgrade.
        self.assertEqual(store.get_account("acc_old")["address"], "old@b.com")
        self.assertEqual([c["name"] for c in store.list_contacts()], ["旧联系人"])

        # The new table is usable for the migrated account.
        mapping = store.set_contact_session(
            account_id="acc_old",
            email="alice@example.com",
            session_id="sess_1",
            workspace_id="ws_1",
            title="Alice",
        )
        self.assertEqual(mapping["sessionId"], "sess_1")

        # Repeatable: re-running v10 (and the whole migration) is a no-op.
        self.assertEqual(store.migrate(), SCHEMA_VERSION)
        MIGRATIONS[10](store._conn)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        self.assertEqual(store.count_contact_sessions(), 1)
        self.assertEqual(
            store.get_contact_session(
                account_id="acc_old", email="alice@example.com"
            )["title"],
            "Alice",
        )


class ContactSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)
        self.account_id = self._add_account()

    def _add_account(self, address: str = "me@example.com") -> str:
        return self.service.dispatch(
            "account.add",
            {"address": address, "imapHost": "imap.example.com", "username": address},
        )["id"]

    def _set(self, **overrides) -> dict:
        params = {
            "accountId": self.account_id,
            "email": "Alice@Example.COM ",
            "sessionId": "sess_1",
            "workspaceId": "ws_1",
            "title": "Alice",
        }
        params.update(overrides)
        return self.service.dispatch("contactSession.set", params)

    def _get(self, **params) -> dict:
        params.setdefault("email", "alice@example.com")
        return self.service.dispatch("contactSession.get", params)

    def test_get_missing_returns_empty_session_not_an_error(self):
        self.assertEqual(self._get(), {"sessionId": ""})
        self.assertEqual(
            self._get(accountId=self.account_id, email="nobody@example.com"),
            {"sessionId": ""},
        )

    def test_set_then_get_normalizes_email_case_and_spaces(self):
        result = self._set()

        self.assertEqual(result["sessionId"], "sess_1")
        self.assertEqual(result["workspaceId"], "ws_1")
        self.assertEqual(result["title"], "Alice")
        self.assertTrue(result["updatedAt"].endswith("Z"))
        self.assertEqual(
            set(result), {"sessionId", "workspaceId", "title", "updatedAt"}
        )

        self.assertEqual(
            self._get(accountId=self.account_id, email="ALICE@example.com"),
            {
                "sessionId": "sess_1",
                "workspaceId": "ws_1",
                "title": "Alice",
                "updatedAt": result["updatedAt"],
            },
        )
        stored = self.service.store.get_contact_session(
            account_id=self.account_id, email="alice@example.com"
        )
        self.assertEqual(stored["email"], "alice@example.com")

    def test_set_upserts_one_row_and_updates_timestamp(self):
        first = self._set()
        second = self._set(sessionId="sess_2", title="Alice 2")

        self.assertEqual(second["sessionId"], "sess_2")
        self.assertEqual(second["title"], "Alice 2")
        self.assertEqual(self.service.store.count_contact_sessions(), 1)
        self.assertGreaterEqual(second["updatedAt"], first["updatedAt"])
        stored = self.service.store.get_contact_session(
            account_id=self.account_id, email="alice@example.com"
        )
        self.assertEqual(stored["sessionId"], "sess_2")

    def test_set_keeps_omitted_metadata(self):
        self._set()
        result = self._set(sessionId="sess_2", workspaceId=None, title=None)

        self.assertEqual(result["sessionId"], "sess_2")
        self.assertEqual(result["workspaceId"], "ws_1")
        self.assertEqual(result["title"], "Alice")

    def test_get_without_account_returns_most_recent_mapping(self):
        other_account = self._add_account("other@example.com")
        self._set(sessionId="sess_main")
        self._set(accountId=other_account, sessionId="sess_other", title="Other")

        self.assertEqual(self._get()["sessionId"], "sess_other")
        self.assertEqual(
            self._get(accountId=self.account_id)["sessionId"], "sess_main"
        )

    def test_set_validation(self):
        cases = (
            ("empty email", {"email": "   "}),
            ("email type", {"email": 1}),
            ("missing sessionId", {"sessionId": ""}),
            ("sessionId type", {"sessionId": 2}),
            ("workspaceId type", {"workspaceId": []}),
            ("title type", {"title": {}}),
            ("missing accountId", {"accountId": None}),
        )
        for label, overrides in cases:
            with self.subTest(case=label):
                params = {
                    "accountId": self.account_id,
                    "email": "alice@example.com",
                    "sessionId": "sess_1",
                }
                params.update(overrides)
                if params.get("accountId") is None:
                    params.pop("accountId")
                with self.assertRaises(MailCoreError) as context:
                    self.service.dispatch("contactSession.set", params)
                self.assertEqual(context.exception.code, "BAD_REQUEST")

        with self.assertRaises(MailCoreError) as context:
            self._set(accountId="acc_missing")
        self.assertEqual(context.exception.code, "NOT_FOUND")

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("contactSession.get", {"email": ""})
        self.assertEqual(context.exception.code, "BAD_REQUEST")

    def test_remove_and_idempotence(self):
        self._set()
        self.assertEqual(
            self.service.dispatch(
                "contactSession.remove",
                {"accountId": self.account_id, "email": "ALICE@example.com"},
            ),
            {"removed": True},
        )
        self.assertEqual(self._get()["sessionId"], "")
        # Removing an absent mapping is a no-op success.
        self.assertEqual(
            self.service.dispatch(
                "contactSession.remove",
                {"accountId": self.account_id, "email": "alice@example.com"},
            ),
            {"removed": True},
        )

    def test_list_order_and_account_filter(self):
        other_account = self._add_account("other@example.com")
        # Control timestamps directly so ordering is deterministic.
        self.service.store.set_contact_session(
            account_id=self.account_id,
            email="older@example.com",
            session_id="s_old",
            now="2026-01-01T00:00:00Z",
        )
        self.service.store.set_contact_session(
            account_id=self.account_id,
            email="newer@example.com",
            session_id="s_new",
            now="2026-02-01T00:00:00Z",
        )
        self.service.store.set_contact_session(
            account_id=other_account,
            email="other@example.com",
            session_id="s_other",
            now="2026-03-01T00:00:00Z",
        )

        listed = self.service.dispatch(
            "contactSession.list", {"accountId": self.account_id}
        )["sessions"]
        self.assertEqual(
            [item["email"] for item in listed],
            ["newer@example.com", "older@example.com"],
        )
        self.assertEqual(
            set(listed[0]), {"email", "sessionId", "workspaceId", "title", "updatedAt"}
        )

        everything = self.service.dispatch("contactSession.list", {})["sessions"]
        self.assertEqual(
            [item["email"] for item in everything],
            ["other@example.com", "newer@example.com", "older@example.com"],
        )

    def test_removing_the_account_cascades_to_its_mappings(self):
        self._set()
        other_account = self._add_account("other@example.com")
        self._set(accountId=other_account, sessionId="sess_other")
        self.assertEqual(self.service.store.count_contact_sessions(), 2)

        self.service.dispatch("account.remove", {"id": self.account_id})

        self.assertEqual(self.service.store.count_contact_sessions(), 1)
        self.assertEqual(self._get(accountId=other_account)["sessionId"], "sess_other")


if __name__ == "__main__":
    unittest.main()
