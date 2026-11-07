"""Draft storage and schema v5 migration tests.

Drafts are pure local data; nothing here talks to a mail server.  The
approval-bound send path lives in ``test_send.py``.
"""

from __future__ import annotations

import hashlib
import sqlite3
import unittest
from pathlib import Path

from mail_core import drafts
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


class DraftHashTests(unittest.TestCase):
    def test_hash_is_sha256_of_fields_joined_with_newline(self):
        expected = hashlib.sha256(
            "to@example.com\ncc@example.com\n主题\n正文\n".encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            drafts.content_hash("to@example.com", "cc@example.com", "主题", "正文"),
            expected,
        )
        # The rich-text HTML is part of the approval binding too.
        self.assertNotEqual(
            drafts.content_hash(
                "to@example.com", "cc@example.com", "主题", "正文", "<p>富文本</p>"
            ),
            drafts.content_hash("to@example.com", "cc@example.com", "主题", "正文"),
        )

    def test_hash_normalizes_line_endings(self):
        self.assertEqual(
            drafts.content_hash("a@b.com", "", "s", "line1\r\nline2\rline3"),
            drafts.content_hash("a@b.com", "", "s", "line1\nline2\nline3"),
        )

    def test_hash_changes_with_every_field(self):
        base = drafts.content_hash("a@b.com", "", "s", "b")
        for changed in (
            drafts.content_hash("c@b.com", "", "s", "b"),
            drafts.content_hash("a@b.com", "c@b.com", "s", "b"),
            drafts.content_hash("a@b.com", "", "t", "b"),
            drafts.content_hash("a@b.com", "", "s", "c"),
        ):
            self.assertNotEqual(base, changed)


class SchemaV5MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _make_v4_database(self, path: Path) -> None:
        connection = sqlite3.connect(str(path))
        try:
            connection.executescript(MIGRATIONS[1])
            MIGRATIONS[2](connection)
            connection.executescript(MIGRATIONS[3])
            connection.executescript(MIGRATIONS[4])
            connection.execute("PRAGMA user_version=4")
            connection.execute(ACCOUNT_ROW)
            connection.execute(
                """
                INSERT INTO contacts (id, name, email, created_at, updated_at)
                VALUES ('con_old', '旧联系人', 'old@example.com',
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO templates (id, name, subject, body, created_at, updated_at)
                VALUES ('tpl_old', '旧模板', '标题', '正文',
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """
            )
            connection.commit()
        finally:
            connection.close()

class DraftServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)
        self.account_id = self._add_account()

    def _add_account(self, **overrides) -> str:
        params = {
            "address": "me@example.com",
            "imapHost": "imap.example.com",
            "username": "me@example.com",
        }
        params.update(overrides)
        return self.service.dispatch("account.add", params)["id"]

    def _save(self, **overrides) -> dict:
        params = {
            "accountId": self.account_id,
            "to": "you@example.com",
            "cc": "",
            "subject": "季度报告",
            "body": "您好，\r\n附件是报告。",
        }
        params.update(overrides)
        return self.service.dispatch("draft.save", params)

    def test_save_creates_and_get_returns_every_field(self):
        saved = self._save()

        self.assertTrue(saved["id"].startswith("dft_"))
        self.assertEqual(saved["status"], "draft")
        self.assertEqual(
            saved["contentHash"],
            drafts.content_hash("you@example.com", "", "季度报告", "您好，\r\n附件是报告。"),
        )

        draft = self.service.dispatch("draft.get", {"id": saved["id"]})
        self.assertEqual(draft["id"], saved["id"])
        self.assertEqual(draft["accountId"], self.account_id)
        self.assertEqual(draft["to"], "you@example.com")
        self.assertEqual(draft["cc"], "")
        self.assertEqual(draft["subject"], "季度报告")
        self.assertEqual(draft["body"], "您好，\r\n附件是报告。")
        self.assertEqual(draft["contentHash"], saved["contentHash"])
        self.assertEqual(draft["status"], "draft")
        self.assertTrue(draft["createdAt"].endswith("Z"))
        self.assertEqual(draft["updatedAt"], draft["createdAt"])

    def test_save_updates_in_place_and_recomputes_hash(self):
        created = self._save()
        before = self.service.dispatch("draft.get", {"id": created["id"]})
        updated = self._save(draftId=created["id"], body="您好，\r\n正文已修改。")

        self.assertEqual(updated["id"], created["id"])
        self.assertNotEqual(updated["contentHash"], created["contentHash"])
        self.assertEqual(
            updated["contentHash"],
            drafts.content_hash("you@example.com", "", "季度报告", "您好，\r\n正文已修改。"),
        )

        after = self.service.dispatch("draft.get", {"id": created["id"]})
        self.assertEqual(after["body"], "您好，\r\n正文已修改。")
        self.assertEqual(after["createdAt"], before["createdAt"])
        self.assertGreaterEqual(after["updatedAt"], before["updatedAt"])
        self.assertEqual(len(self.service.dispatch("draft.list", {})["drafts"]), 1)

    def test_list_filters_by_account_and_returns_summaries(self):
        first = self._save(subject="第一封")
        second = self._save(subject="第二封")
        other_account = self._add_account(address="other@example.com")
        third = self._save(accountId=other_account, subject="别人的草稿")

        all_drafts = self.service.dispatch("draft.list", {})["drafts"]
        self.assertEqual(len(all_drafts), 3)
        self.assertEqual(
            set(all_drafts[0]),
            {
                "id",
                "accountId",
                "to",
                "subject",
                "bodyHtml",
                "contentHash",
                "status",
                "updatedAt",
            },
        )
        self.assertNotIn("body", all_drafts[0])

        mine = self.service.dispatch("draft.list", {"accountId": self.account_id})["drafts"]
        self.assertEqual({d["id"] for d in mine}, {first["id"], second["id"]})

        theirs = self.service.dispatch("draft.list", {"accountId": other_account})["drafts"]
        self.assertEqual([d["id"] for d in theirs], [third["id"]])

    def test_list_orders_by_most_recent_edit(self):
        account_id = self.account_id
        older = self.service.store.create_draft(
            account_id=account_id,
            to_addr="a@b.com",
            subject="旧",
            content_hash="h1",
            created_at="2026-01-01T00:00:00Z",
        )
        newer = self.service.store.create_draft(
            account_id=account_id,
            to_addr="a@b.com",
            subject="新",
            content_hash="h2",
            created_at="2026-02-01T00:00:00Z",
        )
        listed = self.service.dispatch("draft.list", {})["drafts"]
        self.assertEqual([d["id"] for d in listed], [newer["id"], older["id"]])

    def test_remove_and_unknown_id(self):
        saved = self._save()
        self.assertEqual(
            self.service.dispatch("draft.remove", {"id": saved["id"]}),
            {"removed": True},
        )
        self.assertEqual(self.service.dispatch("draft.list", {})["drafts"], [])

        for method in ("draft.get", "draft.remove"):
            with self.subTest(method=method):
                with self.assertRaises(MailCoreError) as context:
                    self.service.dispatch(method, {"id": saved["id"]})
                self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_save_unknown_account_or_draft_is_not_found(self):
        with self.assertRaises(MailCoreError) as context:
            self._save(accountId="acc_missing")
        self.assertEqual(context.exception.code, "NOT_FOUND")

        with self.assertRaises(MailCoreError) as context:
            self._save(draftId="dft_missing")
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_save_validates_required_fields(self):
        cases = (
            ("missing account", {"accountId": None}),
            ("missing to", {"to": None}),
            ("blank to", {"to": "   "}),
            ("to type", {"to": 123}),
            ("cc type", {"cc": 1}),
            ("subject type", {"subject": []}),
            ("body type", {"body": {}}),
            ("empty draftId", {"draftId": "  "}),
            ("draftId type", {"draftId": 7}),
        )
        for label, overrides in cases:
            with self.subTest(case=label):
                with self.assertRaises(MailCoreError) as context:
                    self._save(**overrides)
                self.assertEqual(context.exception.code, "BAD_REQUEST")


if __name__ == "__main__":
    unittest.main()
