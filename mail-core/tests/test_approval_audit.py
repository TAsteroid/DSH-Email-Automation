"""Approval audit trail (schema v6): ``decidedVia`` / ``decidedBy``.

Schema v6 is additive: two audit columns on ``approvals`` that record how a
decision was reached (host user prompt, policy auto-approval, API) and who
made it (user, agent).  No mail is sent here.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from mail_core.protocol import MailCoreError
from mail_core.service import Service
from mail_core.store import MIGRATIONS, SCHEMA_VERSION, Store
from tests.support import workspace_temporary_directory

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

DRAFT_ROW = """
INSERT INTO drafts (
    id, account_id, to_addr, cc_addr, subject, body,
    content_hash, status, created_at, updated_at
) VALUES (
    'dft_old', 'acc_old', 'to@example.com', '', '旧草稿', '旧正文',
    'hash-old', 'approved', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
)
"""

APPROVAL_ROW = """
INSERT INTO approvals (
    id, draft_id, action, content_hash, status,
    created_at, expires_at, decided_at
) VALUES (
    'apr_old', 'dft_old', 'send', 'hash-old', 'approved',
    '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z', '2026-01-02T00:00:00Z'
)
"""


class SchemaV6MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _make_v5_database(self, path: Path) -> None:
        connection = sqlite3.connect(str(path))
        try:
            connection.executescript(MIGRATIONS[1])
            MIGRATIONS[2](connection)
            connection.executescript(MIGRATIONS[3])
            connection.executescript(MIGRATIONS[4])
            connection.executescript(MIGRATIONS[5])
            connection.execute("PRAGMA user_version=5")
            connection.execute(ACCOUNT_ROW)
            connection.execute(DRAFT_ROW)
            connection.execute(APPROVAL_ROW)
            connection.commit()
        finally:
            connection.close()

    def test_v5_database_upgrades_to_v6_and_keeps_old_decisions(self):
        path = self.tmp / "legacy-v5.db"
        self._make_v5_database(path)

        store = Store(path)
        self.addCleanup(store.close)

        self.assertGreaterEqual(SCHEMA_VERSION, 6)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)

        columns = {
            row[1] for row in store._conn.execute("PRAGMA table_info(approvals)")
        }
        self.assertTrue({"decided_via", "decided_by"} <= columns)

        # A decision made before v6 keeps its data and admits no provenance.
        approval = store.get_approval("apr_old")
        self.assertEqual(approval["status"], "approved")
        self.assertEqual(approval["decidedAt"], "2026-01-02T00:00:00Z")
        self.assertEqual(approval["decidedVia"], "")
        self.assertEqual(approval["decidedBy"], "")
        self.assertEqual(store.get_draft("dft_old")["subject"], "旧草稿")

        # The columns are usable for a decision made after the upgrade.
        draft = store.create_draft(
            account_id="acc_old", to_addr="new@example.com", content_hash="hash-new"
        )
        fresh = store.add_approval(
            draft_id=draft["id"], content_hash=draft["contentHash"]
        )
        self.assertTrue(
            store.decide_approval(
                fresh["id"], "approved", decided_via="prompt", decided_by="user"
            )
        )
        decided = store.get_approval(fresh["id"])
        self.assertEqual(decided["decidedVia"], "prompt")
        self.assertEqual(decided["decidedBy"], "user")

        # Re-running v6 (and the whole migration) is a no-op.
        self.assertEqual(store.migrate(), SCHEMA_VERSION)
        MIGRATIONS[6](store._conn)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        self.assertEqual(store.get_approval("apr_old")["decidedVia"], "")


class ApprovalAuditServiceTests(unittest.TestCase):
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

    def _pending_approval(self) -> dict:
        draft = self.service.dispatch(
            "draft.save",
            {
                "accountId": self.account_id,
                "to": "you@example.com",
                "subject": "季度报告",
                "body": "您好，附件是报告。",
            },
        )
        return self.service.dispatch("approval.request", {"draftId": draft["id"]})

    def _list(self) -> dict:
        return self.service.dispatch("approval.list", {})

    def test_pending_items_report_empty_audit_fields(self):
        approval = self._pending_approval()

        pending = self._list()["pending"]
        self.assertEqual([item["id"] for item in pending], [approval["id"]])
        self.assertEqual(pending[0]["decidedVia"], "")
        self.assertEqual(pending[0]["decidedBy"], "")

    def test_approve_stores_audit_values_and_history_reports_them(self):
        approval = self._pending_approval()

        result = self.service.dispatch(
            "approval.approve",
            {"id": approval["id"], "decidedVia": "prompt", "decidedBy": "user"},
        )
        self.assertEqual(result, {"approved": True})

        stored = self.service.store.get_approval(approval["id"])
        self.assertEqual(stored["status"], "approved")
        self.assertEqual(stored["decidedVia"], "prompt")
        self.assertEqual(stored["decidedBy"], "user")

        history = self._list()["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["decidedVia"], "prompt")
        self.assertEqual(history[0]["decidedBy"], "user")

    def test_approve_without_audit_values_stores_empty_strings(self):
        approval = self._pending_approval()
        self.service.dispatch("approval.approve", {"id": approval["id"]})

        stored = self.service.store.get_approval(approval["id"])
        self.assertEqual(stored["decidedVia"], "")
        self.assertEqual(stored["decidedBy"], "")
        self.assertEqual(self._list()["history"][0]["decidedVia"], "")

    def test_reject_stores_audit_values_symmetrically(self):
        approval = self._pending_approval()

        result = self.service.dispatch(
            "approval.reject",
            {"id": approval["id"], "decidedVia": "policy-never", "decidedBy": "agent"},
        )
        self.assertEqual(result, {"rejected": True})

        stored = self.service.store.get_approval(approval["id"])
        self.assertEqual(stored["status"], "rejected")
        self.assertEqual(stored["decidedVia"], "policy-never")
        self.assertEqual(stored["decidedBy"], "agent")
        self.assertEqual(self._list()["history"][0]["decidedVia"], "policy-never")

    def test_reject_without_audit_values_stores_empty_strings(self):
        approval = self._pending_approval()
        self.service.dispatch("approval.reject", {"id": approval["id"]})

        stored = self.service.store.get_approval(approval["id"])
        self.assertEqual(stored["decidedVia"], "")
        self.assertEqual(stored["decidedBy"], "")

    def test_every_allowed_value_is_accepted(self):
        cases = (("prompt", "user"), ("policy-never", "agent"), ("api", "user"), ("", ""))
        for decided_via, decided_by in cases:
            with self.subTest(decidedVia=decided_via, decidedBy=decided_by):
                approval = self._pending_approval()
                self.service.dispatch(
                    "approval.approve",
                    {"id": approval["id"], "decidedVia": decided_via, "decidedBy": decided_by},
                )
                stored = self.service.store.get_approval(approval["id"])
                self.assertEqual(stored["decidedVia"], decided_via)
                self.assertEqual(stored["decidedBy"], decided_by)

    def test_invalid_audit_values_are_bad_request(self):
        cases = (
            ("unknown via", {"decidedVia": "host"}),
            ("via case", {"decidedVia": "PROMPT"}),
            ("via padded", {"decidedVia": " prompt "}),
            ("via type", {"decidedVia": 1}),
            ("via null-like object", {"decidedVia": {}}),
            ("unknown by", {"decidedBy": "system"}),
            ("by case", {"decidedBy": "User"}),
            ("by padded", {"decidedBy": " agent "}),
            ("by type", {"decidedBy": True}),
        )
        for label, overrides in cases:
            with self.subTest(case=label):
                approval = self._pending_approval()
                for method in ("approval.approve", "approval.reject"):
                    with self.subTest(method=method):
                        params = {"id": approval["id"], **overrides}
                        with self.assertRaises(MailCoreError) as context:
                            self.service.dispatch(method, params)
                        self.assertEqual(context.exception.code, "BAD_REQUEST")
                        self.assertIn(
                            "decidedVia" if "decidedVia" in overrides else "decidedBy",
                            context.exception.message,
                        )

                # A rejected request must not consume the approval.
                self.assertEqual(
                    self.service.store.get_approval(approval["id"])["status"],
                    "pending",
                )
                self.service.dispatch("approval.approve", {"id": approval["id"]})
                self.assertEqual(
                    self.service.store.get_approval(approval["id"])["status"],
                    "approved",
                )

    def test_invalid_audit_value_wins_over_unknown_approval(self):
        with self.assertRaises(MailCoreError) as context:
            self.service.dispatch(
                "approval.approve", {"id": "apr_missing", "decidedVia": "host"}
            )
        self.assertEqual(context.exception.code, "BAD_REQUEST")


if __name__ == "__main__":
    unittest.main()
