"""Rich-text drafts (schema v15): bodyHtml storage, hash and send MIME."""

from __future__ import annotations

import unittest
from pathlib import Path

from mail_core import sender
from mail_core.service import Service
from tests.support import workspace_temporary_directory

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class DraftHtmlTests(unittest.TestCase):
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

    def _save(self, *, body: str = "纯文本", body_html: str = "", draft_id=None):
        params = {
            "accountId": self.account_id,
            "to": "you@example.com",
            "subject": "富文本",
            "body": body,
            "bodyHtml": body_html,
        }
        if draft_id is not None:
            params["draftId"] = draft_id
        return self.service.dispatch("draft.save", params)

    def test_draft_roundtrips_body_html_and_hash_covers_it(self):
        saved = self._save(body_html="<p>富文本</p>")
        draft = self.service.dispatch("draft.get", {"id": saved["id"]})
        self.assertEqual(draft["body"], "纯文本")
        self.assertEqual(draft["bodyHtml"], "<p>富文本</p>")

        listed = self.service.dispatch("draft.list", {})["drafts"]
        self.assertEqual(listed[0]["bodyHtml"], "<p>富文本</p>")
        self.assertGreaterEqual(self.service.store.schema_version, 15)

        # Same text but different HTML -> different approval hash.
        other = self._save(body_html="<p>另一版</p>")
        self.assertNotEqual(saved["contentHash"], other["contentHash"])
        same = self._save(body_html="<p>富文本</p>")
        self.assertEqual(saved["contentHash"], same["contentHash"])

        # An update also stores the new HTML.
        updated = self._save(body_html="<p>改过</p>", draft_id=saved["id"])
        draft = self.service.dispatch("draft.get", {"id": updated["id"]})
        self.assertEqual(draft["bodyHtml"], "<p>改过</p>")

    def test_send_uses_body_html_and_does_not_append_signature(self):
        message, _ = sender.build_message(
            from_addr="me@example.com",
            to_addr="you@example.com",
            subject="富文本",
            body="纯文本",
            body_html="<p>富文本正文</p>",
            signature_html="<div>签名</div>",
        )
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        text_part = message.get_body(preferencelist=("plain",)).get_content()
        html_part = message.get_body(preferencelist=("html",)).get_content()
        self.assertEqual(text_part.strip(), "纯文本")
        self.assertEqual(html_part.strip(), "<p>富文本正文</p>")
        self.assertNotIn("签名", html_part)

        # Empty plain text falls back to the de-tagged HTML.
        fallback, _ = sender.build_message(
            from_addr="me@example.com",
            to_addr="you@example.com",
            subject="富文本",
            body="",
            body_html="<p>富文本正文</p>",
        )
        text_part = fallback.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("富文本正文", text_part)

        # Regression: no bodyHtml keeps the legacy signature behaviour.
        legacy, _ = sender.build_message(
            from_addr="me@example.com",
            to_addr="you@example.com",
            subject="旧草稿",
            body="Hello",
            body_html="",
            signature_html="<div>签名</div>",
        )
        self.assertEqual(legacy.get_content_type(), "multipart/alternative")
        legacy_html = legacy.get_body(preferencelist=("html",)).get_content()
        self.assertTrue(legacy_html.rstrip().endswith("<div>签名</div>"))

    def test_body_html_cid_attaches_related_image(self):
        html = '<p>富文本</p><p><img src="cid:sig_x"></p>'
        images = [
            {
                "cid": "sig_x",
                "mime": "image/png",
                "filename": "logo.png",
                "data": PNG,
            }
        ]
        message, _ = sender.build_message(
            from_addr="me@example.com",
            to_addr="you@example.com",
            subject="富文本图片",
            body="富文本",
            body_html=html,
            signature_images=images,
        )
        self.assertEqual(message.get_content_type(), "multipart/related")
        parts = message.get_payload()
        self.assertEqual(parts[0].get_content_type(), "multipart/alternative")
        self.assertIn("cid:sig_x", parts[0].get_payload()[1].get_content())
        self.assertEqual(parts[1].get_content_type(), "image/png")
        self.assertIn("sig_x", str(parts[1]["Content-ID"]))
        self.assertEqual(parts[1].get_payload(decode=True), PNG)


if __name__ == "__main__":
    unittest.main()
