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

MAX_SIGNATURE_CHARS = 20000

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
