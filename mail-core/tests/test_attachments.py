"""Attachment metadata + on-demand download tests (schema v8)."""

from __future__ import annotations

import hashlib
import unittest
from email.header import Header
from pathlib import Path

from mail_core.protocol import MailCoreError
from mail_core.service import Service
from tests.fake_servers import FakeImapServer, FakePop3Server
from tests.support import workspace_temporary_directory

PASSWORD = "s3cret-pass"
BOUNDARY = "----mail-core-attachment-boundary"


def _encoded_header(value: str) -> str:
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return Header(value, "utf-8").encode()


def _part(lines: list[str]) -> list[str]:
    return [f"--{BOUNDARY}", *lines]


def _build_message(
    parts: list[list[str]],
    *,
    subject: str = "附件测试",
    from_addr: str = "alice@example.com",
    date: str = "Mon, 02 Nov 2026 09:00:00 +0000",
    message_id: str = "<attach-root@example.com>",
) -> bytes:
    lines = [
        f"From: {from_addr}",
        "To: me@example.com",
        f"Subject: {_encoded_header(subject)}",
        f"Date: {date}",
        f"Message-ID: {message_id}",
        "MIME-Version: 1.0",
        f'Content-Type: multipart/mixed; boundary="{BOUNDARY}"',
        "",
    ]
    for part in parts:
        lines.extend(_part(part))
        lines.append("")
    lines.append(f"--{BOUNDARY}--")
    lines.append("")
    return "\r\n".join(lines).encode("utf-8")


TEXT_PART = [
    'Content-Type: text/plain; charset="utf-8"',
    "Content-Transfer-Encoding: 8bit",
    "",
    "hello body",
]
PDF_PART = [
    "Content-Type: application/pdf",
    'Content-Disposition: attachment; filename="report.pdf"',
    "Content-Transfer-Encoding: base64",
    "",
    "JVBERi0xLjQ=",  # %PDF-1.4
]
CHINESE_PART = [
    "Content-Type: application/octet-stream",
    "Content-Disposition: attachment; filename*=utf-8''%E6%8A%A5%E5%91%8A.pdf",
    "Content-Transfer-Encoding: base64",
    "",
    "YWJj",  # abc
]
INLINE_PART = [
    "Content-Type: image/png",
    "Content-ID: <logo>",
    'Content-Disposition: inline; filename="logo.png"',
    "Content-Transfer-Encoding: base64",
    "",
    "aGVsbG8=",  # hello
]
MESSAGE = _build_message([TEXT_PART, PDF_PART, CHINESE_PART, INLINE_PART])

PDF_BYTES = b"%PDF-1.4"
CHINESE_BYTES = b"abc"


def _duplicate_filename_message() -> bytes:
    first = [
        "Content-Type: text/plain",
        'Content-Disposition: attachment; filename="same.txt"',
        "Content-Transfer-Encoding: base64",
        "",
        "b25l",  # one
    ]
    second = [
        "Content-Type: text/plain",
        'Content-Disposition: attachment; filename="same.txt"',
        "Content-Transfer-Encoding: base64",
        "",
        "dHdv",  # two
    ]
    return _build_message([TEXT_PART, first, second], message_id="<dupes@example.com>")


def _traversal_message() -> bytes:
    part = [
        "Content-Type: text/plain",
        'Content-Disposition: attachment; filename="../../evil.txt"',
        "Content-Transfer-Encoding: base64",
        "",
        "ZXZpbA==",  # evil
    ]
    return _build_message([TEXT_PART, part], message_id="<evil@example.com>")


class AttachmentTests(unittest.TestCase):
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
                "displayName": "附件测试",
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

    def _list(self, **params) -> list[dict]:
        return self.service.dispatch("attachment.list", params)["attachments"]

    def _fetch(self, attachment_id: str, **params) -> dict:
        return self.service.dispatch(
            "attachment.fetch", {"id": attachment_id, **params}
        )

    # -- parsing / listing -------------------------------------------------

    def test_multipart_metadata_including_rfc2231_and_inline_filter(self):
        with FakeImapServer([MESSAGE]) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)

        attachments = self._list(accountId=account_id)
        by_name = {attachment["filename"]: attachment for attachment in attachments}
        self.assertEqual(set(by_name), {"report.pdf", "报告.pdf"})
        self.assertEqual(by_name["report.pdf"]["contentType"], "application/pdf")
        self.assertEqual(by_name["report.pdf"]["size"], len(PDF_BYTES))
        self.assertEqual(by_name["report.pdf"]["fetchedAt"], "")
        self.assertEqual(by_name["报告.pdf"]["size"], len(CHINESE_BYTES))
        self.assertEqual(
            by_name["report.pdf"]["subject"], "附件测试"
        )
        self.assertEqual(
            by_name["report.pdf"]["fromAddr"], "alice@example.com"
        )
        # Inline resources are stored but never offered as "to analyse" items.
        self.assertEqual(len(attachments), 2)
        self.assertEqual(self.service.store.count_attachments(), 3)

        # Filter by message and by "not downloaded yet".
        message_id = by_name["report.pdf"]["messageId"]
        self.assertEqual(len(self._list(messageId=message_id)), 2)
        self.assertEqual(
            len(self._list(accountId=account_id, pendingOnly=True)), 2
        )

    # -- download ----------------------------------------------------------

    def test_fetch_writes_file_and_records_sha256(self):
        with FakeImapServer([MESSAGE]) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            report = next(
                attachment
                for attachment in self._list(accountId=account_id)
                if attachment["filename"] == "report.pdf"
            )
            result = self._fetch(report["id"])

        path = Path(result["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), PDF_BYTES)
        self.assertEqual(result["size"], len(PDF_BYTES))
        self.assertEqual(result["sha256"], hashlib.sha256(PDF_BYTES).hexdigest())
        self.assertTrue(result["fetchedAt"].endswith("Z"))
        expected_dir = (
            self.service.data_dir
            / "attachments"
            / report["messageId"]
        ).resolve()
        self.assertEqual(path.resolve().parent, expected_dir)

        detail = self.service.dispatch("attachment.get", {"id": report["id"]})
        self.assertTrue(detail["downloaded"])
        self.assertEqual(detail["path"], result["path"])
        self.assertEqual(detail["sha256"], result["sha256"])
        pending = self._list(accountId=account_id, pendingOnly=True)
        self.assertNotIn(report["id"], {item["id"] for item in pending})

        # A second fetch is a no-op that returns the same file.
        again = self._fetch(report["id"])
        self.assertEqual(again["path"], result["path"])
        self.assertEqual(again["sha256"], result["sha256"])

    def test_duplicate_filenames_get_numeric_suffix(self):
        with FakeImapServer([_duplicate_filename_message()]) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            attachments = self._list(accountId=account_id)
            self.assertEqual(len(attachments), 2)
            first = self._fetch(attachments[0]["id"])
            second = self._fetch(attachments[1]["id"])

        names = {Path(first["path"]).name, Path(second["path"]).name}
        self.assertEqual(names, {"same.txt", "same-2.txt"})
        contents = {Path(first["path"]).read_bytes(), Path(second["path"]).read_bytes()}
        self.assertEqual(contents, {b"one", b"two"})

    def test_filename_cannot_escape_the_attachments_directory(self):
        with FakeImapServer([_traversal_message()]) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            attachment = self._list(accountId=account_id)[0]
            result = self._fetch(attachment["id"])

        path = Path(result["path"]).resolve()
        root = (self.service.data_dir / "attachments").resolve()
        self.assertTrue(path.is_relative_to(root))
        self.assertEqual(path.name, "evil.txt")
        self.assertEqual(path.read_bytes(), b"evil")

    def test_oversized_attachment_is_rejected_before_download(self):
        with FakeImapServer([MESSAGE]) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            report = next(
                attachment
                for attachment in self._list(accountId=account_id)
                if attachment["filename"] == "report.pdf"
            )
            with self.assertRaises(MailCoreError) as context:
                self._fetch(report["id"], maxBytes=4)

        self.assertEqual(context.exception.code, "BAD_REQUEST")
        self.assertNotIn(PASSWORD, context.exception.message)
        detail = self.service.dispatch("attachment.get", {"id": report["id"]})
        self.assertFalse(detail["downloaded"])
        self.assertFalse(Path(detail["path"]).exists() if detail["path"] else False)

    def test_pop3_accounts_cannot_fetch_attachments(self):
        with FakePop3Server(bodies=[MESSAGE]) as server:
            account_id = self._add_account(server.port, protocol="pop3")
            self._sync(account_id, folder="INBOX")
            attachments = self._list(accountId=account_id)
            self.assertEqual(len(attachments), 2)
            with self.assertRaises(MailCoreError) as context:
                self._fetch(attachments[0]["id"])

        self.assertEqual(context.exception.code, "BAD_REQUEST")
        self.assertIn("pop3", context.exception.message.lower())

    def test_attachments_cascade_with_message_and_account(self):
        with FakeImapServer([MESSAGE]) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)

        self.assertEqual(self.service.store.count_attachments(), 3)
        self.service.dispatch("sync.reset", {"accountId": account_id, "folder": "INBOX"})
        self.assertEqual(self.service.store.count_attachments(), 0)

        with FakeImapServer([MESSAGE]) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
        self.assertEqual(self.service.store.count_attachments(), 3)
        self.service.dispatch("account.remove", {"id": account_id})
        self.assertEqual(self.service.store.count_attachments(), 0)

    def test_unknown_attachment_id_is_not_found(self):
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("attachment.fetch", {"id": "att_missing"})
        self.assertEqual(context.exception.code, "NOT_FOUND")
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("attachment.get", {"id": "att_missing"})
        self.assertEqual(context.exception.code, "NOT_FOUND")


class AttachmentBackfillTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)

    def test_backfill_reparses_old_messages_once(self):
        with FakeImapServer([MESSAGE]) as server:
            account_id = self.service.dispatch(
                "account.add",
                {
                    "address": "me@example.com",
                    "imapHost": "127.0.0.1",
                    "imapPort": server.port,
                    "imapSecurity": "none",
                    "username": "me@example.com",
                    "password": PASSWORD,
                    "smtpHost": "",
                },
            )["id"]
            self.service.dispatch("sync.run", {"accountId": account_id})
            # Simulate a v7 message: no attachment rows, not marked synced.
            message_id = self.service.store._conn.execute(
                "SELECT id FROM messages"
            ).fetchone()["id"]
            self.service.store._conn.execute(
                "DELETE FROM attachments"
            )
            self.service.store._conn.execute(
                "UPDATE messages SET attachments_synced = 0"
            )
            self.service.store._conn.commit()

            first = self.service.dispatch(
                "attachment.backfill", {"accountId": account_id}
            )
            second = self.service.dispatch(
                "attachment.backfill", {"accountId": account_id}
            )

        self.assertEqual(first["scanned"], 1)
        self.assertEqual(first["updated"], 1)
        self.assertEqual(first["skipped"], 0)
        self.assertEqual(first["errors"], [])
        self.assertEqual(second["scanned"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["skipped"], 0)
        self.assertEqual(len(self.service.dispatch("attachment.list", {})["attachments"]), 2)
        self.assertEqual(
            self.service.store.count_attachments(message_id), 3
        )

    def test_backfill_skips_missing_server_message_and_keeps_going(self):
        """A vanished UID is a per-message skip, not a batch failure."""
        second = _build_message(
            [TEXT_PART, PDF_PART],
            subject="second",
            message_id="<second@example.com>",
        )
        with FakeImapServer([MESSAGE, second]) as server:
            account_id = self.service.dispatch(
                "account.add",
                {
                    "address": "me@example.com",
                    "imapHost": "127.0.0.1",
                    "imapPort": server.port,
                    "imapSecurity": "none",
                    "username": "me@example.com",
                    "password": PASSWORD,
                    "smtpHost": "",
                },
            )["id"]
            self.service.dispatch("sync.run", {"accountId": account_id})

            rows = self.service.store._conn.execute(
                "SELECT id, uid FROM messages ORDER BY uid ASC"
            ).fetchall()
            missing_id = rows[0]["id"]
            kept_id = rows[1]["id"]
            self.assertNotEqual(missing_id, kept_id)

            # Simulate a pre-v8 backlog and a UID that no longer exists.
            self.service.store._conn.execute("DELETE FROM attachments")
            self.service.store._conn.execute(
                "UPDATE messages SET attachments_synced = 0"
            )
            self.service.store._conn.commit()
            server.deleted_uids["INBOX"] = {1}

            first = self.service.dispatch(
                "attachment.backfill", {"accountId": account_id}
            )
            # The failed message stays pending for the next attempt.
            pending = self.service.store.pending_attachment_messages(account_id)
            self.assertEqual([message["id"] for message in pending], [missing_id])
            self.assertEqual(
                self.service.store.count_attachments(kept_id), 1
            )

            server.deleted_uids.clear()
            second_run = self.service.dispatch(
                "attachment.backfill", {"accountId": account_id}
            )

        self.assertEqual(first["scanned"], 2)
        self.assertEqual(first["updated"], 1)
        self.assertEqual(first["skipped"], 1)
        self.assertEqual(len(first["errors"]), 1)
        self.assertEqual(first["errors"][0]["messageId"], missing_id)
        self.assertTrue(first["errors"][0]["reason"])
        self.assertEqual(second_run["scanned"], 1)
        self.assertEqual(second_run["updated"], 1)
        self.assertEqual(second_run["skipped"], 0)
        self.assertEqual(
            self.service.store.pending_attachment_messages(account_id), []
        )

    def test_backfill_errors_are_capped_at_five(self):
        messages = [
            _build_message(
                [TEXT_PART, PDF_PART],
                subject=f"m{index}",
                message_id=f"<m{index}@example.com>",
                date=f"Mon, 0{index} Nov 2026 09:00:00 +0000",
            )
            for index in range(1, 7)
        ]
        with FakeImapServer(messages) as server:
            account_id = self.service.dispatch(
                "account.add",
                {
                    "address": "me@example.com",
                    "imapHost": "127.0.0.1",
                    "imapPort": server.port,
                    "imapSecurity": "none",
                    "username": "me@example.com",
                    "password": PASSWORD,
                    "smtpHost": "",
                },
            )["id"]
            self.service.dispatch("sync.run", {"accountId": account_id})
            self.service.store._conn.execute("DELETE FROM attachments")
            self.service.store._conn.execute(
                "UPDATE messages SET attachments_synced = 0"
            )
            self.service.store._conn.commit()
            server.deleted_uids["INBOX"] = set(range(1, 7))

            result = self.service.dispatch(
                "attachment.backfill", {"accountId": account_id}
            )

        self.assertEqual(result["scanned"], 6)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], 6)
        self.assertEqual(len(result["errors"]), 5)
        error_ids = [error["messageId"] for error in result["errors"]]
        self.assertEqual(len(set(error_ids)), 5)
        pending = {
            message["id"]
            for message in self.service.store.pending_attachment_messages(
                account_id
            )
        }
        self.assertTrue(set(error_ids) <= pending)


if __name__ == "__main__":
    unittest.main()
