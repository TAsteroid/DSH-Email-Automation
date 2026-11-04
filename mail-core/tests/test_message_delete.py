"""``message.delete`` / ``message.restore`` against a fake IMAP server.

The delete path (Trash-move, permanent expunge, POP3 local-only) and the
restore path run against a real loopback server (``FakeImapServer``), so the
tests can inspect the exact ``UID COPY`` / ``UID STORE`` / ``EXPUNGE`` the
server received, count mailbox growth, and prove the local row is only
dropped once the server-side change was actually verified.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from mail_core import imap_utf7
from mail_core.protocol import MailCoreError
from mail_core.service import Service
from tests.fake_servers import FakeImapServer, FakePop3Server, make_message
from tests.support import workspace_temporary_directory

PASSWORD = "s3cret-pass"

MESSAGES = [
    make_message(
        subject=f"消息 {index}",
        message_id=f"<imap{index}@example.com>",
        body=f"body {index}",
        date=f"Mon, 0{index} Nov 2026 10:00:00 +0000",
    )
    for index in range(1, 4)
]

INBOX_LINE = b'* LIST (\\HasNoChildren) "/" INBOX\r\n'
TRASH_LINE = b'* LIST (\\HasNoChildren) "/" Trash\r\n'
ARCHIVE_LINE = b'* LIST (\\HasNoChildren) "/" Archive\r\n'
DELETED_LINE = (
    '* LIST (\\HasNoChildren) "/" ' + imap_utf7.encode("已删除") + "\r\n"
).encode("utf-8")


class MessageDeleteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)

    # -- helpers -----------------------------------------------------------

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

    def _test_account(self, account_id: str) -> None:
        """Populate the cached folder list (the source of the Trash name)."""
        self.service.dispatch("account.test", {"id": account_id})

    def _sync(self, account_id: str, folder: str = "INBOX") -> dict:
        return self.service.dispatch(
            "sync.run", {"accountId": account_id, "folder": folder}
        )

    def _message(self, account_id: str, *, uid: int = 1, folder: str = "INBOX") -> dict:
        messages = self.service.dispatch(
            "message.list", {"accountId": account_id, "folder": folder}
        )["messages"]
        return next(item for item in messages if item["uid"] == uid)

    def _imap_ready(self, server) -> str:
        account_id = self._add_account(server.port)
        self._test_account(account_id)
        self._sync(account_id)
        return account_id

    # -- IMAP --------------------------------------------------------------

    def test_move_to_trash_copies_flags_expunges_and_drops_local_row(self):
        with FakeImapServer(MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE]) as server:
            account_id = self._imap_ready(server)
            message = self._message(account_id, uid=1)

            result = self.service.dispatch("message.delete", {"id": message["id"]})
            copies = list(server.copies)
            stores = list(server.stores)
            expunges = server.expunges
            commands = list(server.commands)

        self.assertEqual(result, {"deleted": True, "trashed": True, "localOnly": False})
        self.assertEqual(copies, [(1, "Trash")])
        self.assertEqual(stores, [(1, "+FLAGS (\\Deleted)")])
        self.assertEqual(expunges, 1)
        # ``FakeImapServer.commands`` keeps the tag; compare the command body.
        bodies = [
            command.split(" ", 1)[1] if " " in command else command
            for command in commands
        ]
        self.assertLess(
            bodies.index("UID COPY 1 Trash"),
            bodies.index("UID STORE 1 +FLAGS (\\Deleted)"),
        )
        self.assertLess(
            bodies.index("UID STORE 1 +FLAGS (\\Deleted)"),
            bodies.index("EXPUNGE"),
        )

        self.assertEqual(
            self.service.dispatch("message.list", {"accountId": account_id})["total"], 2
        )
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("message.get", {"id": message["id"]})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_missing_trash_folder_degrades_to_permanent_delete(self):
        with FakeImapServer(MESSAGES, folder_lines=[INBOX_LINE]) as server:
            account_id = self._imap_ready(server)
            message = self._message(account_id, uid=2)

            result = self.service.dispatch("message.delete", {"id": message["id"]})
            copies = list(server.copies)
            stores = list(server.stores)
            expunges = server.expunges

        self.assertEqual(
            result, {"deleted": True, "trashed": False, "localOnly": False}
        )
        self.assertEqual(copies, [], "no Trash folder means no COPY")
        self.assertEqual(stores, [(2, "+FLAGS (\\Deleted)")])
        self.assertEqual(expunges, 1)
        self.assertIsNone(self.service.store.get_message(message["id"]))

    def test_permanent_delete_skips_copy_even_when_trash_exists(self):
        with FakeImapServer(MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE]) as server:
            account_id = self._imap_ready(server)
            message = self._message(account_id, uid=3)

            result = self.service.dispatch(
                "message.delete", {"id": message["id"], "permanent": True}
            )
            copies = list(server.copies)
            stores = list(server.stores)

        self.assertEqual(
            result, {"deleted": True, "trashed": False, "localOnly": False}
        )
        self.assertEqual(copies, [])
        self.assertEqual(stores, [(3, "+FLAGS (\\Deleted)")])
        self.assertIsNone(self.service.store.get_message(message["id"]))

    def test_trash_folder_is_matched_after_utf7_decoding(self):
        with FakeImapServer(MESSAGES, folder_lines=[INBOX_LINE, DELETED_LINE]) as server:
            account_id = self._imap_ready(server)
            message = self._message(account_id, uid=1)

            result = self.service.dispatch("message.delete", {"id": message["id"]})
            copies = list(server.copies)

        self.assertEqual(result["trashed"], True)
        self.assertEqual(copies, [(1, imap_utf7.encode("已删除"))])

    def test_deleting_from_trash_is_a_real_delete_without_copy(self):
        with FakeImapServer(MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE]) as server:
            account_id = self._imap_ready(server)
            self._sync(account_id, folder="Trash")
            message = self._message(account_id, uid=1, folder="Trash")

            result = self.service.dispatch("message.delete", {"id": message["id"]})
            copies = list(server.copies)
            stores = list(server.stores)

        self.assertEqual(result["trashed"], False)
        self.assertEqual(copies, [], "copying Trash onto itself would duplicate it")
        self.assertEqual(stores, [(1, "+FLAGS (\\Deleted)")])
        self.assertIsNone(self.service.store.get_message(message["id"]))

    def test_network_failure_keeps_the_local_row(self):
        server = FakeImapServer(MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE])
        server.start()
        self.addCleanup(server.stop)
        account_id = self._imap_ready(server)
        message = self._message(account_id, uid=1)
        server.stop()

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("message.delete", {"id": message["id"]})
        self.assertEqual(context.exception.code, "IMAP_ERROR")
        self.assertNotIn(PASSWORD, context.exception.message)

        # The synced row (and its body) is still there for the user to retry.
        kept = self.service.store.get_message(message["id"])
        self.assertIsNotNone(kept)
        self.assertEqual(kept["uid"], 1)

    def test_copy_that_does_not_land_fails_and_keeps_the_local_row(self):
        server = FakeImapServer(
            MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE], copy_noop=True
        )
        with server:
            account_id = self._imap_ready(server)
            message = self._message(account_id, uid=2)

            with self.assertRaises(MailCoreError) as context:
                self.service.dispatch("message.delete", {"id": message["id"]})
            self.assertEqual(context.exception.code, "IMAP_ERROR")
            self.assertIn("did not land", context.exception.message)
            self.assertEqual(server.stores, [], "must not expunge after a no-op copy")
            self.assertEqual(server.expunges, 0)

        self.assertIsNotNone(self.service.store.get_message(message["id"]))

    def test_expunge_that_leaves_the_message_fails_and_keeps_the_local_row(self):
        server = FakeImapServer(
            MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE], expunge_noop=True
        )
        with server:
            account_id = self._imap_ready(server)
            message = self._message(account_id, uid=1)

            with self.assertRaises(MailCoreError) as context:
                self.service.dispatch("message.delete", {"id": message["id"]})
            self.assertEqual(context.exception.code, "IMAP_ERROR")
            self.assertIn("still on the server", context.exception.message)

        self.assertIsNotNone(self.service.store.get_message(message["id"]))

    def test_permanent_delete_does_not_check_the_trash_count(self):
        # ``copy_noop`` proves the permanent path never looks at Trash.
        server = FakeImapServer(
            MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE], copy_noop=True
        )
        with server:
            account_id = self._imap_ready(server)
            message = self._message(account_id, uid=3)

            result = self.service.dispatch(
                "message.delete", {"id": message["id"], "permanent": True}
            )
            copies = list(server.copies)

        self.assertEqual(result["trashed"], False)
        self.assertEqual(copies, [])
        self.assertIsNone(self.service.store.get_message(message["id"]))

    def test_repeated_delete_is_not_found(self):
        with FakeImapServer(MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE]) as server:
            account_id = self._imap_ready(server)
            message = self._message(account_id, uid=1)
            self.service.dispatch("message.delete", {"id": message["id"]})

            with self.assertRaises(MailCoreError) as context:
                self.service.dispatch("message.delete", {"id": message["id"]})
            self.assertEqual(context.exception.code, "NOT_FOUND")
            self.assertEqual(len(server.copies), 1, "a repeat must not touch the server")

    def test_unknown_message_is_not_found(self):
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("message.delete", {"id": "msg_missing"})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_permanent_must_be_boolean(self):
        with FakeImapServer(MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE]) as server:
            account_id = self._imap_ready(server)
            message = self._message(account_id, uid=1)

            with self.assertRaises(MailCoreError) as context:
                self.service.dispatch(
                    "message.delete", {"id": message["id"], "permanent": "yes"}
                )
            self.assertEqual(context.exception.code, "BAD_REQUEST")
            self.assertIsNotNone(self.service.store.get_message(message["id"]))

    # -- POP3 --------------------------------------------------------------

    def test_pop3_deletes_the_local_copy_only(self):
        with FakePop3Server(messages=3) as server:
            account_id = self._add_account(server.port, protocol="pop3")
            self._test_account(account_id)
            self._sync(account_id)
            message = self._message(account_id, uid=1)
            before = list(server.commands)

            result = self.service.dispatch("message.delete", {"id": message["id"]})
            after = list(server.commands)

        self.assertEqual(
            result, {"deleted": True, "trashed": False, "localOnly": True}
        )
        self.assertEqual(before, after, "POP3 delete must not talk to the server")
        self.assertFalse(
            any(command.upper().startswith("DELE") for command in after),
            "POP3 has no per-message delete; DELE must never be sent",
        )
        self.assertIsNone(self.service.store.get_message(message["id"]))
        self.assertEqual(
            self.service.dispatch("message.list", {"accountId": account_id})["total"], 2
        )


class MessageRestoreTests(unittest.TestCase):
    """``message.restore``: Trash -> real folder, with the same fail-closed checks."""

    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)

    # -- helpers -----------------------------------------------------------

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

    def _ready(self, server, *, with_archive: bool = False) -> str:
        folder_lines = [INBOX_LINE, TRASH_LINE]
        if with_archive:
            folder_lines.append(ARCHIVE_LINE)
        server.folder_lines = folder_lines
        account_id = self._add_account(server.port)
        self.service.dispatch("account.test", {"id": account_id})
        self.service.dispatch("sync.run", {"accountId": account_id, "folder": "INBOX"})
        self.service.dispatch("sync.run", {"accountId": account_id, "folder": "Trash"})
        return account_id

    def _message(self, account_id: str, *, uid: int = 1, folder: str = "Trash") -> dict:
        messages = self.service.dispatch(
            "message.list", {"accountId": account_id, "folder": folder}
        )["messages"]
        return next(item for item in messages if item["uid"] == uid)

    def _restore(self, message_id: str, **params) -> dict:
        params["id"] = message_id
        return self.service.dispatch("message.restore", params)

    # -- IMAP --------------------------------------------------------------

    def test_restore_to_inbox_copies_back_and_drops_local_row(self):
        with FakeImapServer(MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE]) as server:
            account_id = self._ready(server)
            message = self._message(account_id, uid=2)
            before_inbox = len(server.extra_uids.get("INBOX", []))

            result = self._restore(message["id"])
            copies = list(server.copies)
            inbox_uid_count = len(server.extra_uids.get("INBOX", []))
            trashed_uids = set(server.deleted_uids.get("Trash", set()))
            stores = list(server.stores)

        self.assertEqual(result, {"restored": True, "folder": "INBOX"})
        self.assertEqual(copies, [(2, "INBOX")])
        self.assertEqual(inbox_uid_count, before_inbox + 1, "target grew by one")
        self.assertIn(2, trashed_uids, "source uid is gone after the expunge")
        self.assertEqual([uid for uid, _flags in stores], [2])

        # Only the Trash row is gone; the INBOX row for the same UID remains.
        self.assertIsNone(self.service.store.get_message(message["id"]))
        inbox_messages = self.service.dispatch(
            "message.list", {"accountId": account_id, "folder": "INBOX"}
        )["messages"]
        self.assertEqual(len(inbox_messages), 3)
        self.assertTrue(any(item["uid"] == 2 for item in inbox_messages))

    def test_restore_to_explicit_existing_folder(self):
        with FakeImapServer(
            MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE, ARCHIVE_LINE]
        ) as server:
            account_id = self._ready(server, with_archive=True)
            message = self._message(account_id, uid=1)

            result = self._restore(message["id"], folder="Archive")
            copies = list(server.copies)
            archive_uids = list(server.extra_uids.get("Archive", []))

        self.assertEqual(result, {"restored": True, "folder": "Archive"})
        self.assertEqual(copies, [(1, "Archive")])
        self.assertEqual(len(archive_uids), 1)
        self.assertIsNone(self.service.store.get_message(message["id"]))

    def test_restore_to_missing_folder_is_bad_request(self):
        with FakeImapServer(MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE]) as server:
            account_id = self._ready(server)
            message = self._message(account_id, uid=1)

            with self.assertRaises(MailCoreError) as context:
                self._restore(message["id"], folder="Archive")
            self.assertEqual(context.exception.code, "BAD_REQUEST")
            self.assertEqual(server.copies, [])

        self.assertIsNotNone(self.service.store.get_message(message["id"]))

    def test_restore_of_non_trash_message_is_bad_request(self):
        with FakeImapServer(MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE]) as server:
            account_id = self._ready(server)
            message = self._message(account_id, uid=1, folder="INBOX")

            with self.assertRaises(MailCoreError) as context:
                self._restore(message["id"])
            self.assertEqual(context.exception.code, "BAD_REQUEST")
            self.assertEqual(server.copies, [])

        self.assertIsNotNone(self.service.store.get_message(message["id"]))

    def test_restore_copy_that_does_not_land_keeps_the_local_row(self):
        server = FakeImapServer(
            MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE], copy_noop=True
        )
        with server:
            account_id = self._ready(server)
            message = self._message(account_id, uid=2)

            with self.assertRaises(MailCoreError) as context:
                self._restore(message["id"])
            self.assertEqual(context.exception.code, "IMAP_ERROR")
            self.assertEqual(server.stores, [], "no expunge after a failed copy")

        self.assertIsNotNone(self.service.store.get_message(message["id"]))

    def test_restore_expunge_that_leaves_the_message_keeps_the_local_row(self):
        server = FakeImapServer(
            MESSAGES, folder_lines=[INBOX_LINE, TRASH_LINE], expunge_noop=True
        )
        with server:
            account_id = self._ready(server)
            message = self._message(account_id, uid=1)

            with self.assertRaises(MailCoreError) as context:
                self._restore(message["id"])
            self.assertEqual(context.exception.code, "IMAP_ERROR")

        self.assertIsNotNone(self.service.store.get_message(message["id"]))

    def test_restore_unknown_message_is_not_found(self):
        with self.assertRaises(MailCoreError) as context:
            self._restore("msg_missing")
        self.assertEqual(context.exception.code, "NOT_FOUND")

    # -- POP3 --------------------------------------------------------------

    def test_restore_on_pop3_is_bad_request(self):
        with FakePop3Server(messages=2) as server:
            account_id = self._add_account(server.port, protocol="pop3")
            self.service.dispatch("account.test", {"id": account_id})
            self.service.dispatch("sync.run", {"accountId": account_id})
            messages = self.service.dispatch(
                "message.list", {"accountId": account_id}
            )["messages"]
            message = messages[0]

            with self.assertRaises(MailCoreError) as context:
                self._restore(message["id"])
            self.assertEqual(context.exception.code, "BAD_REQUEST")
            self.assertIn("IMAP", context.exception.message)

        self.assertIsNotNone(self.service.store.get_message(message["id"]))


if __name__ == "__main__":
    unittest.main()
