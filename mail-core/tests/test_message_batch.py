"""Batch message RPCs: ``message.markSeen`` and ``message.move``."""

from __future__ import annotations

import unittest
from pathlib import Path

from mail_core.protocol import MailCoreError
from mail_core.service import Service
from tests.fake_servers import FakeImapServer, FakePop3Server, make_message
from tests.support import workspace_temporary_directory

PASSWORD = "s3cret-pass"

MESSAGES = [
    make_message(
        subject=f"批量邮件 {index}",
        message_id=f"<batch{index}@example.com>",
        date=f"Mon, 0{index} Feb 2026 09:00:00 +0000",
        body=f"body {index}",
    )
    for index in range(1, 4)
]


class BatchMessageTests(unittest.TestCase):
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
                "protocol": protocol,
                "imapHost": "127.0.0.1",
                "imapPort": port,
                "imapSecurity": "none",
                "username": "me@example.com",
                "password": PASSWORD,
            },
        )["id"]

    def _sync(self, account_id: str) -> None:
        self.service.dispatch("sync.run", {"accountId": account_id})

    def _messages(self) -> list[dict]:
        return self.service.dispatch("message.list", {})["messages"]

    def _seen(self) -> dict[str, bool]:
        return {
            message["id"]: message["seen"] for message in self._messages()
        }

    # -- message.markSeen --------------------------------------------------

    def test_mark_seen_batch_updates_local_and_remote(self):
        with FakeImapServer(MESSAGES) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            first, second = self._messages()[:2]
            ids = [first["id"], second["id"]]

            read = self.service.dispatch(
                "message.markSeen", {"ids": ids, "seen": True}
            )
            self.assertEqual(read, {"updated": 2, "skipped": 0, "remote": True})
            seen = self._seen()
            self.assertTrue(seen[first["id"]])
            self.assertTrue(seen[second["id"]])

            unread = self.service.dispatch(
                "message.markSeen", {"ids": ids, "seen": False}
            )
            self.assertEqual(
                unread, {"updated": 2, "skipped": 0, "remote": True}
            )
            seen = self._seen()
            self.assertFalse(seen[first["id"]])
            self.assertFalse(seen[second["id"]])

            partial = self.service.dispatch(
                "message.markSeen",
                {"ids": [first["id"], "msg_missing"], "seen": True},
            )
            self.assertEqual(
                partial, {"updated": 1, "skipped": 1, "remote": True}
            )
            stores = list(server.stores)

        flags = {(uid, flag) for uid, flag in stores}
        self.assertIn((first["uid"], "+FLAGS (\\Seen)"), flags)
        self.assertIn((second["uid"], "-FLAGS (\\Seen)"), flags)

        for bad_ids in ([], "not-a-list", [1], [""]):
            with self.subTest(ids=bad_ids):
                with self.assertRaises(MailCoreError) as context:
                    self.service.dispatch(
                        "message.markSeen", {"ids": bad_ids, "seen": True}
                    )
                self.assertEqual(context.exception.code, "BAD_REQUEST")

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "message.markSeen", {"ids": ["x"], "seen": "yes"}
            )
        self.assertEqual(context.exception.code, "BAD_REQUEST")

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "message.markSeen",
                {"ids": [f"msg_{index}" for index in range(501)], "seen": True},
            )
        self.assertEqual(context.exception.code, "BAD_REQUEST")

    # -- message.move ------------------------------------------------------

    def _move_one(self, *, support_move: bool):
        with FakeImapServer(MESSAGES, support_move=support_move) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            message = self._messages()[0]

            result = self.service.dispatch(
                "message.move", {"ids": [message["id"]], "folder": "Archive"}
            )
            state = {
                "moves": list(server.moves),
                "copies": list(server.copies),
                "stores": list(server.stores),
                "uid_expunges": server.uid_expunges,
            }
        return result, message, state

    def test_move_uses_move_when_supported_and_falls_back_to_copy(self):
        result, message, state = self._move_one(support_move=True)
        self.assertEqual(result["moved"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertIsNone(self.service.store.get_message(message["id"]))
        self.assertTrue(state["moves"], "UID MOVE should be used")
        self.assertEqual(state["copies"], [])
        self.assertEqual(state["uid_expunges"], 0)

        result, message, state = self._move_one(support_move=False)
        self.assertEqual(result["moved"], 1)
        self.assertEqual(result["errors"], [])
        self.assertIsNone(self.service.store.get_message(message["id"]))
        self.assertEqual(state["moves"], [])
        self.assertEqual(state["copies"], [(message["uid"], "Archive")])
        self.assertIn((message["uid"], "+FLAGS (\\Deleted)"), state["stores"])
        self.assertEqual(state["uid_expunges"], 1)

    def test_move_target_missing_fails_and_pop3_is_skipped(self):
        with FakeImapServer(
            MESSAGES, missing_folders={"Missing"}
        ) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            message = self._messages()[0]

            result = self.service.dispatch(
                "message.move",
                {"ids": [message["id"]], "folder": "Missing"},
            )

        self.assertEqual(result["moved"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["errors"])
        self.assertIn("ImapError", result["errors"][0]["error"])
        self.assertIn("Missing", result["errors"][0]["target"])
        self.assertIsNotNone(self.service.store.get_message(message["id"]))

        with FakePop3Server(messages=2) as server:
            pop3_id = self._add_account(server.port, protocol="pop3")
            self._sync(pop3_id)
            pop3_messages = [
                message
                for message in self._messages()
                if message["accountId"] == pop3_id
            ]
            self.assertEqual(len(pop3_messages), 2)

            result = self.service.dispatch(
                "message.move",
                {"ids": [item["id"] for item in pop3_messages], "folder": "INBOX"},
            )

        self.assertEqual(result["moved"], 0)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["failed"], 0)
        remaining = [
            message
            for message in self._messages()
            if message["accountId"] == pop3_id
        ]
        self.assertEqual(len(remaining), 2)


if __name__ == "__main__":
    unittest.main()
