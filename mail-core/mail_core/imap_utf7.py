"""IMAP modified UTF-7 (RFC 3501 §5.1.3) codec, standard library only.

Mailbox names on the IMAP wire are *modified* UTF-7: printable US-ASCII runs
travel as-is, ``&`` opens a base64 run of UTF-16BE text and ``-`` closes it,
and a literal ``&`` is written as ``&-``.  The "modified" base64 alphabet uses
``,`` where standard base64 uses ``/`` and omits padding.

Both directions are total functions.  A malformed name never raises: a run
that cannot be decoded is kept verbatim so callers still get a readable
string (better than a garbled or empty folder name in the UI).

Typical use at the protocol boundary::

    decoded = imap_utf7.decode("&XfJT0ZAB-")   # -> "已发送"
    wire = imap_utf7.encode("已发送")           # -> "&XfJT0ZAB-"
"""

from __future__ import annotations

import base64
import binascii

# Visible ASCII travels verbatim; everything else goes through base64.
_ASCII_LOW = 0x20
_ASCII_HIGH = 0x7E


def decode(name: str) -> str:
    """Decode a modified UTF-7 mailbox name into Unicode.

    ``&`` starts a base64 run (terminated by ``-``); ``&-`` is a literal
    ``&``.  Invalid runs are returned unchanged instead of raising, and a
    trailing ``&`` without a terminator is treated as a literal ampersand.

    >>> decode("&XfJT0ZAB-")
    '已发送'
    >>> decode("&-")
    '&'
    """
    if not name or "&" not in name:
        return name

    out: list[str] = []
    index = 0
    length = len(name)
    while index < length:
        char = name[index]
        if char != "&":
            out.append(char)
            index += 1
            continue

        end = name.find("-", index + 1)
        if end < 0:
            segment = name[index + 1 :]
            terminator = ""
            consumed = length
        else:
            segment = name[index + 1 : end]
            terminator = "-"
            consumed = end + 1

        if not segment:
            # ``&-`` is a literal ampersand; a lone trailing ``&`` is kept.
            out.append("&")
        else:
            decoded = _decode_segment(segment)
            if decoded is None:
                # Unparseable run: keep it visible rather than dropping it.
                out.append("&" + segment + terminator)
            else:
                out.append(decoded)
        index = consumed
    return "".join(out)


def encode(text: str) -> str:
    """Encode Unicode text into a modified UTF-7 mailbox name.

    ASCII is passed through except ``&`` (written ``&-``); any run of
    non-ASCII characters is base64(UTF-16BE) wrapped in ``&`` … ``-``.

    >>> encode("已发送")
    '&XfJT0ZAB-'
    >>> encode("Drafts & More")
    'Drafts &- More'
    """
    if not text:
        return ""

    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        raw = "".join(pending).encode("utf-16-be", "replace")
        token = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        out.append(f"&{token}-")
        pending.clear()

    for char in text:
        if _ASCII_LOW <= ord(char) <= _ASCII_HIGH:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(out)


def _decode_segment(segment: str) -> str | None:
    """Decode one base64 run; ``None`` when it is not valid modified base64."""
    text = segment.rstrip("=").replace(",", "/")
    if not text:
        return None
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw:
        return None
    try:
        # Odd byte counts / lone surrogates are damaged but still readable.
        return raw.decode("utf-16-be", "replace")
    except (UnicodeDecodeError, ValueError):  # pragma: no cover - defensive
        return None
