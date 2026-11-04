"""Thread assembly and history backfill tests (schema v7)."""

from __future__ import annotations

import unittest
from pathlib import Path

from mail_core.protocol import MailCoreError
from mail_core.service import Service
from tests.fake_servers import FakeImapServer, make_message
from tests.support import workspace_temporary_directory

PASSWORD = "s3cret-pass"

ROOT = make_message(
    subject="Project kickoff",
    from_addr="alice@example.com",
    to_addr="me@example.com",
    date="Mon, 02 Nov 2026 09:00:00 +0000",
    message_id="<root@example.com>",
    body="root body",
)
REPLY_ONE = make_message(
    subject="Re: Project kickoff",
    from_addr="bob@example.com",
    to_addr="alice@example.com",
    date="Tue, 03 Nov 2026 10:00:00 +0000",
    message_id="<reply1@example.com>",
    in_reply_to="<root@example.com>",
    references="<root@example.com>",
    body="reply one",
)
REPLY_TWO = make_message(
    subject="Re: Project kickoff",
    from_addr="alice@example.com",
    to_addr="bob@example.com",
    date="Wed, 04 Nov 2026 11:00:00 +0000",
    message_id="<reply2@example.com>",
    in_reply_to="<reply1@example.com>",
    references="<root@example.com> <reply1@example.com>",
    body="reply two",
)
THREAD = [ROOT, REPLY_ONE, REPLY_TWO]


class ThreadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)

    def _add_account(self, port: int, *, address: str = "me@example.com") -> str:
        return self.service.dispatch(
            "account.add",
            {
                "address": address,
                "displayName": "线程测试",
                "imapHost": "127.0.0.1",
                "imapPort": port,
                "imapSecurity": "none",
                "username": address,
                "password": PASSWORD,
                "smtpHost": "",
            },
        )["id"]

    def _sync(self, account_id: str, **params) -> dict:
        params.setdefault("accountId", account_id)
        return self.service.dispatch("sync.run", params)

    def _threads(self, **params) -> list[dict]:
        return self.service.dispatch("thread.list", params)["threads"]

    # -- merge rules -------------------------------------------------------

    def test_reference_chain_forms_one_thread(self):
        with FakeImapServer(THREAD) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)

        threads = self._threads(accountId=account_id)
        self.assertEqual(len(threads), 1)
        thread = threads[0]
        self.assertEqual(thread["threadId"], f"{account_id}:<root@example.com>")
        self.assertEqual(thread["subject"], "Project kickoff")
        self.assertEqual(thread["messageCount"], 3)
        self.assertEqual(thread["unreadCount"], 3)
        self.assertEqual(thread["lastDate"], "2026-11-04T11:00:00Z")
        self.assertEqual(
            {address.lower() for address in thread["participants"]},
            {"alice@example.com", "bob@example.com", "me@example.com"},
        )

        detail = self.service.dispatch(
            "thread.get", {"threadId": thread["threadId"]}
        )
        self.assertEqual(detail["threadId"], thread["threadId"])
        self.assertEqual(detail["subject"], "Project kickoff")
        self.assertEqual(
            [message["subject"] for message in detail["messages"]],
            ["Project kickoff", "Re: Project kickoff", "Re: Project kickoff"],
        )
        self.assertEqual(
            [message["date"] for message in detail["messages"]],
            [
                "2026-11-02T09:00:00Z",
                "2026-11-03T10:00:00Z",
                "2026-11-04T11:00:00Z",
            ],
        )
        self.assertEqual(
            {message["threadId"] for message in detail["messages"]},
            {thread["threadId"]},
        )

        listed = self.service.dispatch("message.list", {"accountId": account_id})
        self.assertEqual(
            {message["threadId"] for message in listed["messages"]},
            {thread["threadId"]},
        )

        # Folder filter works and an unknown folder has no threads.
        self.assertEqual(len(self._threads(accountId=account_id, folder="INBOX")), 1)
        self.assertEqual(self._threads(accountId=account_id, folder="Sent"), [])

    def test_messages_without_references_become_separate_threads(self):
        first = make_message(
            subject="独立一",
            date="Mon, 02 Nov 2026 09:00:00 +0000",
            message_id="<lonely1@example.com>",
        )
        second = make_message(
            subject="独立二",
            date="Tue, 03 Nov 2026 09:00:00 +0000",
            message_id="<lonely2@example.com>",
        )
        with FakeImapServer([first, second]) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)

        threads = self._threads(accountId=account_id)
        self.assertEqual(len(threads), 2)
        self.assertEqual(
            {thread["threadId"] for thread in threads},
            {
                f"{account_id}:<lonely1@example.com>",
                f"{account_id}:<lonely2@example.com>",
            },
        )
        self.assertEqual(
            {thread["messageCount"] for thread in threads},
            {1},
        )
        # Newest activity first.
        self.assertEqual(threads[0]["lastDate"], "2026-11-03T09:00:00Z")

    def test_same_message_id_in_two_accounts_stays_separate(self):
        with FakeImapServer([ROOT]) as server:
            account_a = self._add_account(server.port)
            account_b = self._add_account(server.port, address="other@example.com")
            self._sync(account_a)
            self._sync(account_b)

        threads_a = self._threads(accountId=account_a)
        threads_b = self._threads(accountId=account_b)
        self.assertEqual(len(threads_a), 1)
        self.assertEqual(len(threads_b), 1)
        self.assertNotEqual(
            threads_a[0]["threadId"], threads_b[0]["threadId"]
        )
        self.assertEqual(len(self._threads()), 2)

        detail_a = self.service.dispatch(
            "thread.get", {"threadId": threads_a[0]["threadId"]}
        )
        self.assertEqual(
            {message["accountId"] for message in detail_a["messages"]},
            {account_a},
        )

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("thread.get", {"threadId": "missing:thread"})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    # -- history backfill --------------------------------------------------

    def test_history_backfill_is_idempotent_and_does_not_move_the_cursor(self):
        with FakeImapServer(THREAD) as server:
            account_id = self._add_account(server.port)
            # Limit 2 keeps only the newest messages (reply one + reply two).
            self._sync(account_id, limit=2)
            self.assertEqual(
                self._threads(accountId=account_id)[0]["messageCount"], 2
            )

            first = self.service.dispatch(
                "history.backfill",
                {
                    "accountId": account_id,
                    "folder": "INBOX",
                    "before": "2026-11-03T00:00:00Z",
                    "limit": 10,
                },
            )
            second = self.service.dispatch(
                "history.backfill",
                {
                    "accountId": account_id,
                    "folder": "INBOX",
                    "before": "2026-11-03T00:00:00Z",
                    "limit": 10,
                },
            )
            # The forward cursor stayed at the newest UID.
            self.assertEqual(self._sync(account_id)["stored"], 0)

        self.assertEqual(first["fetched"], 1)
        self.assertEqual(first["stored"], 1)
        self.assertEqual(first["oldestDate"], "2026-11-02T09:00:00Z")
        self.assertEqual(second["fetched"], 0)
        self.assertEqual(second["stored"], 0)
        self.assertEqual(second["oldestDate"], "2026-11-02T09:00:00Z")
        # The old root joined the thread the replies already formed.
        thread = self._threads(accountId=account_id)[0]
        self.assertEqual(thread["messageCount"], 3)
        self.assertEqual(
            self.service.dispatch("message.list", {"accountId": account_id})["total"],
            3,
        )

    def test_history_backfill_only_stores_messages_before_the_cutoff(self):
        with FakeImapServer(THREAD) as server:
            account_id = self._add_account(server.port)
            result = self.service.dispatch(
                "history.backfill",
                {
                    "accountId": account_id,
                    "before": "2026-11-03T00:00:00Z",
                    "limit": 50,
                },
            )

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["stored"], 1)
        self.assertEqual(result["oldestDate"], "2026-11-02T09:00:00Z")
        listed = self.service.dispatch("message.list", {"accountId": account_id})
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["messages"][0]["subject"], "Project kickoff")

    def test_sync_run_backfill_mode_matches_history_backfill(self):
        with FakeImapServer(THREAD) as server:
            account_id = self._add_account(server.port)
            result = self._sync(
                account_id,
                mode="backfill",
                before="2026-11-03",
                limit=10,
            )

        self.assertEqual(result["before"], "2026-11-03T00:00:00Z")
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["stored"], 1)
        self.assertEqual(result["oldestDate"], "2026-11-02T09:00:00Z")


if __name__ == "__main__":
    unittest.main()
