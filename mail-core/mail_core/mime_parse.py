"""Turn raw RFC 822 bytes into the message fields the IPC protocol exposes.

Only the standard library is used.  Everything here is best-effort: a broken
message must still be storable, so header/charset problems degrade to
``errors="replace"`` instead of raising.

Body handling follows the size rule from the sync spec: a message larger than
1 MB keeps at most the first 512 KB of its body text/html, and its snippet
carries :data:`TRUNCATION_MARKER`.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import hashlib
import re
from datetime import timezone
from html.parser import HTMLParser
from typing import Any

MAX_RAW_BYTES = 1024 * 1024
MAX_BODY_BYTES = 512 * 1024
TRUNCATION_MARKER = " [truncated]"
SNIPPET_LENGTH = 200

# Block-level tags get a line break in the text rendering of an HTML body.
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})
# Whitespace that RFC 5322 allows in a folded header.
_HEADER_WS = re.compile(r"[\t\r\n]+")


def _decode_bytes(raw: bytes, charset: str | None) -> str:
    """Decode with ``replace`` and fall back to UTF-8/Latin-1 on bad charset."""
    if charset:
        try:
            return raw.decode(charset, "replace")
        except (LookupError, ValueError, TypeError):
            pass
    try:
        return raw.decode("utf-8", "replace")
    except (LookupError, ValueError, TypeError):  # pragma: no cover - defensive
        return raw.decode("latin-1", "replace")


def decode_header_value(value: Any) -> str:
    """Decode one RFC 2047 header value and collapse folded whitespace.

    ``email.header.make_header`` is tried first (it is the canonical decoder);
    if it trips over an unknown charset or broken bytes, the parts are decoded
    manually with ``errors="replace"``.
    """
    if value is None:
        return ""
    try:
        if isinstance(value, bytes):
            parts = email.header.decode_header(value.decode("utf-8", "replace"))
        else:
            parts = email.header.decode_header(str(value))
    except Exception:
        return _HEADER_WS.sub(" ", str(value)).strip()

    try:
        text = str(email.header.make_header(parts))
    except Exception:
        chunks: list[str] = []
        for part, charset in parts:
            if isinstance(part, str):
                chunks.append(part)
            else:
                chunks.append(_decode_bytes(part, charset))
        text = "".join(chunks)
    return _HEADER_WS.sub(" ", text).strip()


def decode_date(value: Any) -> str:
    """Convert a ``Date:`` header to ``YYYY-MM-DDTHH:MM:SSZ`` (UTC).

    Returns ``""`` when the header is missing or unparsable.  A date without a
    timezone is treated as UTC.
    """
    text = decode_header_value(value)
    if not text:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return ""
    if parsed is None:
        return ""
    try:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, ValueError):
        return ""


def _decode_part(part: email.message.Message) -> str:
    """Decode a text part, preferring its declared charset."""
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        if isinstance(raw, str):
            return raw
        return ""
    return _decode_bytes(payload, part.get_content_charset())


class _HtmlTextExtractor(HTMLParser):
    """Rough HTML-to-text: keep text, break on block elements, drop scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    """Strip tags from an HTML body; collapses whitespace within each line."""
    if not html:
        return ""
    parser = _HtmlTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Malformed HTML: keep whatever was collected before the failure.
        pass
    lines = [" ".join(line.split()) for line in parser.get_text().splitlines()]
    return "\n".join(line for line in lines if line)


def extract_bodies(message: email.message.Message) -> tuple[str, str]:
    """Return ``(text, html)``; text falls back to a tag-stripped HTML body."""
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            decoded = _decode_part(part)
            if decoded.strip():
                plain.append(decoded)
        elif content_type == "text/html":
            decoded = _decode_part(part)
            if decoded.strip():
                html.append(decoded)

    html_text = "\n".join(html)
    text = "\n".join(plain)
    if not text.strip() and html_text.strip():
        text = html_to_text(html_text)
    return text, html_text


def make_snippet(text: str) -> str:
    """First 200 characters of the body, squeezed onto one line."""
    return " ".join((text or "").split())[:SNIPPET_LENGTH]


# -- attachments -----------------------------------------------------------

# Parts that render the message body.  They are only treated as attachments
# when they carry a filename or an explicit attachment disposition (a mailed
# `.txt` file is an attachment, the readable body is not).
_BODY_TYPES = frozenset({"text/plain", "text/html"})


def _iter_parts(message, prefix: str = ""):
    """Yield ``(imap_section, part)`` pairs matching IMAP body sections.

    ``multipart/mixed`` children are ``1``, ``2`` …; nested containers extend
    the path (``1.1``, ``1.2`` …).  A single-part message yields ``1``.  A
    ``message/rfc822`` part is itself an attachment, not a container to walk
    into.
    """
    if message.is_multipart() and message.get_content_type() != "message/rfc822":
        payload = message.get_payload()
        if not isinstance(payload, list):
            yield prefix or "1", message
            return
        for index, child in enumerate(payload, start=1):
            part_id = f"{prefix}.{index}" if prefix else str(index)
            yield from _iter_parts(child, part_id)
    else:
        yield prefix or "1", message


def _disposition(part) -> str:
    raw = part.get("Content-Disposition") or ""
    return str(raw).split(";", 1)[0].strip().lower()


def _attachment_filename(part) -> str:
    """Decoded filename (RFC 2231 handled by ``get_filename``, 2047 on top)."""
    try:
        name = part.get_filename()
    except Exception:
        name = None
    if not name:
        return ""
    text = str(name).strip()
    if "=?" in text:
        text = decode_header_value(text)
    return text


def _part_size(part) -> int:
    """Byte length of the transfer-decoded part content."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if isinstance(payload, (bytes, bytearray)):
        return len(payload)
    raw = part.get_payload()
    if isinstance(raw, list):
        total = 0
        for item in raw:
            encoder = getattr(item, "as_bytes", None)
            if callable(encoder):
                total += len(encoder())
        return total
    if isinstance(raw, (bytes, bytearray)):
        return len(raw)
    if isinstance(raw, str):
        return len(raw.encode("utf-8", "replace"))
    return 0


def extract_attachments(message: email.message.Message) -> list[dict]:
    """List the attachment parts of a parsed message.

    Detection follows the product rule: a part counts when it has a filename
    or an explicit ``attachment`` disposition, body text parts do not count,
    and inline resources (Content-ID / ``inline`` disposition) are flagged
    ``inline`` so the list RPC can keep them out of the "to analyse" queue.
    """
    try:
        parts = list(_iter_parts(message))
    except Exception:  # a broken MIME tree must not fail the whole sync
        return []

    attachments: list[dict] = []
    for part_id, part in parts:
        try:
            content_type = (part.get_content_type() or "").lower()
            filename = _attachment_filename(part)
            disposition = _disposition(part)
            has_content_id = bool(part.get("Content-ID"))
            is_body = (
                content_type in _BODY_TYPES
                and not filename
                and disposition != "attachment"
            )
            if is_body:
                continue
            if not filename and disposition != "attachment":
                continue
            inline = disposition == "inline" or (
                has_content_id and disposition != "attachment"
            )
            attachments.append(
                {
                    "filename": filename,
                    "contentType": content_type or "application/octet-stream",
                    "size": _part_size(part),
                    "partId": part_id,
                    "inline": inline,
                }
            )
        except Exception:  # one malformed part must not hide the others
            continue
    return attachments


def decode_part_bytes(mime_bytes: bytes, body_bytes: bytes) -> bytes:
    """Decode a fetched IMAP body part using its MIME headers.

    ``BODY[<part>.MIME]`` gives the headers (Content-Type /
    Content-Transfer-Encoding) and ``BODY[<part>]`` gives the still-encoded
    body; reassembling them and calling ``get_payload(decode=True)`` is the
    standard-library way to undo base64 / quoted-printable.  When the server
    declines the MIME fetch, the body is returned untouched.
    """
    if not mime_bytes:
        return body_bytes
    try:
        headers = mime_bytes.rstrip(b"\r\n")
        part = email.message_from_bytes(headers + b"\r\n\r\n" + body_bytes)
        decoded = part.get_payload(decode=True)
        if isinstance(decoded, (bytes, bytearray)):
            return bytes(decoded)
    except Exception:
        pass
    return body_bytes


def _truncate_utf8(text: str) -> tuple[str, bool]:
    """Keep at most :data:`MAX_BODY_BYTES` UTF-8 bytes."""
    if not text:
        return "", False
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= MAX_BODY_BYTES:
        return text, False
    return encoded[:MAX_BODY_BYTES].decode("utf-8", "ignore"), True


def _empty_message(size: int) -> dict:
    return {
        "subject": "",
        "fromAddr": "",
        "toAddr": "",
        "ccAddr": "",
        "messageId": "",
        "inReplyTo": "",
        "references": "",
        "date": "",
        "snippet": "",
        "text": "",
        "html": "",
        "size": int(size),
        "attachments": [],
    }


def _fallback_message_id(payload: bytes) -> str:
    """Content-derived Message-ID for mail whose header is missing/empty.

    Hashing the raw message keeps the value stable across repeated syncs of
    the same bytes; the sync layer passes a UID-derived id when it has one
    (that stays stable even if the server rewrites the message).
    """
    return f"<{hashlib.sha1(payload).hexdigest()}@mail-core>"


def parse_message(
    raw: bytes,
    size: int | None = None,
    *,
    fallback_message_id: str = "",
) -> dict:
    """Parse raw RFC 822 bytes into the columns of the ``messages`` table.

    The returned keys use the protocol spelling; the sync layer maps them to
    the snake_case store parameters.  ``inReplyTo`` / ``references`` are the
    decoded header values (thread assembly parses them with
    :func:`mail_core.threads.reference_chain`).
    """
    payload = raw or b""
    total_size = int(size) if size is not None and int(size) > 0 else len(payload)
    try:
        message = email.message_from_bytes(payload)
    except Exception:  # pragma: no cover - parser is normally very tolerant
        return _empty_message(total_size)

    text, html_text = "", ""
    try:
        text, html_text = extract_bodies(message)
    except Exception:  # a broken MIME tree must not fail the whole sync
        pass
    try:
        attachments = extract_attachments(message)
    except Exception:
        attachments = []
    truncated = False
    if total_size > MAX_RAW_BYTES:
        text, text_cut = _truncate_utf8(text)
        html_text, html_cut = _truncate_utf8(html_text)
        truncated = text_cut or html_cut

    snippet = make_snippet(text)
    if truncated:
        snippet = f"{snippet}{TRUNCATION_MARKER}"

    message_id = decode_header_value(message.get("Message-ID"))
    if not message_id:
        message_id = fallback_message_id or _fallback_message_id(payload)

    return {
        "subject": decode_header_value(message.get("Subject")),
        "fromAddr": decode_header_value(message.get("From")),
        "toAddr": decode_header_value(message.get("To")),
        "ccAddr": decode_header_value(message.get("Cc")),
        "messageId": message_id,
        "inReplyTo": decode_header_value(message.get("In-Reply-To")),
        "references": decode_header_value(message.get("References")),
        "date": decode_date(message.get("Date")),
        "snippet": snippet,
        "text": text,
        "html": html_text,
        "size": total_size,
        "attachments": attachments,
    }
