"""HTML signature sanitising and preview helpers.

The signature is user-authored HTML, so it is kept (tags/styles survive) but
stripped of the constructs that would make an email dangerous: ``script`` /
``iframe`` / ``object`` / ``embed`` elements (with their content), ``on*=``
event attributes and ``javascript:`` URLs.  This is deliberately conservative:
there is no parser and no allow-list of tags.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Optional

MAX_SIGNATURE_CHARS = 20000
MAX_IMAGE_BYTES = 512 * 1024

# Magic bytes -> (mime, extension).  The extension only names the local file;
# the stored MIME type always comes from the bytes, never from the filename.
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)

_BLOCKED_BLOCK = re.compile(
    r"<\s*(script|iframe|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_BLOCKED_TAG = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed)\b[^>]*>", re.IGNORECASE
)
_EVENT_ATTRIBUTE = re.compile(
    r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE
)
_JAVASCRIPT_SCHEME = re.compile(r"javascript\s*:", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>", re.DOTALL)
_BLOCK_BREAK = re.compile(
    r"<\s*(?:br|/p|/div|/li|/tr|/h[1-6])\s*/?\s*>", re.IGNORECASE
)


def sanitize_html(value: str) -> str:
    """Remove active content from a signature, keeping the rest of the HTML."""
    text = str(value or "")
    text = _BLOCKED_BLOCK.sub("", text)
    text = _BLOCKED_TAG.sub("", text)
    text = _EVENT_ATTRIBUTE.sub("", text)
    text = _JAVASCRIPT_SCHEME.sub("", text)
    return text


def plain_text(value: str) -> str:
    """The visible text of an HTML fragment (for matching against the body)."""
    text = _BLOCK_BREAK.sub("\n", str(value or ""))
    text = _TAG.sub("", text)
    text = html_lib.unescape(text)
    return text.strip()


def plain_fragment(value: str, length: int = 40) -> str:
    """First ``length`` characters of the de-tagged, single-spaced signature."""
    text = re.sub(r"\s+", " ", plain_text(value)).strip()
    return text[:length]


def detect_image_mime(data: bytes) -> Optional[tuple[str, str]]:
    """Return ``(mime, extension)`` from magic bytes, or ``None`` if unknown."""
    payload = bytes(data or b"")
    for magic, mime, extension in _IMAGE_SIGNATURES:
        if payload.startswith(magic):
            return mime, extension
    if (
        len(payload) >= 12
        and payload[:4] == b"RIFF"
        and payload[8:12] == b"WEBP"
    ):
        return "image/webp", "webp"
    return None
