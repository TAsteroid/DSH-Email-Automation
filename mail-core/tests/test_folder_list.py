"""``folder.list`` RPC, plus folder names crossing the IMAP boundary.

``account.test`` fills the local ``folders`` table from a real ``LIST`` reply;
``folder.list`` serves it without touching the network.  The wire names in
:data:`LIST_LINES` are the ones an Alibaba enterprise mailbox actually sends.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from mail_core.protocol import MailCoreError
from mail_core.service import Service
from tests.fake_servers import FakeImapServer, make_message
from tests.support import workspace_temporary_directory

PASSWORD = "s3cret-pass"

LIST_LINES = [
    b'* LIST (\\HasNoChildren) "/" "&XfJT0ZAB-"\r\n',   # 已发送
    b'* LIST (\\HasNoChildren) "/" "&XfJSIJZkkK5O9g-"\r\n',  # 已删除邮件
    b'* LIST (\\HasNoChildren) "/" "&g0l6Pw-"\r\n',  # 草稿
    b'* LIST (\\HasNoChildren) "/" INBOX\r\n',
]

CACHED = [
    {"name": "已发送", "delimiter": "/", "flags": [r"\HasNoChildren"]},
    {"name": "INBOX", "delimiter": "/", "flags": [r"\HasNoChildren"]},
]

MESSAGES = [
    make_message(
        subject="hi",
        date="Tue, 03 Nov 2026 10:00:00 +0000",
        message_id="<one@example.com>",
        body="hello",
    )
]


class FolderListTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)

    def _add(self, port: int = 993) -> str:
        return self.service.dispatch(
            "account.add",
            {
                "address": "me@example.com",
                "protocol": "imap",
                "imapHost": "127.0.0.1",
                "imapPort": port,
                "imapSecurity": "none",
                "username": "me@example.com",
                "password": PASSWORD,
                "smtpHost": "",
            },
        )["id"]

    def test_returns_the_stored_folders(self):
        account_id = self._add()
        self.service.store.replace_folders(account_id, CACHED)

        self.assertEqual(
            self.service.dispatch("folder.list", {"accountId": account_id}),
            {"folders": CACHED},
        )

    def test_account_without_folder_rows_returns_empty_list(self):
        account_id = self._add()

        self.assertEqual(
            self.service.dispatch("folder.list", {"accountId": account_id}),
            {"folders": []},
        )

    def test_unknown_account_is_not_found(self):
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("folder.list", {"accountId": "acc_missing"})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_missing_account_id_is_bad_request(self):
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("folder.list", {})
        self.assertEqual(context.exception.code, "BAD_REQUEST")

    def test_account_test_stores_decoded_names_and_folder_list_reads_them(self):
        with FakeImapServer(MESSAGES, folder_lines=LIST_LINES) as server:
            account_id = self._add(server.port)
            tested = self.service.dispatch("account.test", {"id": account_id})

        self.assertEqual(
            [folder["name"] for folder in tested["folders"]],
            ["已发送", "已删除邮件", "草稿", "INBOX"],
        )

        # No server is listening any more: folder.list must not need one.
        listed = self.service.dispatch("folder.list", {"accountId": account_id})
        self.assertEqual(
            [folder["name"] for folder in listed["folders"]],
            ["已发送", "已删除邮件", "草稿", "INBOX"],
        )

    def test_sync_selects_the_folder_under_its_encoded_wire_name(self):
        with FakeImapServer(MESSAGES, folder_lines=LIST_LINES) as server:
            account_id = self._add(server.port)
            result = self.service.dispatch(
                "sync.run", {"accountId": account_id, "folder": "已发送"}
            )
            selected = list(server.selected)

        self.assertEqual(selected, [("&XfJT0ZAB-", False)])
        self.assertEqual(result["folder"], "已发送")
        self.assertEqual(result["stored"], 1)
        stored = self.service.dispatch(
            "message.list", {"accountId": account_id, "folder": "已发送"}
        )
        self.assertEqual(stored["total"], 1)


if __name__ == "__main__":
    unittest.main()
