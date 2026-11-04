"""IMAP modified UTF-7: round trips, the real mailbox samples, lenient decode.

The wire names below are copied verbatim from an Alibaba enterprise mailbox's
``LIST`` reply — that is what the bug report was about.
"""

from __future__ import annotations

import unittest

from mail_core import imap_client, imap_utf7

# Undecoded ``LIST`` names from the real mailbox.
SENT_WIRE = "&XfJT0ZAB-"
DELETED_WIRE = "&XfJSIJZkkK5O9g-"
DRAFTS_WIRE = "&g0l6Pw-"
JUNK_WIRE = "&V4NXPpCuTvY-"

NAMES = [
    "INBOX",
    "已发送",
    "已删除邮件",
    "Drafts & More",
    "&",
    "A&B",
    "已发送 (2026)",
    "已发送/2026",
    "Mixed 中文 & ascii",
]


class RoundTripTests(unittest.TestCase):
    def test_decode_after_encode_gives_the_original_name(self):
        for name in NAMES:
            with self.subTest(name=name):
                self.assertEqual(imap_utf7.decode(imap_utf7.encode(name)), name)

    def test_encode_after_decode_gives_the_original_wire_name(self):
        for wire in (SENT_WIRE, DELETED_WIRE, DRAFTS_WIRE, JUNK_WIRE, "INBOX"):
            with self.subTest(wire=wire):
                self.assertEqual(imap_utf7.encode(imap_utf7.decode(wire)), wire)

    def test_ascii_passes_through_and_ampersand_is_escaped(self):
        self.assertEqual(imap_utf7.encode("INBOX"), "INBOX")
        self.assertEqual(imap_utf7.encode("Drafts & More"), "Drafts &- More")
        self.assertEqual(imap_utf7.decode("&-"), "&")

    def test_empty_string(self):
        self.assertEqual(imap_utf7.encode(""), "")
        self.assertEqual(imap_utf7.decode(""), "")


class RealSampleTests(unittest.TestCase):
    def test_sent_sample_is_yi_fa_song(self):
        self.assertEqual(imap_utf7.decode(SENT_WIRE), "已发送")

    def test_deleted_sample(self):
        # This is the sample quoted in the bug report: it is the "deleted
        # messages" folder, not "sent" (that one is ``&XfJT0ZAB-``).
        self.assertEqual(imap_utf7.decode(DELETED_WIRE), "已删除邮件")

    def test_drafts_and_junk_samples(self):
        self.assertEqual(imap_utf7.decode(DRAFTS_WIRE), "草稿")
        self.assertEqual(imap_utf7.decode(JUNK_WIRE), "垃圾邮件")


class LenientDecodeTests(unittest.TestCase):
    def test_malformed_input_never_raises(self):
        for raw in ("Drafts & More", "&not base64-", "&&", "A&B", "&", "broken&"):
            with self.subTest(raw=raw):
                self.assertIsInstance(imap_utf7.decode(raw), str)

    def test_unencoded_ascii_name_survives(self):
        self.assertEqual(imap_utf7.decode("Drafts & More"), "Drafts & More")

    def test_undecodable_run_is_kept_verbatim(self):
        self.assertEqual(imap_utf7.decode("&not base64-"), "&not base64-")


class ListResponseTests(unittest.TestCase):
    def test_list_reply_is_decoded_at_the_protocol_boundary(self):
        data = [
            b'(\\HasNoChildren) "/" "&XfJT0ZAB-"',
            b'(\\HasNoChildren) "/" "&XfJSIJZkkK5O9g-"',
            b'(\\HasNoChildren) "/" "&g0l6Pw-"',
            b'(\\HasNoChildren) "/" "&V4NXPpCuTvY-"',
            b'(\\HasNoChildren) "/" INBOX',
        ]

        names = [folder["name"] for folder in imap_client.parse_list_response(data)]

        self.assertEqual(names, ["已发送", "已删除邮件", "草稿", "垃圾邮件", "INBOX"])


class MailboxTokenTests(unittest.TestCase):
    """The token put back into ``SELECT`` / ``STATUS``."""

    def test_plain_and_encoded_names_are_atoms(self):
        self.assertEqual(imap_client.encode_mailbox("INBOX"), "INBOX")
        self.assertEqual(imap_client.encode_mailbox("已发送"), SENT_WIRE)

    def test_name_with_a_space_is_a_quoted_string(self):
        self.assertEqual(
            imap_client.encode_mailbox("Sent Items"), '"Sent Items"'
        )
        self.assertEqual(
            imap_client.encode_mailbox("Drafts & More"), '"Drafts &- More"'
        )


if __name__ == "__main__":
    unittest.main()
