"""Full-text search tests: v1/v3 -> v4 migration, rebuild, escaping, paging."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from mail_core.protocol import MailCoreError
from mail_core.service import Service
from mail_core.store import MIGRATIONS, SCHEMA_VERSION, Store
from tests.support import workspace_temporary_directory

# Exactly the ``message.list`` keys: search adds no new fields, it only
# replaces ``snippet`` with the highlighted FTS excerpt.
MESSAGE_KEYS = {
    "id",
    "accountId",
    "folder",
    "uid",
    "subject",
    "fromAddr",
    "toAddr",
    "date",
    "snippet",
    "seen",
}

ACCOUNT_ROW = """
INSERT INTO accounts (
    id, address, display_name, imap_host, imap_port, imap_security,
    smtp_host, smtp_port, smtp_security, username, password_blob, enabled,
    created_at, updated_at
) VALUES (
    'acc_old', 'old@b.com', '旧账号', 'imap.old.com', 993, 'ssl',
    '', 465, 'ssl', 'old@b.com', NULL, 1,
    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
)
"""


class SearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.service = Service(self.data_dir)
        self.addCleanup(self.service.close)
        self.store = self.service.store
        # Messages cascade from an account, so every test starts with one.
        self.store.add_account(
            account_id="acc_1",
            address="me@example.com",
            imap_host="imap.example.com",
            imap_port=993,
            imap_security="ssl",
            username="me@example.com",
        )

    # -- helpers -----------------------------------------------------------

    def _insert(self, uid: int, **overrides) -> None:
        params = dict(
            account_id="acc_1",
            folder="INBOX",
            uidvalidity=1,
            uid=uid,
            subject="",
            from_addr="",
            to_addr="me@example.com",
            body_text="",
            date="2026-11-04T09:00:00Z",
            snippet="正文预览…",
        )
        params.update(overrides)
        self.assertTrue(self.store.insert_message(**params))

    def _search(self, query: str, **params) -> dict:
        return self.service.dispatch("message.search", {"query": query, **params})

    def _id_for_subject(self, subject: str) -> str:
        row = self.store._conn.execute(
            "SELECT id FROM messages WHERE subject = ?", (subject,)
        ).fetchone()
        self.assertIsNotNone(row, f"no message with subject {subject!r}")
        return row["id"]

    def test_cjk_queries_match_inside_a_longer_word(self):
        """「合同」 must hit 「合同确认书」 — FTS5 tokenisation alone cannot."""
        self._insert(1, subject="合同确认书", body_text="请查收附件")
        self._insert(2, subject="季度报告", body_text="本周完成情况")

        result = self._search("合同")
        self.assertEqual(result["total"], 1, "CJK substring search missed the subject")
        self.assertEqual(result["messages"][0]["subject"], "合同确认书")
        self.assertIn("«合同»", result["messages"][0]["snippet"])

        body_result = self._search("附件")
        self.assertEqual(body_result["total"], 1, "CJK substring search missed the body")
        self.assertIn("«附件»", body_result["messages"][0]["snippet"])

        # 两个词都要命中（AND 语义）
        self.assertEqual(self._search("合同 季度")["total"], 0)

    def test_cjk_queries_do_not_treat_like_wildcards(self):
        self._insert(1, subject="100%完成", body_text="a_b")
        self.assertEqual(self._search("100%")["total"], 1)
        self.assertEqual(self._search("%")["total"], 0, "裸 % 不应匹配全部")

    def _make_v3_database(self, path: Path) -> None:
        """A v3 database with messages that predate the FTS index."""
        connection = sqlite3.connect(str(path))
        try:
            connection.executescript(MIGRATIONS[1])
            MIGRATIONS[2](connection)
            connection.executescript(MIGRATIONS[3])
            connection.execute(ACCOUNT_ROW)
            connection.execute(
                """
                INSERT INTO messages (
                    id, account_id, folder, uidvalidity, uid, subject,
                    from_addr, to_addr, date, body_text, created_at
                ) VALUES (
                    'msg_v3_a', 'acc_old', 'INBOX', 7, 1, '季度报告',
                    '张三 <zhang@example.com>', 'me@b.com',
                    '2026-03-01T08:00:00Z', '本周完成情况如下',
                    '2026-03-01T08:00:01Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO messages (
                    id, account_id, folder, uidvalidity, uid, subject,
                    from_addr, to_addr, date, body_text, created_at
                ) VALUES (
                    'msg_v3_b', 'acc_old', 'INBOX', 7, 2, '会议纪要',
                    '李四 <li@example.com>', 'me@b.com',
                    '2026-03-02T08:00:00Z', '已收到，财务报表 见附件',
                    '2026-03-02T08:00:01Z'
                )
                """
            )
            connection.execute("PRAGMA user_version=3")
            connection.commit()
        finally:
            connection.close()

    # -- migration ---------------------------------------------------------

    def test_v1_database_upgrades_to_v4_with_working_search(self):
        path = self.data_dir / "legacy-v1.db"
        connection = sqlite3.connect(str(path))
        try:
            connection.executescript(MIGRATIONS[1])
            connection.execute(ACCOUNT_ROW)
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        finally:
            connection.close()

        store = Store(path)
        self.addCleanup(store.close)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        self.assertEqual(store.get_account("acc_old")["address"], "old@b.com")

        tables = {
            row["name"]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            )
        }
        self.assertTrue(
            {"contacts", "templates", "messages_fts"} <= tables,
            "schema v4 tables are missing after the v1 upgrade",
        )
        self.assertTrue(
            {"messages_fts_insert", "messages_fts_delete", "messages_fts_update"}
            <= tables
        )

        store.insert_message(
            account_id="acc_old",
            folder="INBOX",
            uidvalidity=7,
            uid=1,
            subject="季度报告",
            body_text="本周完成情况如下",
            from_addr="张三 <zhang@example.com>",
        )
        total, messages = store.search_messages("季度报告")
        self.assertEqual(total, 1)
        self.assertEqual(messages[0]["subject"], "季度报告")

    def test_v3_database_rebuilds_fts_and_keeps_messages(self):
        path = self.data_dir / "legacy-v3.db"
        self._make_v3_database(path)

        store = Store(path)
        self.addCleanup(store.close)

        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        self.assertEqual(store.count_messages(), 2)

        # The rebuild indexed rows that were written before the triggers existed.
        total, messages = store.search_messages("季度报告")
        self.assertEqual(total, 1)
        self.assertEqual(messages[0]["id"], "msg_v3_a")

        total, messages = store.search_messages("财务报表")
        self.assertEqual(total, 1)
        self.assertEqual(messages[0]["id"], "msg_v3_b")

        total, messages = store.search_messages("张三")
        self.assertEqual(total, 1)
        self.assertEqual(messages[0]["id"], "msg_v3_a")

    # -- hits --------------------------------------------------------------

    def test_search_hits_subject_body_and_sender(self):
        self._insert(
            1,
            subject="季度报告",
            from_addr="张三 <zhang@example.com>",
            body_text="本周完成情况如下",
        )
        self._insert(
            2,
            subject="会议纪要",
            from_addr="李四 <li@example.com>",
            body_text="已收到，财务报表 见附件",
        )

        for label, query, uid in (
            ("subject", "季度报告", 1),
            ("body", "财务报表", 2),
            ("sender", "张三", 1),
            ("sender", "李四", 2),
        ):
            with self.subTest(case=label):
                result = self._search(query)
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["messages"][0]["uid"], uid)

        # Terms are ANDed: only the message carrying both terms matches.
        result = self._search("张三 季度报告")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["messages"][0]["uid"], 1)

        result = self._search("张三 财务报表")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["messages"], [])

    def test_search_returns_message_list_fields_plus_highlighted_snippet(self):
        self._insert(1, subject="季度报告", body_text="正文预览不重要")

        listed = self.service.dispatch("message.list", {})["messages"][0]
        result = self._search("季度报告")
        hit = result["messages"][0]

        self.assertEqual(set(hit), MESSAGE_KEYS)
        self.assertEqual(set(hit), set(listed))
        self.assertEqual(hit["id"], listed["id"])
        self.assertEqual(hit["subject"], "季度报告")
        self.assertIn("«季度报告»", hit["snippet"])
        self.assertNotEqual(hit["snippet"], listed["snippet"])

    def test_snippet_excerpts_a_long_body(self):
        long_body = " ".join(["开场白"] * 80) + " 专项审计报告 " + " ".join(["结束语"] * 80)
        self._insert(1, subject="长邮件", body_text=long_body)

        hit = self._search("专项审计报告")["messages"][0]
        snippet = hit["snippet"]
        self.assertIn("«专项审计报告»", snippet)
        self.assertLess(len(snippet), len(long_body))

    def test_no_results_is_an_empty_array(self):
        self._insert(1, subject="季度报告")
        result = self._search("完全不存在的关键词")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["messages"], [])

    # -- paging ------------------------------------------------------------

    def test_total_matches_hits_and_paging_works(self):
        for uid in (1, 2, 3):
            self._insert(uid, subject=f"季度报告 第{uid}期")

        first = self._search("季度报告", limit=2)
        self.assertEqual(first["total"], 3)
        self.assertEqual(len(first["messages"]), 2)

        rest = self._search("季度报告", limit=2, offset=2)
        self.assertEqual(rest["total"], 3)
        self.assertEqual(len(rest["messages"]), 1)

        beyond = self._search("季度报告", limit=2, offset=10)
        self.assertEqual(beyond["total"], 3)
        self.assertEqual(beyond["messages"], [])

        # The pages do not overlap.
        self.assertEqual(
            {message["id"] for message in first["messages"]}
            & {message["id"] for message in rest["messages"]},
            set(),
        )

    def test_limit_defaults_to_50_and_is_capped_at_200(self):
        for uid in range(1, 206):
            self._insert(uid, subject="季度报告")

        self.assertEqual(len(self._search("季度报告")["messages"]), 50)

        capped = self._search("季度报告", limit=999)
        self.assertEqual(capped["total"], 205)
        self.assertEqual(len(capped["messages"]), 200)

    # -- validation --------------------------------------------------------

    def test_empty_query_is_rejected(self):
        for params in ({}, {"query": ""}, {"query": "   "}, {"query": None}, {"query": 7}):
            with self.subTest(params=params):
                with self.assertRaises(MailCoreError) as context:
                    self.service.dispatch("message.search", params)
                self.assertEqual(context.exception.code, "BAD_REQUEST")

    def test_limit_and_offset_are_validated(self):
        for params in ({"limit": 0}, {"limit": -1}, {"offset": -1}):
            with self.subTest(params=params):
                with self.assertRaises(MailCoreError) as context:
                    self.service.dispatch("message.search", {"query": "报告", **params})
                self.assertEqual(context.exception.code, "BAD_REQUEST")

    def test_fts_syntax_characters_are_treated_as_text(self):
        self._insert(1, subject="季度报告", body_text="he said hi")
        queries = (
            'he said "hi"',
            "a - b",
            "*",
            "-",
            "OR",
            "NOT 报告",
            "NEAR(a b)",
            "(unbalanced",
            'foo:bar',
            '"季度报告"',
            "!!!",
            "「」…",
        )
        for query in queries:
            with self.subTest(query=query):
                result = self._search(query)
                self.assertIsInstance(result["total"], int)
                self.assertIsInstance(result["messages"], list)

        # A quoted phrase from the user still matches its literal token.
        self.assertEqual(self._search('"季度报告"')["total"], 1)

    # -- index maintenance -------------------------------------------------

    def test_index_follows_updates_and_deletes(self):
        self._insert(1, subject="季度报告", body_text="本周完成情况如下")
        message_id = self._id_for_subject("季度报告")

        self.store._conn.execute(
            "UPDATE messages SET subject = '年度总结' WHERE id = ?", (message_id,)
        )
        self.store._conn.commit()

        self.assertEqual(self._search("季度报告")["total"], 0)
        self.assertEqual(self._search("年度总结")["total"], 1)

        self.store.delete_messages("acc_1", "INBOX")
        self.assertEqual(self._search("年度总结")["total"], 0)
        self.assertEqual(self._search("年度总结")["messages"], [])


if __name__ == "__main__":
    unittest.main()
