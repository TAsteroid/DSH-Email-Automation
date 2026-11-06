"""Sync tests: IMAP/POP3 fetch, idempotence, UIDVALIDITY reset, message APIs."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from mail_core import mime_parse
from mail_core.protocol import MailCoreError
from mail_core.service import Service
from tests.fake_servers import FakeImapServer, FakePop3Server, make_message
from tests.support import workspace_temporary_directory

PASSWORD = "s3cret-pass"


def _imap_message(index: int, subject: str, date: str, body: str) -> bytes:
    return make_message(
        subject=subject,
        from_addr=f"sender{index}@example.com",
        to_addr="me@example.com",
        date=date,
        message_id=f"<imap{index}@example.com>",
        body=body,
    )


MESSAGES = [
    _imap_message(1, "First", "Mon, 02 Nov 2026 09:00:00 +0000", "alpha body"),
    _imap_message(2, "Second", "Tue, 03 Nov 2026 10:00:00 +0000", "beta body"),
    _imap_message(3, "Third", "Wed, 04 Nov 2026 11:00:00 +0000", "gamma body"),
]


class SyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)

    def _add_account(self, port: int, *, protocol: str = "imap") -> str:
        return self.service.dispatch(
            "account.add",
            {
                "address": "me@example.com",
                "displayName": "测试邮箱",
                "protocol": protocol,
                "imapHost": "127.0.0.1",
                "imapPort": port,
                "imapSecurity": "none",
                "username": "me@example.com",
                "password": PASSWORD,
                "smtpHost": "",
            },
        )["id"]

    def _sync(self, account_id: str, **params) -> dict:
        params.setdefault("accountId", account_id)
        return self.service.dispatch("sync.run", params)

    def _list(self, **params) -> dict:
        return self.service.dispatch("message.list", params)

    # -- IMAP --------------------------------------------------------------

    def test_imap_sync_lists_three_messages_in_date_order(self):
        with FakeImapServer(MESSAGES) as server:
            account_id = self._add_account(server.port)
            result = self._sync(account_id)

        self.assertEqual(result["accountId"], account_id)
        self.assertEqual(result["folder"], "INBOX")
        self.assertEqual(result["uidValidity"], 12345)
        self.assertEqual(result["fetched"], 3)
        self.assertEqual(result["stored"], 3)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["lastUid"], 3)
        self.assertTrue(result["startedAt"].endswith("Z"))
        self.assertTrue(result["finishedAt"].endswith("Z"))

        listed = self._list(accountId=account_id)
        self.assertEqual(listed["total"], 3)
        self.assertEqual(
            [message["subject"] for message in listed["messages"]],
            ["Third", "Second", "First"],
        )
        for message in listed["messages"]:
            self.assertTrue(message["id"].startswith("msg_"))
            self.assertEqual(message["accountId"], account_id)
            self.assertEqual(message["folder"], "INBOX")
            self.assertFalse(message["seen"])
            self.assertNotIn("text", message)
            self.assertNotIn("html", message)
        self.assertNotIn(PASSWORD, json.dumps(listed, ensure_ascii=False))

    def test_imap_sync_reads_seen_flag(self):
        with FakeImapServer(
            MESSAGES,
            message_flags={1: [r"\Seen"], 2: []},
        ) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            commands = list(server.commands)

        by_uid = {
            message["uid"]: message
            for message in self._list(accountId=account_id)["messages"]
        }
        self.assertTrue(by_uid[1]["seen"])
        self.assertFalse(by_uid[2]["seen"])
        # The FETCH asks for FLAGS in the same round trip as the body.
        self.assertTrue(
            any(
                "FETCH" in command and "FLAGS" in command
                for command in commands
            )
        )

    def test_mark_read_updates_local_and_remote(self):
        with FakeImapServer(MESSAGES) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            message = self._list(accountId=account_id)["messages"][0]

            result = self.service.dispatch(
                "message.markRead", {"id": message["id"]}
            )
            self.assertEqual(result, {"seen": True, "remote": True})
            self.assertEqual(
                list(server.stores), [(message["uid"], "+FLAGS (\\Seen)")]
            )
            # Idempotent: a second call still succeeds.
            again = self.service.dispatch(
                "message.markRead", {"id": message["id"]}
            )
            self.assertTrue(again["seen"])

        detail = self.service.dispatch(
            "message.get", {"id": message["id"]}
        )
        self.assertTrue(detail["seen"])

    def test_mark_read_keeps_local_flag_when_remote_fails(self):
        with FakeImapServer(MESSAGES, reject_store=True) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            message = self._list(accountId=account_id)["messages"][0]

            result = self.service.dispatch(
                "message.markRead", {"id": message["id"]}
            )

        self.assertEqual(result, {"seen": True, "remote": False})
        self.assertTrue(
            self.service.dispatch("message.get", {"id": message["id"]})["seen"]
        )
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("message.markRead", {"id": "msg_missing"})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_second_sync_refreshes_seen_flags_without_refetching_body(self):
        with FakeImapServer(MESSAGES) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            before_flags = {
                message["uid"]: message["seen"]
                for message in self._list(accountId=account_id)["messages"]
            }
            self.assertFalse(before_flags[1])

            # UID 1 was expunged elsewhere, so the response sequence numbers no
            # longer equal UIDs: * 1 FETCH (UID 2 ...).  UID 2 was read in
            # another client; the refresh must key on UID, not the sequence.
            server.deleted_uids["INBOX"] = {1}
            server.message_flags = {2: [r"\Seen"]}
            start = len(server.commands)
            result = self._sync(account_id)
            second_commands = list(server.commands[start:])

        self.assertEqual(result["stored"], 0)
        self.assertEqual(result["fetched"], 0)
        after_flags = {
            message["uid"]: message["seen"]
            for message in self._list(accountId=account_id)["messages"]
        }
        self.assertTrue(after_flags[2], "the UID in the response must win")
        self.assertFalse(after_flags[1], "the sequence number must not be used")
        self.assertFalse(after_flags[3])
        self.assertTrue(
            any(
                "FETCH" in command and "(UID FLAGS)" in command
                for command in second_commands
            )
        )
        self.assertFalse(
            any("BODY.PEEK[]" in command for command in second_commands),
            "the FLAGS refresh must not download bodies again",
        )

    def test_seen_refresh_failure_does_not_fail_sync(self):
        with FakeImapServer(MESSAGES[:2], reject_flags_fetch=True) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)

            server.messages.append(
                make_message(
                    subject="Third",
                    date="Thu, 05 Mar 2026 09:00:00 +0000",
                    message_id="<third@example.com>",
                    body="third body",
                )
            )
            result = self._sync(account_id)

        self.assertEqual(result["stored"], 1)
        self.assertEqual(self._list(accountId=account_id)["total"], 3)

    def test_limit_fetches_newest_first(self):
        with FakeImapServer(MESSAGES) as server:
            account_id = self._add_account(server.port)
            result = self._sync(account_id, limit=2)

            # 限额 2 时应先拿最新的两封（UID 3、2），而不是最旧的两封
            self.assertEqual(result["stored"], 2)
            self.assertEqual(result["lastUid"], 3)
            listed = self._list(accountId=account_id)
            self.assertEqual(
                [message["subject"] for message in listed["messages"]],
                ["Third", "Second"],
            )

            # 再同步一次不应重复入库
            again = self._sync(account_id, limit=2)
            self.assertEqual(again["stored"], 0)
            self.assertEqual(self._list(accountId=account_id)["total"], 2)

    def test_sync_reset_clears_local_copy_and_refetches(self):
        with FakeImapServer(MESSAGES) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            self.assertEqual(self._list(accountId=account_id)["total"], 3)

            reset = self.service.dispatch("sync.reset", {"accountId": account_id, "folder": "INBOX"})
            self.assertEqual(reset["removed"], 3)
            self.assertEqual(self._list(accountId=account_id)["total"], 0)

            again = self._sync(account_id)
            self.assertEqual(again["stored"], 3)
            self.assertEqual(self._list(accountId=account_id)["total"], 3)

    def test_repeated_sync_is_idempotent(self):
        with FakeImapServer(MESSAGES) as server:
            account_id = self._add_account(server.port)
            first = self._sync(account_id)
            second = self._sync(account_id)

        self.assertEqual(first["stored"], 3)
        self.assertEqual(second["stored"], 0)
        self.assertEqual(second["fetched"], 0)
        self.assertEqual(second["skipped"], 0)
        self.assertEqual(second["lastUid"], 3)
        self.assertEqual(self._list(accountId=account_id)["total"], 3)

    def test_uidvalidity_change_resets_and_does_not_error(self):
        with FakeImapServer(MESSAGES) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            server.uidvalidity = 999
            result = self._sync(account_id)

        self.assertEqual(result["uidValidity"], 999)
        self.assertEqual(result["stored"], 3)
        listed = self._list(accountId=account_id)
        self.assertEqual(listed["total"], 3)
        self.assertEqual(
            [message["subject"] for message in listed["messages"]],
            ["Third", "Second", "First"],
        )

    def test_network_error_keeps_stored_messages_and_marks_sync_state(self):
        with FakeImapServer(MESSAGES) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)

        # The listener is closed now: the next sync cannot connect.
        with self.assertRaises(MailCoreError) as context:
            self._sync(account_id)
        self.assertEqual(context.exception.code, "IMAP_ERROR")
        self.assertNotIn(PASSWORD, context.exception.message)

        self.assertEqual(self._list(accountId=account_id)["total"], 3)
        state = self.service.store.get_sync_state(account_id, "INBOX")
        self.assertEqual(state["status"], "error")
        self.assertTrue(state["error"])

    def test_message_get_returns_body_and_unknown_id_is_not_found(self):
        with FakeImapServer(MESSAGES) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)

        listed = self._list(accountId=account_id)["messages"]
        target = next(message for message in listed if message["subject"] == "Second")
        detail = self.service.dispatch("message.get", {"id": target["id"]})

        self.assertEqual(detail["subject"], "Second")
        self.assertEqual(detail["fromAddr"], "sender2@example.com")
        self.assertEqual(detail["toAddr"], "me@example.com")
        self.assertEqual(detail["ccAddr"], "")
        self.assertEqual(detail["text"].strip(), "beta body")
        self.assertIn("html", detail)
        self.assertGreater(detail["size"], 0)
        self.assertNotIn("messageId", detail)
        self.assertNotIn("body_text", detail)
        self.assertNotIn(PASSWORD, json.dumps(detail, ensure_ascii=False))

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("message.get", {"id": "msg_missing"})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_sync_status_counts_match_the_database(self):
        with FakeImapServer(MESSAGES) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)

        status = self.service.dispatch("sync.status", {})
        self.assertEqual(status["counters"]["accounts"], 1)
        self.assertEqual(status["counters"]["messages"], 3)
        self.assertEqual(status["counters"]["unread"], 3)
        self.assertEqual(status["counters"]["messages"], self.service.store.count_messages())
        self.assertEqual(
            status["counters"]["unread"], self.service.store.count_unread_messages()
        )
        self.assertEqual(status["lastSyncAt"], self.service.store.latest_sync_at())
        self.assertTrue(status["lastSyncAt"].endswith("Z"))

    # -- POP3 --------------------------------------------------------------

    def test_pop3_sync_is_idempotent(self):
        bodies = [
            make_message(
                subject=f"pop {index}",
                from_addr=f"pop{index}@example.com",
                date=f"Mon, {index:02d} Nov 2026 0{index}:00:00 +0000",
                message_id=f"<pop{index}@example.com>",
                body=f"pop body {index}",
            )
            for index in range(1, 4)
        ]
        with FakePop3Server(bodies=bodies) as server:
            account_id = self._add_account(server.port, protocol="pop3")
            first = self._sync(account_id, folder="ignored")
            second = self._sync(account_id, folder="ignored")

        self.assertEqual(first["uidValidity"], 0)
        self.assertEqual(first["stored"], 3)
        self.assertEqual(second["stored"], 0)
        listed = self._list(accountId=account_id)
        self.assertEqual(listed["total"], 3)
        self.assertEqual(
            [message["subject"] for message in listed["messages"]],
            ["pop 3", "pop 2", "pop 1"],
        )


class LargeMessageTests(unittest.TestCase):
    def test_body_over_one_megabyte_is_truncated_and_snippet_marked(self):
        raw = make_message(
            subject="big",
            body="x" * (mime_parse.MAX_RAW_BYTES + 100),
        )
        parsed = mime_parse.parse_message(raw, len(raw))

        self.assertLessEqual(
            len(parsed["text"].encode("utf-8")), mime_parse.MAX_BODY_BYTES
        )
        self.assertTrue(parsed["snippet"].endswith(mime_parse.TRUNCATION_MARKER))


if __name__ == "__main__":
    unittest.main()
