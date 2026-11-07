"""Attachment metadata + on-demand download tests (schema v8)."""

from __future__ import annotations

import hashlib
import io
import re
import unittest
import zipfile
from email.header import Header
from pathlib import Path

from mail_core.protocol import MailCoreError
from mail_core.service import Service
from mail_core.store import MIGRATIONS, SCHEMA_VERSION
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


_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _minimal_xlsx() -> bytes:
    """A tiny workbook: one inline string, one shared string, two numbers."""
    shared = f'<sst xmlns="{_XLSX_NS}"><si><t>Score</t></si></sst>'.encode(
        "utf-8"
    )
    sheet = (
        f'<worksheet xmlns="{_XLSX_NS}"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>姓名</t></is></c>'
        '<c r="B1" t="s"><v>0</v></c></row>'
        '<row r="2"><c r="A2"><v>1</v></c><c r="B2"><v>90</v></c></row>'
        "</sheetData></worksheet>"
    ).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def _pdf_with_image() -> bytes:
    """A minimal PDF with one text stream and one binary image stream.

    The image stream deliberately contains a ``(...) Tj`` fragment plus
    high-bit bytes: a parser that ignores the object dictionary would extract
    it as text, which is exactly the production bug this guards against.
    """
    text_stream = (
        b"BT /F1 12 Tf "
        b"(Limes Latin America) Tj T* "
        b"(Confidential Information Memorandum) Tj T* "
        b"(Bad\x01\x02\x03\x04\x05 line) Tj ET"
    )
    image_stream = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
        b"(GARBAGE\x80\x81\x82\x83) Tj"
        b"\xff\xd9"
    )
    parts = [
        b"%PDF-1.4\n",
        b"1 0 obj\n<< /Length "
        + str(len(text_stream)).encode("ascii")
        + b" >>\nstream\n"
        + text_stream
        + b"\nendstream\nendobj\n",
        b"2 0 obj\n<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
        b"/Length "
        + str(len(image_stream)).encode("ascii")
        + b" >>\nstream\n"
        + image_stream
        + b"\nendstream\nendobj\n",
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n",
    ]
    return b"".join(parts)


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
        self.assertIn(
            report["id"],
            {item["id"] for item in pending},
            "downloading must leave the attachment in the pending list",
        )

        # Only an explicit dismiss closes the follow-up item.
        self.service.dispatch("attachment.dismiss", {"id": report["id"]})
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

    def _synced_report(self) -> tuple[str, dict]:
        with FakeImapServer([MESSAGE]) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
        attachments = self._list(accountId=account_id)
        return account_id, next(
            item for item in attachments if item["filename"] == "report.pdf"
        )

    def test_dismiss_drops_from_pending_but_stays_in_the_full_list(self):
        account_id, report = self._synced_report()
        self.assertEqual(report["dismissedAt"], "")

        result = self.service.dispatch(
            "attachment.dismiss", {"id": report["id"]}
        )
        self.assertEqual(result["dismissed"], True)
        self.assertTrue(result["dismissedAt"].endswith("Z"))

        pending = self._list(accountId=account_id, pendingOnly=True)
        self.assertEqual([item["filename"] for item in pending], ["报告.pdf"])

        full = {
            item["filename"]: item for item in self._list(accountId=account_id)
        }
        self.assertEqual(full["report.pdf"]["dismissedAt"], result["dismissedAt"])
        self.assertEqual(full["报告.pdf"]["dismissedAt"], "")

        # v11 is a guarded add-column migration, so re-running it is a no-op.
        self.assertGreaterEqual(SCHEMA_VERSION, 11)
        MIGRATIONS[11](self.service.store._conn)
        self.assertEqual(self.service.store.schema_version, SCHEMA_VERSION)

    def test_pending_only_ignores_attachments_outside_inbox(self):
        with FakeImapServer([MESSAGE]) as server:
            account_id = self._add_account(server.port)
            self._sync(account_id)
            self._sync(account_id, folder="Sent")

        inbox_ids = {
            message["id"]
            for message in self.service.dispatch(
                "message.list", {"accountId": account_id, "folder": "INBOX"}
            )["messages"]
        }
        self.assertEqual(len(inbox_ids), 1)

        pending = self._list(accountId=account_id, pendingOnly=True)
        self.assertEqual(len(pending), 2)
        self.assertTrue({item["messageId"] for item in pending} <= inbox_ids)

        # Without pendingOnly both copies (INBOX + Sent) are still listed.
        self.assertEqual(len(self._list(accountId=account_id)), 4)

    # -- text extraction (standard library only) ---------------------------

    def _downloaded_attachment(
        self,
        filename: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        downloaded: bool = True,
        root: Path | None = None,
    ) -> str:
        store = self.service.store
        if store.get_account("acc_text") is None:
            store.add_account(
                account_id="acc_text",
                address="me@example.com",
                imap_host="imap.example.com",
                imap_port=993,
                imap_security="ssl",
                username="me@example.com",
            )
        uid = store.count_messages() + 1
        store.insert_message(
            account_id="acc_text",
            folder="INBOX",
            uidvalidity=1,
            uid=uid,
            message_id=f"<text{uid}@example.com>",
            subject="附件文本",
            attachments=[
                {
                    "filename": filename,
                    "contentType": content_type,
                    "size": len(data),
                    "partId": "2",
                }
            ],
        )
        message = store._conn.execute(
            "SELECT id FROM messages ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        attachment = next(
            item
            for item in store.list_attachments(
                message_id=message["id"], include_inline=True
            )
            if item["filename"] == filename
        )
        if downloaded:
            path = (root or Path(self._tmp.name)) / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            store.set_attachment_download(
                attachment["id"],
                local_path=str(path),
                sha256=hashlib.sha256(data).hexdigest(),
                fetched_at="2026-11-05T01:00:00Z",
            )
        return attachment["id"]

    def _text(self, attachment_id: str, **params) -> dict:
        return self.service.dispatch(
            "attachment.text", {"id": attachment_id, **params}
        )

    def test_text_reads_utf8_and_gbk_and_truncates(self):
        attachment_id = self._downloaded_attachment(
            "notes.txt",
            "第一行\n第二行 hello".encode("utf-8-sig"),
            content_type="text/plain",
        )
        result = self._text(attachment_id)
        self.assertEqual(result["kind"], "text")
        self.assertEqual(result["filename"], "notes.txt")
        self.assertEqual(result["text"], "第一行\n第二行 hello")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["chars"], len(result["text"]))

        truncated = self._text(attachment_id, maxChars=4)
        self.assertEqual(truncated["text"], "第一行\n")
        self.assertEqual(truncated["chars"], 4)
        self.assertTrue(truncated["truncated"])

        gbk_id = self._downloaded_attachment(
            "legacy.log", "中文GBK内容".encode("gbk"), content_type="text/plain"
        )
        self.assertEqual(self._text(gbk_id)["text"], "中文GBK内容")

    def test_text_extracts_xlsx_as_tsv(self):
        attachment_id = self._downloaded_attachment(
            "表格.xlsx",
            _minimal_xlsx(),
            content_type=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
        )
        result = self._text(attachment_id)
        self.assertEqual(result["kind"], "xlsx")
        self.assertEqual(result["text"], "姓名\tScore\n1\t90")
        self.assertFalse(result["truncated"])

    def test_text_requires_a_downloaded_attachment(self):
        attachment_id = self._downloaded_attachment(
            "未下载.txt", b"hello", content_type="text/plain", downloaded=False
        )
        with self.assertRaises(MailCoreError) as context:
            self._text(attachment_id)
        self.assertEqual(context.exception.code, "BAD_REQUEST")
        self.assertIn("download", context.exception.message)

    def test_text_rejects_unsupported_type(self):
        attachment_id = self._downloaded_attachment(
            "photo.png", b"\x89PNG\r\n", content_type="image/png"
        )
        with self.assertRaises(MailCoreError) as context:
            self._text(attachment_id)
        self.assertEqual(context.exception.code, "BAD_REQUEST")
        self.assertIn("supported", context.exception.message)
        self.assertIn(".pdf", context.exception.message)

    def test_text_pdf_skips_image_streams_and_dirty_lines(self):
        attachment_id = self._downloaded_attachment(
            "memorandum.pdf", _pdf_with_image(), content_type="application/pdf"
        )
        result = self._text(attachment_id)

        self.assertEqual(result["kind"], "pdf")
        self.assertIn("Limes Latin America", result["text"])
        self.assertIn("Confidential Information Memorandum", result["text"])
        # The image stream is binary: neither its fake text operator nor the
        # high-bit bytes may leak through.
        self.assertNotIn("GARBAGE", result["text"])
        self.assertIsNone(re.search(r"[\x80-\xff]{3,}", result["text"]))
        # The control-char-heavy text line is dropped by the 30% ratio rule.
        self.assertNotIn("Bad", result["text"])

    def test_delete_local_removes_file_and_is_idempotent(self):
        data = b"\x89PNG local file payload"
        attachments_root = self.service.data_dir / "attachments"
        attachment_id = self._downloaded_attachment(
            "cached.png",
            data,
            content_type="image/png",
            root=attachments_root,
        )
        stored = self.service.store.get_attachment(attachment_id)
        path = Path(stored["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(stored["fetchedAt"], "2026-11-05T01:00:00Z")

        result = self.service.dispatch(
            "attachment.deleteLocal", {"id": attachment_id}
        )
        self.assertEqual(
            result,
            {"deleted": True, "id": attachment_id, "freedBytes": len(data)},
        )
        self.assertFalse(path.exists())
        cleared = self.service.store.get_attachment(attachment_id)
        self.assertEqual(cleared["fetchedAt"], "")
        self.assertEqual(cleared["path"], "")
        self.assertEqual(cleared["sha256"], "")
        self.assertFalse(cleared["downloaded"])
        # The metadata row and its message association are kept.
        self.assertEqual(cleared["messageId"], stored["messageId"])

        again = self.service.dispatch(
            "attachment.deleteLocal", {"id": attachment_id}
        )
        self.assertEqual(
            again,
            {"deleted": True, "id": attachment_id, "freedBytes": 0},
        )

        # A path outside <data-dir>/attachments must never be deleted.
        outside_id = self._downloaded_attachment(
            "outside.txt", b"outside", root=Path(self._tmp.name)
        )
        outside_path = Path(
            self.service.store.get_attachment(outside_id)["path"]
        )
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "attachment.deleteLocal", {"id": outside_id}
            )
        self.assertEqual(context.exception.code, "BAD_REQUEST")
        self.assertTrue(outside_path.is_file())

    def test_delete_local_unknown_attachment_is_not_found(self):
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "attachment.deleteLocal", {"id": "att_missing"}
            )
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

if __name__ == "__main__":
    unittest.main()
