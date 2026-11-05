"""``admin.rebasePaths`` / ``admin.cleanup`` plus the schema v12 migration.

``admin.cleanup`` may only clear the local body of mail that is old, already
summarised and not still waiting in the follow-up queue; these tests pin that
rule down and prove ``dryRun`` writes nothing.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from mail_core.service import Service
from mail_core.store import MIGRATIONS, SCHEMA_VERSION, Store, utc_now
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

MESSAGE_ROW = """
INSERT INTO messages (id, account_id, folder, uid, subject, body_text, created_at)
VALUES ('msg_old', 'acc_old', 'INBOX', 1, '旧邮件', '旧正文', '2026-01-01T00:00:00Z')
"""


class SchemaV12MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _make_v11_database(self, path: Path) -> None:
        connection = sqlite3.connect(str(path))
        try:
            connection.executescript(MIGRATIONS[1])
            for target in range(2, 12):
                migration = MIGRATIONS[target]
                if callable(migration):
                    migration(connection)
                else:
                    connection.executescript(migration)
            connection.execute("PRAGMA user_version=11")
            connection.execute(ACCOUNT_ROW)
            connection.execute(MESSAGE_ROW)
            connection.commit()
        finally:
            connection.close()

    def test_v11_database_upgrades_to_v12_and_keeps_bodies(self):
        path = self.tmp / "legacy-v11.db"
        self._make_v11_database(path)

        store = Store(path)
        self.addCleanup(store.close)

        self.assertGreaterEqual(SCHEMA_VERSION, 12)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        columns = {
            row[1] for row in store._conn.execute("PRAGMA table_info(messages)")
        }
        self.assertIn("body_purged_at", columns)
        message = store.get_message("msg_old")
        self.assertEqual(message["text"], "旧正文")
        purged = store._conn.execute(
            "SELECT body_purged_at FROM messages WHERE id = 'msg_old'"
        ).fetchone()
        self.assertEqual(purged["body_purged_at"], "")

        # Re-running v12 (and the whole migration) is a no-op.
        self.assertEqual(store.migrate(), SCHEMA_VERSION)
        MIGRATIONS[12](store._conn)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        self.assertEqual(store.get_message("msg_old")["text"], "旧正文")


class AdminMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.service = Service(self.tmp)
        self.addCleanup(self.service.close)
        self.account_id = self.service.dispatch(
            "account.add",
            {
                "address": "me@example.com",
                "imapHost": "imap.example.com",
                "username": "me@example.com",
            },
        )["id"]
        self._uid = 0

    def _message(
        self,
        *,
        subject: str,
        date: str,
        body: str = "正文",
        todo_state: str = "",
        attachments: list[dict] | None = None,
    ) -> str:
        self._uid += 1
        message_id = f"<admin{self._uid}@example.com>"
        self.service.store.insert_message(
            account_id=self.account_id,
            folder="INBOX",
            uidvalidity=1,
            uid=self._uid,
            message_id=message_id,
            subject=subject,
            from_addr="a@b.com",
            date=date,
            body_text=body,
            body_html=f"<p>{body}</p>",
            todo_state=todo_state,
            attachments=attachments or [],
        )
        row = self.service.store._conn.execute(
            "SELECT id FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return str(row["id"])

    def _attachments(self, message_id: str) -> list[dict]:
        return self.service.store.list_attachments(
            message_id=message_id, include_inline=True
        )

    def _diagnostic(self, message_id: str) -> sqlite3.Row:
        return self.service.store._conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()

    # -- rebase ------------------------------------------------------------

    def test_rebase_paths_moves_only_the_old_prefix(self):
        old_root = str(self.tmp / "old-data")
        new_root = str(self.tmp / "new-data")
        other_root = str(self.tmp / "other-data")

        first = self._message(
            subject="一",
            date=utc_now(),
            attachments=[{"filename": "a.pdf", "partId": "2"}],
        )
        second = self._message(
            subject="二",
            date=utc_now(),
            attachments=[{"filename": "b.pdf", "partId": "2"}],
        )
        third = self._message(
            subject="三",
            date=utc_now(),
            attachments=[{"filename": "c.pdf", "partId": "2"}],
        )
        first_attachment = self._attachments(first)[0]["id"]
        second_attachment = self._attachments(second)[0]["id"]
        third_attachment = self._attachments(third)[0]["id"]
        self.service.store.set_attachment_download(
            first_attachment,
            local_path=str(Path(old_root) / "a.pdf"),
            sha256="x",
            fetched_at="2026-11-01T00:00:00Z",
        )
        self.service.store.set_attachment_download(
            second_attachment,
            local_path=str(Path(other_root) / "b.pdf"),
            sha256="x",
            fetched_at="2026-11-01T00:00:00Z",
        )
        self.service.store.set_attachment_download(
            third_attachment,
            local_path=old_root,
            sha256="x",
            fetched_at="2026-11-01T00:00:00Z",
        )

        result = self.service.dispatch(
            "admin.rebasePaths", {"from": old_root, "to": new_root}
        )
        self.assertEqual(result, {"updated": 2})

        paths = {
            row["id"]: row["local_path"]
            for row in self.service.store._conn.execute(
                "SELECT id, local_path FROM attachments"
            )
        }
        self.assertEqual(paths[first_attachment], str(Path(new_root) / "a.pdf"))
        self.assertEqual(paths[third_attachment], new_root)
        self.assertEqual(paths[second_attachment], str(Path(other_root) / "b.pdf"))

        again = self.service.dispatch(
            "admin.rebasePaths", {"from": old_root, "to": new_root}
        )
        self.assertEqual(again, {"updated": 0})

        with self.assertRaises(Exception) as context:
            self.service.dispatch(
                "admin.rebasePaths", {"from": "", "to": new_root}
            )
        self.assertEqual(getattr(context.exception, "code", ""), "BAD_REQUEST")

    # -- cleanup -----------------------------------------------------------

    def test_cleanup_dry_run_then_real_run(self):
        old_date = "2026-01-01T00:00:00Z"
        clean_id = self._message(
            subject="旧邮件有摘要",
            date=old_date,
            body="可以清理的正文",
            attachments=[{"filename": "x.pdf", "partId": "2"}],
        )
        self.service.store.add_summary(
            message_id=clean_id, category="其他", summary="已有总结"
        )
        no_summary_id = self._message(
            subject="旧邮件无摘要", date=old_date, body="没有摘要不能清"
        )
        pending_id = self._message(
            subject="旧邮件待办", date=old_date, body="待办不能清", todo_state="pending"
        )
        self.service.store.add_summary(
            message_id=pending_id, category="其他", summary="已有总结"
        )
        fresh_id = self._message(
            subject="新邮件", date=utc_now(), body="太新不能清"
        )
        self.service.store.add_summary(
            message_id=fresh_id, category="其他", summary="已有总结"
        )

        expected = {
            "candidates": 3,
            "cleaned": 1,
            "skippedNoSummary": 1,
            "skippedTodo": 1,
        }
        dry = self.service.dispatch(
            "admin.cleanup", {"olderThanDays": 30, "dryRun": True}
        )
        self.assertEqual(dry, expected)
        # dryRun wrote nothing.
        for message_id in (clean_id, no_summary_id, pending_id, fresh_id):
            row = self._diagnostic(message_id)
            self.assertNotEqual(row["body_text"], "")
            self.assertEqual(row["body_purged_at"], "")

        real = self.service.dispatch("admin.cleanup", {"olderThanDays": 30})
        self.assertEqual(real, expected)

        cleaned = self._diagnostic(clean_id)
        self.assertEqual(cleaned["body_text"], "")
        self.assertEqual(cleaned["body_html"], "")
        self.assertTrue(cleaned["body_purged_at"].endswith("Z"))
        self.assertEqual(cleaned["subject"], "旧邮件有摘要")
        self.assertEqual(self.service.store.count_attachments(clean_id), 1)

        for message_id in (no_summary_id, pending_id, fresh_id):
            self.assertNotEqual(self._diagnostic(message_id)["body_text"], "")

        # Idempotent: a purged row is no longer a candidate; the still-skipped
        # rows remain visible as "not cleanable yet".
        again = self.service.dispatch(
            "admin.cleanup", {"olderThanDays": 30, "dryRun": True}
        )
        self.assertEqual(again["candidates"], 2)
        self.assertEqual(again["cleaned"], 0)
        self.assertEqual(again["skippedNoSummary"], 1)
        self.assertEqual(again["skippedTodo"], 1)


if __name__ == "__main__":
    unittest.main()
