"""Approval-bound sending: ``approval.*`` plus ``action.execute``.

The happy path and the duplicate-execute guard run against a real loopback
SMTP server (``FakeSmtpServer(auth=True)``), so ``smtplib`` really performs
``AUTH`` / ``MAIL`` / ``RCPT`` / ``DATA`` and the test can count the messages
that arrived.  Transport selection is checked with mocks because a TLS
handshake is not what those branches are about.
"""

from __future__ import annotations

import email
import email.policy
import ssl
import unittest
from pathlib import Path
from unittest import mock

from mail_core import sender
from mail_core.protocol import MailCoreError
from mail_core.service import Service
from tests.fake_servers import FakeSmtpServer
from tests.support import workspace_temporary_directory

PASSWORD = "s3cret-pass"
FROM_ADDR = "me@example.com"


def _message():
    message, _message_id = sender.build_message(
        from_addr=FROM_ADDR,
        to_addr="you@example.com",
        subject="测试",
        body="正文",
    )
    return message


class ApprovalSendTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)

    # -- helpers -----------------------------------------------------------

    def _add_account(self, *, smtp_host: str = "", smtp_port: int = 465, **overrides) -> str:
        params = {
            "address": FROM_ADDR,
            "displayName": "测试邮箱",
            "imapHost": "imap.example.com",
            "username": FROM_ADDR,
            "password": PASSWORD,
            "smtpHost": smtp_host,
            "smtpPort": smtp_port,
            "smtpSecurity": "none",
        }
        params.update(overrides)
        return self.service.dispatch("account.add", params)["id"]

    def _add_local_account(self) -> str:
        return self._add_account(smtp_host="")

    def _draft(self, account_id: str, **overrides) -> dict:
        params = {
            "accountId": account_id,
            "to": "you@example.com",
            "subject": "季度报告",
            "body": "您好，\r\n附件是报告。",
        }
        params.update(overrides)
        return self.service.dispatch("draft.save", params)

    def _request(self, draft_id: str, **overrides) -> dict:
        params = {"draftId": draft_id}
        params.update(overrides)
        return self.service.dispatch("approval.request", params)

    def _approve(self, approval_id: str) -> dict:
        return self.service.dispatch("approval.approve", {"id": approval_id})

    def _execute(self, approval_id: str, **overrides) -> dict:
        params = {"approvalId": approval_id}
        params.update(overrides)
        return self.service.dispatch("action.execute", params)

    def _approved_draft(self, account_id: str, **overrides) -> tuple[dict, dict]:
        draft = self._draft(account_id, **overrides)
        approval = self._request(draft["id"])
        self._approve(approval["id"])
        return draft, approval

    # -- happy path --------------------------------------------------------

    def test_request_approve_execute_sends_exactly_one_message(self):
        with FakeSmtpServer(auth=True) as server:
            account_id = self._add_account(smtp_host=server.host, smtp_port=server.port)
            draft = self._draft(account_id)
            approval = self._request(draft["id"])

            self.assertTrue(approval["id"].startswith("apr_"))
            self.assertEqual(approval["draftId"], draft["id"])
            self.assertEqual(approval["contentHash"], draft["contentHash"])
            self.assertTrue(approval["expiresAt"].endswith("Z"))

            self.assertEqual(self._approve(approval["id"]), {"approved": True})
            result = self._execute(approval["id"])

            self.assertTrue(result["sent"])
            self.assertTrue(result["messageId"].startswith("<"))
            self.assertTrue(result["executedAt"].endswith("Z"))
            self.assertEqual(len(server.messages), 1, "exactly one message must arrive")
            self.assertEqual(server.logins, [(FROM_ADDR, PASSWORD)])
            raw = server.messages[0]

        message = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(message["Message-ID"], result["messageId"])
        self.assertEqual(message["From"], FROM_ADDR)
        self.assertEqual(message["To"], "you@example.com")
        self.assertEqual(message["Subject"], "季度报告")
        self.assertIsNone(message["Cc"])
        self.assertIn("附件是报告", message.get_body(("plain",)).get_content())

        # Draft is sent, approval stays approved, one successful action row.
        self.assertEqual(
            self.service.dispatch("draft.get", {"id": draft["id"]})["status"], "sent"
        )
        self.assertEqual(
            self.service.store.get_approval(approval["id"])["status"], "approved"
        )
        actions = self.service.store.list_mail_actions(approval["id"])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["result"], "sent")
        self.assertEqual(actions[0]["error"], "")
        self.assertEqual(actions[0]["draftId"], draft["id"])
        self.assertEqual(actions[0]["idempotencyKey"], approval["id"])
        self.assertEqual(actions[0]["executedAt"], result["executedAt"])

    def test_execute_sends_cc_and_protocol_never_leaks_password(self):
        with FakeSmtpServer(auth=True) as server:
            account_id = self._add_account(smtp_host=server.host, smtp_port=server.port)
            _draft, approval = self._approved_draft(account_id, cc="cc@example.com")
            result = self._execute(approval["id"])
            raw = server.messages[0]

        message = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(message["Cc"], "cc@example.com")
        self.assertNotIn(PASSWORD, str(result))

    def test_duplicate_execute_returns_already_executed_and_never_resends(self):
        with FakeSmtpServer(auth=True) as server:
            account_id = self._add_account(smtp_host=server.host, smtp_port=server.port)
            _draft, approval = self._approved_draft(account_id)
            self._execute(approval["id"])

            with self.assertRaises(MailCoreError) as context:
                self._execute(approval["id"])
            self.assertEqual(context.exception.code, "ALREADY_EXECUTED")

            self.assertEqual(len(server.messages), 1, "a retry must not resend")
            self.assertEqual(len(server.senders), 1)

        actions = self.service.store.list_mail_actions(approval["id"])
        self.assertEqual(len(actions), 1)

    # -- hash binding ------------------------------------------------------

    def test_editing_the_draft_after_approval_blocks_the_send(self):
        with FakeSmtpServer(auth=True) as server:
            account_id = self._add_account(smtp_host=server.host, smtp_port=server.port)
            draft, approval = self._approved_draft(account_id)
            self._draft(account_id, draftId=draft["id"], body="换了一版正文")

            with self.assertRaises(MailCoreError) as context:
                self._execute(approval["id"])
            self.assertEqual(context.exception.code, "HASH_MISMATCH")
            self.assertEqual(len(server.messages), 0)

        self.assertEqual(self.service.store.get_approval(approval["id"])["status"], "approved")
        self.assertEqual(
            self.service.dispatch("draft.get", {"id": draft["id"]})["status"], "draft"
        )
        self.assertEqual(self.service.store.list_mail_actions(), [])

    # -- approval state machine -------------------------------------------

    def test_execute_without_approval_is_rejected(self):
        account_id = self._add_local_account()
        draft = self._draft(account_id)
        approval = self._request(draft["id"])

        with self.assertRaises(MailCoreError) as context:
            self._execute(approval["id"])
        self.assertEqual(context.exception.code, "APPROVAL_NOT_APPROVED")

    def test_execute_unknown_approval_is_not_found(self):
        with self.assertRaises(MailCoreError) as context:
            self._execute("apr_missing")
        self.assertEqual(context.exception.code, "APPROVAL_NOT_FOUND")

    def test_reject_marks_the_draft_and_blocks_execute(self):
        account_id = self._add_local_account()
        draft = self._draft(account_id)
        approval = self._request(draft["id"])

        self.assertEqual(
            self.service.dispatch("approval.reject", {"id": approval["id"]}),
            {"rejected": True},
        )
        self.assertEqual(
            self.service.store.get_approval(approval["id"])["status"], "rejected"
        )
        self.assertEqual(
            self.service.dispatch("draft.get", {"id": draft["id"]})["status"],
            "rejected",
        )

        with self.assertRaises(MailCoreError) as context:
            self._execute(approval["id"])
        self.assertEqual(context.exception.code, "APPROVAL_NOT_APPROVED")

    def test_deciding_twice_is_bad_request_with_the_current_status(self):
        account_id = self._add_local_account()
        draft = self._draft(account_id)
        approval = self._request(draft["id"])
        self._approve(approval["id"])

        with self.assertRaises(MailCoreError) as context:
            self._approve(approval["id"])
        self.assertEqual(context.exception.code, "BAD_REQUEST")
        self.assertIn("approved", context.exception.message)

        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch("approval.reject", {"id": approval["id"]})
        self.assertEqual(context.exception.code, "BAD_REQUEST")

    def test_ttl_zero_approval_is_expired(self):
        account_id = self._add_local_account()
        draft = self._draft(account_id)
        approval = self._request(draft["id"], ttlMinutes=0)

        with self.assertRaises(MailCoreError) as context:
            self._approve(approval["id"])
        self.assertEqual(context.exception.code, "APPROVAL_EXPIRED")

        # Expiry is a decision-time check: the row is still pending.
        pending = self.service.dispatch("approval.list", {})["pending"]
        self.assertEqual([item["id"] for item in pending], [approval["id"]])

    def test_expired_approved_approval_cannot_execute(self):
        with FakeSmtpServer(auth=True) as server:
            account_id = self._add_account(smtp_host=server.host, smtp_port=server.port)
            _draft, approval = self._approved_draft(account_id)
            with self.service.store._conn:
                self.service.store._conn.execute(
                    "UPDATE approvals SET expires_at = '2020-01-01T00:00:00Z' WHERE id = ?",
                    (approval["id"],),
                )

            with self.assertRaises(MailCoreError) as context:
                self._execute(approval["id"])
            self.assertEqual(context.exception.code, "APPROVAL_EXPIRED")
            self.assertEqual(len(server.messages), 0)

    def test_approval_list_shape_and_order(self):
        account_id = self._add_local_account()
        drafts = [self._draft(account_id, subject=f"草稿{i}") for i in range(3)]
        approvals = [self._request(draft["id"]) for draft in drafts]
        with self.service.store._conn:
            for offset, approval in enumerate(approvals, start=1):
                self.service.store._conn.execute(
                    "UPDATE approvals SET created_at = ? WHERE id = ?",
                    (f"2026-0{offset}-01T00:00:00Z", approval["id"]),
                )

        self._approve(approvals[0]["id"])
        self.service.dispatch("approval.reject", {"id": approvals[1]["id"]})

        listing = self.service.dispatch("approval.list", {})
        self.assertEqual([item["id"] for item in listing["pending"]], [approvals[2]["id"]])
        self.assertEqual(
            [item["id"] for item in listing["history"]],
            [approvals[1]["id"], approvals[0]["id"]],
        )
        for item in listing["history"]:
            self.assertEqual(
                set(item),
                {
                    "id",
                    "draftId",
                    "action",
                    "contentHash",
                    "status",
                    "createdAt",
                    "expiresAt",
                    "decidedAt",
                },
            )
            self.assertTrue(item["decidedAt"].endswith("Z"))
        self.assertEqual(listing["history"][0]["status"], "rejected")

    def test_removing_a_draft_removes_its_pending_approvals(self):
        account_id = self._add_local_account()
        draft = self._draft(account_id)
        approval = self._request(draft["id"])

        self.service.dispatch("draft.remove", {"id": draft["id"]})
        self.assertEqual(self.service.dispatch("approval.list", {})["pending"], [])

        with self.assertRaises(MailCoreError) as context:
            self._execute(approval["id"])
        self.assertEqual(context.exception.code, "APPROVAL_NOT_FOUND")

    # -- send failures -----------------------------------------------------

    def test_account_without_smtp_host_fails_with_smtp_error_and_logs_it(self):
        account_id = self._add_local_account()
        draft, approval = self._approved_draft(account_id)

        with self.assertRaises(MailCoreError) as context:
            self._execute(approval["id"])
        self.assertEqual(context.exception.code, "SMTP_ERROR")
        self.assertIn("not configured", context.exception.message)
        self.assertNotIn(PASSWORD, context.exception.message)

        actions = self.service.store.list_mail_actions(approval["id"])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["result"], "failed")
        self.assertIn("not configured", actions[0]["error"])
        self.assertNotIn(PASSWORD, actions[0]["error"])
        # A failed send leaves the draft status untouched (still approved).
        self.assertEqual(
            self.service.dispatch("draft.get", {"id": draft["id"]})["status"],
            "approved",
        )

    def test_smtp_server_failure_returns_smtp_error_without_the_password(self):
        with FakeSmtpServer(reject=True, auth=True) as server:
            account_id = self._add_account(smtp_host=server.host, smtp_port=server.port)
            _draft, approval = self._approved_draft(account_id)

            with self.assertRaises(MailCoreError) as context:
                self._execute(approval["id"])
            self.assertEqual(context.exception.code, "SMTP_ERROR")
            self.assertTrue(context.exception.message)
            self.assertNotIn(PASSWORD, context.exception.message)
            self.assertEqual(server.messages, [])

        actions = self.service.store.list_mail_actions(approval["id"])
        self.assertEqual([action["result"] for action in actions], ["failed"])
        self.assertNotIn(PASSWORD, actions[0]["error"])

    # -- status counters ---------------------------------------------------

    def test_sync_status_counts_drafts_and_pending_approvals(self):
        account_id = self._add_local_account()
        draft = self._draft(account_id)
        approval = self._request(draft["id"])

        counters = self.service.dispatch("sync.status", {})["counters"]
        self.assertEqual(counters["drafts"], 1)
        self.assertEqual(counters["pendingApprovals"], 1)

        self._approve(approval["id"])
        counters = self.service.dispatch("sync.status", {})["counters"]
        self.assertEqual(counters["drafts"], 1)
        self.assertEqual(counters["pendingApprovals"], 0)


class SenderTransportTests(unittest.TestCase):
    """``sender.send_message`` picks the transport implied by ``security``."""

    def test_ssl_uses_smtp_ssl_with_default_context_and_20s_timeout(self):
        with mock.patch.object(sender.smtplib, "SMTP_SSL") as ssl_class, mock.patch.object(
            sender.smtplib, "SMTP"
        ) as plain_class:
            client = ssl_class.return_value
            sender.send_message("smtp.example.com", 465, "ssl", "user", "pass", _message())

        self.assertFalse(plain_class.called)
        args, kwargs = ssl_class.call_args
        self.assertEqual(args, ("smtp.example.com", 465))
        self.assertEqual(kwargs["timeout"], 20.0)
        self.assertIsInstance(kwargs["context"], ssl.SSLContext)
        client.login.assert_called_once_with("user", "pass")
        client.send_message.assert_called_once()
        client.quit.assert_called_once()

    def test_starttls_upgrades_plain_connection(self):
        with mock.patch.object(sender.smtplib, "SMTP") as plain_class:
            client = plain_class.return_value
            sender.send_message("smtp.example.com", 587, "starttls", "user", "pass", _message())

        args, _kwargs = plain_class.call_args
        self.assertEqual(args, ("smtp.example.com", 587))
        client.ehlo.assert_called()
        client.starttls.assert_called_once()
        _args, kwargs = client.starttls.call_args
        self.assertIsInstance(kwargs["context"], ssl.SSLContext)
        client.login.assert_called_once_with("user", "pass")
        client.send_message.assert_called_once()

    def test_none_uses_plain_smtp_without_starttls(self):
        with mock.patch.object(sender.smtplib, "SMTP") as plain_class:
            client = plain_class.return_value
            sender.send_message("smtp.example.com", 25, "none", "user", "pass", _message())

        plain_class.assert_called_once()
        client.starttls.assert_not_called()
        client.login.assert_called_once_with("user", "pass")

    def test_unknown_security_mode_is_a_send_error(self):
        with self.assertRaises(sender.SendError):
            sender.send_message("smtp.example.com", 25, "tls", "user", "pass", _message())

    def test_missing_host_is_a_send_error(self):
        with self.assertRaises(sender.SendError):
            sender.send_message("", 25, "none", "user", "pass", _message())


if __name__ == "__main__":
    unittest.main()
