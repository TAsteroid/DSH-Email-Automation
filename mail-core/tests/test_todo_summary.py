"""Follow-up registration and Agent summaries (schema v9).

Two halves:

* ``SyncTodoTests`` drives real syncs against ``FakeImapServer`` to prove the
  baseline rule: the first normal INBOX sync registers nothing and later INBOX
  arrivals become ``pending``; non-INBOX and backfill imports register nothing.
* ``TodoApiTests`` / ``SummaryApiTests`` exercise ``todo.*`` / ``summary.*``
  against rows inserted directly, which keeps the fixtures small.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from mail_core import summaries
from mail_core.protocol import MailCoreError
from mail_core.service import Service
from mail_core.store import MIGRATIONS, SCHEMA_VERSION, SUMMARY_CATEGORIES, Store
from tests.fake_servers import FakeImapServer, make_message
from tests.support import workspace_temporary_directory

PASSWORD = "s3cret-pass"

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
INSERT INTO messages (id, account_id, folder, uid, subject, created_at)
VALUES ('msg_old', 'acc_old', 'INBOX', 1, '旧邮件', '2026-01-01T00:00:00Z')
"""


def _message(index: int, *, subject: str, date: str, from_addr: str = "sender@example.com") -> bytes:
    return make_message(
        subject=subject,
        from_addr=from_addr,
        date=date,
        message_id=f"<sync{index}@example.com>",
        body=f"body {index}",
    )


class SchemaV9MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _make_v8_database(self, path: Path) -> None:
        connection = sqlite3.connect(str(path))
        try:
            connection.executescript(MIGRATIONS[1])
            MIGRATIONS[2](connection)
            connection.executescript(MIGRATIONS[3])
            connection.executescript(MIGRATIONS[4])
            connection.executescript(MIGRATIONS[5])
            MIGRATIONS[6](connection)
            MIGRATIONS[7](connection)
            MIGRATIONS[8](connection)
            connection.execute("PRAGMA user_version=8")
            connection.execute(ACCOUNT_ROW)
            connection.execute(MESSAGE_ROW)
            connection.commit()
        finally:
            connection.close()

    def test_v8_database_upgrades_to_v9_and_keeps_data(self):
        path = self.tmp / "legacy-v8.db"
        self._make_v8_database(path)

        store = Store(path)
        self.addCleanup(store.close)

        self.assertGreaterEqual(SCHEMA_VERSION, 9)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)

        message_columns = {
            row[1] for row in store._conn.execute("PRAGMA table_info(messages)")
        }
        self.assertTrue({"todo_state", "todo_updated_at"} <= message_columns)
        account_columns = {
            row[1] for row in store._conn.execute("PRAGMA table_info(accounts)")
        }
        self.assertIn("todo_baseline_done", account_columns)
        tables = {
            row["name"]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("summaries", tables)

        # Old rows are preserved and start un-registered / un-baselined.
        old = store.get_message("msg_old")
        self.assertEqual(old["subject"], "旧邮件")
        self.assertEqual(store.get_todo_baseline_done("acc_old"), False)

        # New columns/tables are usable after the upgrade.
        self.assertEqual(
            store.list_todos(account_id="acc_old", state=""), [
                {
                    "messageId": "msg_old",
                    "subject": "旧邮件",
                    "fromAddr": "",
                    "date": "",
                    "folder": "INBOX",
                    "todoState": "",
                    "hasSummary": False,
                    "attachmentCount": 0,
                }
            ],
        )
        self.assertTrue(store.set_todo_baseline_done("acc_old"))
        self.assertTrue(store.get_todo_baseline_done("acc_old"))

        # Repeatable: re-running v9 (and the whole migration) is a no-op.
        self.assertEqual(store.migrate(), SCHEMA_VERSION)
        MIGRATIONS[9](store._conn)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        self.assertEqual(store.get_message("msg_old")["subject"], "旧邮件")


class SyncTodoTests(unittest.TestCase):
    """The baseline rule is enforced by the sync layer, not by the UI."""

    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)

    def _add_account(self, port: int) -> str:
        return self.service.dispatch(
            "account.add",
            {
                "address": "me@example.com",
                "imapHost": "127.0.0.1",
                "imapPort": port,
                "imapSecurity": "none",
                "username": "me@example.com",
                "password": PASSWORD,
            },
        )["id"]

    def _sync(self, account_id: str, **params) -> dict:
        params.setdefault("accountId", account_id)
        params.setdefault("folder", "INBOX")
        return self.service.dispatch("sync.run", params)

    def _todos(self, **params) -> list[dict]:
        return self.service.dispatch("todo.list", params)["todos"]

    def test_first_sync_is_baseline_and_later_sync_registers_pending(self):
        messages = [
            _message(1, subject="第一封", date="Mon, 02 Feb 2026 09:00:00 +0000"),
            _message(2, subject="第二封", date="Tue, 03 Feb 2026 09:00:00 +0000"),
        ]
        with FakeImapServer(messages) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)

            self.assertTrue(self.service.store.get_todo_baseline_done(account_id))
            self.assertEqual(self._todos(), [], "baseline mail is not a follow-up")
            self.assertEqual(len(self._todos(state="")), 2)

            server.messages.append(
                _message(
                    3,
                    subject="新到的邮件",
                    date="Wed, 04 Feb 2026 09:00:00 +0000",
                )
            )
            self._sync(account_id)

        todos = self._todos()
        self.assertEqual(len(todos), 1)
        todo = todos[0]
        self.assertEqual(todo["subject"], "新到的邮件")
        self.assertEqual(todo["todoState"], "pending")
        self.assertEqual(todo["folder"], "INBOX")
        self.assertFalse(todo["hasSummary"])
        self.assertEqual(todo["attachmentCount"], 0)
        self.assertEqual(
            set(todo),
            {
                "messageId",
                "subject",
                "fromAddr",
                "date",
                "folder",
                "todoState",
                "hasSummary",
                "attachmentCount",
            },
        )

    def test_non_inbox_sync_does_not_register_follow_ups(self):
        messages = [
            _message(1, subject="第一封", date="Mon, 02 Feb 2026 09:00:00 +0000"),
        ]
        with FakeImapServer(messages) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            self._sync(account_id, folder="Archive")

        self.assertEqual(self._todos(), [])
        self.assertEqual(len(self._todos(state="")), 2, "INBOX + Archive rows")

    def test_backfill_and_history_backfill_do_not_register_follow_ups(self):
        messages = [
            _message(1, subject="第一封", date="Mon, 02 Feb 2026 09:00:00 +0000"),
        ]
        with FakeImapServer(messages) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)

            server.messages.append(
                _message(
                    2,
                    subject="旧邮件甲",
                    date="Mon, 01 Dec 2025 09:00:00 +0000",
                )
            )
            backfill = self._sync(
                account_id,
                mode="backfill",
                before="2026-02-01T00:00:00Z",
            )
            self.assertEqual(backfill["stored"], 1)

            server.messages.append(
                _message(
                    3,
                    subject="旧邮件乙",
                    date="Mon, 01 Nov 2025 09:00:00 +0000",
                )
            )
            history = self.service.dispatch(
                "history.backfill",
                {
                    "accountId": account_id,
                    "folder": "INBOX",
                    "before": "2026-02-01T00:00:00Z",
                },
            )
            self.assertEqual(history["stored"], 1)

        self.assertEqual(self._todos(), [], "backfilled mail must stay unregistered")
        self.assertEqual(len(self._todos(state="")), 3)


class TodoApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)
        self.account_id = self._add_account()
        self._uid = 0

    def _add_account(self, address: str = "me@example.com") -> str:
        return self.service.dispatch(
            "account.add",
            {"address": address, "imapHost": "imap.example.com", "username": address},
        )["id"]

    def _insert(
        self,
        *,
        subject: str,
        date: str,
        todo_state: str = "",
        from_addr: str = "sender@example.com",
        account_id: str = "",
        attachments: list[dict] | None = None,
    ) -> str:
        account_id = account_id or self.account_id
        self._uid += 1
        self.service.store.insert_message(
            account_id=account_id,
            folder="INBOX",
            uidvalidity=1,
            uid=self._uid,
            message_id=f"<todo{self._uid}@example.com>",
            subject=subject,
            from_addr=from_addr,
            date=date,
            body_text="正文",
            todo_state=todo_state,
            attachments=attachments or [],
        )
        row = self.service.store._conn.execute(
            "SELECT id FROM messages WHERE message_id = ?",
            (f"<todo{self._uid}@example.com>",),
        ).fetchone()
        return str(row["id"])

    def test_pending_list_order_fields_and_badges(self):
        older = self._insert(
            subject="较早", date="Mon, 02 Feb 2026 09:00:00 +0000", todo_state="pending"
        )
        newer = self._insert(
            subject="较新",
            date="Wed, 04 Feb 2026 09:00:00 +0000",
            todo_state="pending",
            attachments=[{"filename": "a.pdf", "contentType": "application/pdf", "size": 12}],
        )
        self.service.store.add_summary(
            message_id=newer, category="其他", summary="已有总结"
        )

        todos = self.service.dispatch("todo.list", {})["todos"]
        self.assertEqual([item["messageId"] for item in todos], [newer, older])
        self.assertTrue(todos[0]["hasSummary"])
        self.assertEqual(todos[0]["attachmentCount"], 1)
        self.assertFalse(todos[1]["hasSummary"])

    def test_state_filter_account_filter_and_limit(self):
        pending = self._insert(
            subject="待办", date="Mon, 02 Feb 2026 09:00:00 +0000", todo_state="pending"
        )
        done = self._insert(
            subject="已完成", date="Tue, 03 Feb 2026 09:00:00 +0000", todo_state="done"
        )
        unregistered = self._insert(
            subject="未登记", date="Wed, 04 Feb 2026 09:00:00 +0000"
        )
        other_account = self._add_account("other@example.com")
        other_pending = self._insert(
            subject="别人的待办",
            date="Thu, 05 Feb 2026 09:00:00 +0000",
            todo_state="pending",
            account_id=other_account,
        )

        pending_rows = self.service.dispatch("todo.list", {"state": "pending"})["todos"]
        self.assertEqual(
            {item["messageId"] for item in pending_rows}, {pending, other_pending}
        )
        self.assertEqual(
            [item["messageId"] for item in self.service.dispatch(
                "todo.list", {"state": "pending", "accountId": self.account_id}
            )["todos"]],
            [pending],
        )
        self.assertEqual(
            [item["messageId"] for item in self.service.dispatch(
                "todo.list", {"state": "done"}
            )["todos"]],
            [done],
        )
        self.assertEqual(
            [item["messageId"] for item in self.service.dispatch(
                "todo.list", {"state": ""}
            )["todos"]],
            [unregistered],
            "state '' is its own bucket, not 'all'",
        )
        self.assertEqual(
            len(self.service.dispatch("todo.list", {"state": "pending", "limit": 1})["todos"]),
            1,
        )

    def test_set_transitions_and_validation(self):
        message_id = self._insert(
            subject="待办", date="Mon, 02 Feb 2026 09:00:00 +0000", todo_state="pending"
        )

        result = self.service.dispatch(
            "todo.set", {"messageId": message_id, "state": "done"}
        )
        self.assertEqual(result, {"messageId": message_id, "state": "done"})
        self.assertEqual(self.service.dispatch("todo.list", {})["todos"], [])
        self.assertEqual(
            [item["messageId"] for item in self.service.dispatch(
                "todo.list", {"state": "done"}
            )["todos"]],
            [message_id],
        )

        result = self.service.dispatch(
            "todo.set", {"messageId": message_id, "state": ""}
        )
        self.assertEqual(result["state"], "")
        self.assertEqual(self.service.dispatch("todo.list", {})["todos"], [])

        for bad_state in ("PENDING", "todo", None, 1):
            with self.subTest(state=bad_state):
                params = {"messageId": message_id, "state": bad_state}
                if bad_state is None:
                    params.pop("state")
                with self.assertRaises(MailCoreError) as context:
                    self.service.dispatch("todo.set", params)
                self.assertEqual(context.exception.code, "BAD_REQUEST")

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "todo.set", {"messageId": "msg_missing", "state": "done"}
            )
        self.assertEqual(context.exception.code, "NOT_FOUND")

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("todo.list", {"state": "unknown"})
        self.assertEqual(context.exception.code, "BAD_REQUEST")


class SummaryApiTests(unittest.TestCase):
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
        self._uid = 0

    def _insert(
        self,
        *,
        subject: str,
        from_addr: str = "sender@example.com",
        date: str = "Mon, 02 Feb 2026 09:00:00 +0000",
        message_id: str = "",
        in_reply_to: str = "",
        references: str = "",
        body: str = "正文",
        attachments: list[dict] | None = None,
    ) -> str:
        self._uid += 1
        generated_id = message_id or f"<sum{self._uid}@example.com>"
        self.service.store.insert_message(
            account_id=self.account_id,
            folder="INBOX",
            uidvalidity=1,
            uid=self._uid,
            message_id=generated_id,
            in_reply_to=in_reply_to,
            references=references,
            subject=subject,
            from_addr=from_addr,
            date=date,
            body_text=body,
            attachments=attachments or [],
        )
        row = self.service.store._conn.execute(
            "SELECT id FROM messages WHERE message_id = ? AND account_id = ?",
            (generated_id, self.account_id),
        ).fetchone()
        return str(row["id"])

    def _save(self, message_id: str, **overrides) -> dict:
        params = {
            "messageId": message_id,
            "category": "会议安排",
            "summary": "会议确认了交付时间。",
            "keyPoints": ["时间：周五", "负责人：张三"],
            "entities": {"company": "示例公司", "people": ["张三"]},
            "model": "deepseek-chat",
            "sessionId": "sess_1",
        }
        params.update(overrides)
        return self.service.dispatch("summary.save", params)

    def test_save_stores_summary_and_metadata(self):
        message_id = self._insert(
            subject="项目周会", from_addr="alice@example.com", date="Mon, 02 Feb 2026 09:00:00 +0000"
        )
        result = self._save(message_id)

        self.assertTrue(result["id"].startswith("sum_"))
        self.assertTrue(result["createdAt"].endswith("Z"))

        stored = self.service.store.get_summary(result["id"])
        self.assertEqual(stored["messageId"], message_id)
        self.assertEqual(stored["category"], "会议安排")
        self.assertEqual(stored["summary"], "会议确认了交付时间。")
        self.assertEqual(stored["keyPoints"], ["时间：周五", "负责人：张三"])
        self.assertEqual(stored["entities"], {"company": "示例公司", "people": ["张三"]})
        self.assertEqual(stored["model"], "deepseek-chat")
        self.assertEqual(stored["sessionId"], "sess_1")
        self.assertEqual(stored["subject"], "项目周会")
        self.assertEqual(
            stored["threadId"], self.service.store.get_message(message_id)["threadId"]
        )

    def test_every_fixed_category_is_accepted(self):
        message_id = self._insert(subject="分类测试")
        for category in SUMMARY_CATEGORIES:
            with self.subTest(category=category):
                result = self._save(message_id, category=category)
                self.assertEqual(
                    self.service.store.get_summary(result["id"])["category"],
                    category,
                )

    def test_save_validation(self):
        message_id = self._insert(subject="校验")
        cases = (
            ("bad category", {"category": "闲聊"}),
            ("missing category", {"category": None}),
            ("empty summary", {"summary": "   "}),
            ("summary type", {"summary": 1}),
            ("keyPoints type", {"keyPoints": "not-a-list"}),
            ("keyPoints item", {"keyPoints": [1]}),
            ("entities type", {"entities": []}),
            ("model type", {"model": 1}),
        )
        for label, overrides in cases:
            with self.subTest(case=label):
                params = {
                    "messageId": message_id,
                    "category": "其他",
                    "summary": "内容",
                }
                params.update(overrides)
                if params.get("category") is None:
                    params.pop("category")
                with self.assertRaises(MailCoreError) as context:
                    self.service.dispatch("summary.save", params)
                self.assertEqual(context.exception.code, "BAD_REQUEST")

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "summary.save",
                {"messageId": "msg_missing", "category": "其他", "summary": "内容"},
            )
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_list_filters_and_order(self):
        first = self._insert(subject="第一封", message_id="<root@example.com>")
        second = self._insert(
            subject="Re: 第一封",
            message_id="<reply@example.com>",
            in_reply_to="<root@example.com>",
            references="<root@example.com>",
            date="Tue, 03 Feb 2026 09:00:00 +0000",
        )
        first_summary = self._save(first, category="其他", summary="旧总结")
        second_summary = self._save(second, category="项目进展", summary="新总结")
        with self.service.store._conn:
            self.service.store._conn.execute(
                "UPDATE summaries SET created_at = '2026-01-01T00:00:00Z' WHERE id = ?",
                (first_summary["id"],),
            )
            self.service.store._conn.execute(
                "UPDATE summaries SET created_at = '2026-02-01T00:00:00Z' WHERE id = ?",
                (second_summary["id"],),
            )

        listed = self.service.dispatch("summary.list", {})["summaries"]
        self.assertEqual(
            [item["id"] for item in listed],
            [second_summary["id"], first_summary["id"]],
        )

        by_message = self.service.dispatch(
            "summary.list", {"messageId": first}
        )["summaries"]
        self.assertEqual([item["id"] for item in by_message], [first_summary["id"]])

        thread_id = self.service.store.get_message(second)["threadId"]
        by_thread = self.service.dispatch(
            "summary.list", {"threadId": thread_id}
        )["summaries"]
        self.assertEqual(
            {item["id"] for item in by_thread},
            {first_summary["id"], second_summary["id"]},
        )

        by_category = self.service.dispatch(
            "summary.list", {"category": "项目进展"}
        )["summaries"]
        self.assertEqual([item["id"] for item in by_category], [second_summary["id"]])

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("summary.list", {"category": "闲聊"})
        self.assertEqual(context.exception.code, "BAD_REQUEST")

    def test_deleting_the_message_cascades_to_summaries(self):
        message_id = self._insert(subject="将被删除")
        self._save(message_id)
        self.assertEqual(self.service.store.count_summaries(), 1)

        self.assertTrue(self.service.store.delete_message(message_id))
        self.assertEqual(self.service.store.count_summaries(), 0)

    def test_delete_removes_one_summary_and_unknown_id_is_not_found(self):
        message_id = self._insert(subject="删除总结")
        saved = self._save(message_id)
        self.assertIn(
            saved["id"],
            {
                item["id"]
                for item in self.service.dispatch("summary.list", {})["summaries"]
            },
        )

        result = self.service.dispatch("summary.delete", {"id": saved["id"]})
        self.assertEqual(result, {"deleted": True, "id": saved["id"]})
        self.assertNotIn(
            saved["id"],
            {
                item["id"]
                for item in self.service.dispatch("summary.list", {})["summaries"]
            },
        )
        # The message itself is untouched.
        self.assertIsNotNone(self.service.store.get_message(message_id))

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("summary.delete", {"id": saved["id"]})
        self.assertEqual(context.exception.code, "NOT_FOUND")


class SummaryContextTests(unittest.TestCase):
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
        self._uid = 0
        self._build_fixture()

    def _insert(self, *, subject, from_addr, date, message_id, **extra) -> str:
        self._uid += 1
        self.service.store.insert_message(
            account_id=self.account_id,
            folder="INBOX",
            uidvalidity=1,
            uid=self._uid,
            message_id=message_id,
            subject=subject,
            from_addr=from_addr,
            date=date,
            body_text=f"正文 {message_id}",
            **extra,
        )
        row = self.service.store._conn.execute(
            "SELECT id FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return str(row["id"])

    def _contact(self, name: str, email: str, company: str) -> None:
        self.service.dispatch(
            "contact.add", {"name": name, "email": email, "company": company}
        )

    def _save(self, message_id: str, summary: str, category: str = "其他") -> dict:
        return self.service.dispatch(
            "summary.save",
            {"messageId": message_id, "category": category, "summary": summary},
        )

    def _build_fixture(self) -> None:
        self._contact("Alice", "alice@example.com", "示例公司")
        self._contact("Carol", "carol@example.com", "示例公司")
        self._contact("Bob", "bob@example.com", "另一家公司")

        self.root = self._insert(
            subject="季度报告",
            from_addr="Alice <alice@example.com>",
            date="Mon, 02 Feb 2026 09:00:00 +0000",
            message_id="<root@example.com>",
            attachments=[
                {
                    "filename": "报告.pdf",
                    "contentType": "application/pdf",
                    "size": 2048,
                    "partId": "2",
                }
            ],
        )
        self.reply = self._insert(
            subject="Re: 季度报告",
            from_addr="me@example.com",
            date="Tue, 03 Feb 2026 09:00:00 +0000",
            message_id="<reply@example.com>",
            in_reply_to="<root@example.com>",
            references="<root@example.com>",
        )
        self.same_subject = self._insert(
            subject="Fwd: 季度报告",
            from_addr="bob@example.com",
            date="Wed, 04 Feb 2026 09:00:00 +0000",
            message_id="<bob@example.com>",
        )
        self.same_sender = self._insert(
            subject="另一个话题",
            from_addr="alice@example.com",
            date="Thu, 05 Feb 2026 09:00:00 +0000",
            message_id="<alice2@example.com>",
        )
        self.same_company = self._insert(
            subject="合同进度",
            from_addr="carol@example.com",
            date="Fri, 06 Feb 2026 09:00:00 +0000",
            message_id="<carol@example.com>",
        )
        self.unrelated = self._insert(
            subject="无关通知",
            from_addr="dave@example.com",
            date="Sat, 07 Feb 2026 09:00:00 +0000",
            message_id="<dave@example.com>",
        )

        self._save(self.root, "根邮件历史总结", "会议安排")
        self._save(self.reply, "回复历史总结", "项目进展")
        self._save(self.same_subject, "同主题历史总结", "其他")

    def test_context_combines_message_thread_related_summaries_and_attachments(self):
        context = self.service.dispatch(
            "summary.context", {"messageId": self.root}
        )

        self.assertEqual(context["message"]["id"], self.root)
        self.assertEqual(context["message"]["text"], "正文 <root@example.com>")
        self.assertEqual([item["id"] for item in context["thread"]], [self.reply])

        reasons = {item["id"]: item["reason"] for item in context["related"]}
        self.assertEqual(reasons[self.reply], "同一对话")
        self.assertEqual(reasons[self.same_subject], "主题相关")
        self.assertEqual(reasons[self.same_sender], "同一发件人")
        self.assertEqual(reasons[self.same_company], "同一公司")
        self.assertNotIn(self.root, reasons)
        self.assertNotIn(self.unrelated, reasons)

        summary_ids = {item["messageId"] for item in context["summaries"]}
        self.assertEqual(
            summary_ids, {self.root, self.reply, self.same_subject}
        )
        for item in context["summaries"]:
            self.assertEqual(
                set(item),
                {"messageId", "subject", "category", "summary", "keyPoints", "createdAt"},
            )
            if item["messageId"] == self.root:
                self.assertEqual(item["subject"], "季度报告")

        self.assertEqual(len(context["attachments"]), 1)
        attachment = context["attachments"][0]
        self.assertEqual(
            set(attachment), {"id", "filename", "size", "fetchedAt"}
        )
        self.assertEqual(attachment["filename"], "报告.pdf")
        self.assertEqual(attachment["size"], 2048)
        self.assertEqual(attachment["fetchedAt"], "")

    def test_context_for_unknown_message_is_not_found(self):
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("summary.context", {"messageId": "msg_missing"})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_subject_core_strips_reply_and_forward_prefixes(self):
        for raw, expected in (
            ("Re: 报价单", "报价单"),
            ("RE: FWD: 报价单 - 客户A", "报价单 - 客户A"),
            ("回复：会议安排", "会议安排"),
            ("转发: 通知", "通知"),
            ("季度报告", "季度报告"),
            ("", ""),
        ):
            with self.subTest(subject=raw):
                self.assertEqual(summaries.normalize_subject(raw), expected)


if __name__ == "__main__":
    unittest.main()
