"""Minimal IMAP probe: connectivity check plus folder enumeration.

Only the standard library is used (``imaplib`` / ``ssl``).  Every failure is
normalised into ``IMAP_ERROR`` carrying the server's own text, with the
password redacted if a server ever echoes it back.

Folder names cross the wire in IMAP modified UTF-7 (RFC 3501 §5.1.3).  This
module is the protocol boundary: :func:`parse_list_response` decodes names
into Unicode, and :func:`encode_mailbox` turns a decoded name back into the
token an IMAP command needs.
"""

from __future__ import annotations

import imaplib
import re
import ssl
from typing import Iterable, Optional

from . import imap_utf7
from .protocol import IMAP_ERROR, MailCoreError

DEFAULT_TIMEOUT = 15.0
SECURITY_MODES = ("ssl", "starttls", "none")

# (\HasNoChildren) "/" "INBOX"   |   (\HasNoChildren) NIL INBOX
_LIST_LINE = re.compile(
    r'^\((?P<flags>[^)]*)\)\s+(?P<delimiter>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$',
    re.IGNORECASE | re.DOTALL,
)

# RFC 3501 atom-specials: such a mailbox name must be a quoted string.
_ATOM_SPECIALS = frozenset('(){%*"\\] ')


class ImapError(MailCoreError):
    """IMAP connection/command failure; maps to the ``IMAP_ERROR`` code."""

    def __init__(self, message: str) -> None:
        super().__init__(IMAP_ERROR, message)


def _redact(text: str, secret: Optional[str]) -> str:
    if secret:
        return text.replace(secret, "***")
    return text


def _decode(raw) -> str:
    """Decode an IMAP wire token to text; folder names are decoded separately."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def encode_mailbox(name: str) -> str:
    """Return the token to put in ``SELECT`` / ``STATUS`` for ``name``.

    ``name`` is the decoded (Unicode) folder name kept in the database; the
    server only understands modified UTF-7, and a name with a space or an
    IMAP atom-special has to be a quoted string.
    """
    wire = imap_utf7.encode(name)
    safe = (
        wire
        and wire.upper() != "NIL"
        and all(0x20 <= ord(char) <= 0x7E for char in wire)
        and not any(char in _ATOM_SPECIALS for char in wire)
    )
    if safe:
        return wire
    escaped = wire.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote(token: str) -> str:
    """Decode an IMAP quoted string or ``NIL`` into plain text."""
    token = token.strip()
    if token.upper() == "NIL":
        return ""
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        body = token[1:-1]
        out = []
        index = 0
        while index < len(body):
            char = body[index]
            if char == "\\" and index + 1 < len(body):
                out.append(body[index + 1])
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out)
    return token


def parse_list_line(line: str) -> Optional[dict]:
    """Parse one ``LIST`` response line into ``{name, delimiter, flags}``.

    ``name`` is decoded from modified UTF-7, so it is the Unicode name the
    rest of the application (and the database) uses.
    """
    match = _LIST_LINE.match(line.strip())
    if match is None:
        return None
    flags = [flag for flag in match.group("flags").split() if flag]
    return {
        "name": imap_utf7.decode(_unquote(match.group("name"))),
        "delimiter": _unquote(match.group("delimiter")),
        "flags": flags,
    }


def parse_list_response(data: Iterable) -> list[dict]:
    """Parse every entry of an ``imaplib`` ``LIST`` response."""
    folders: list[dict] = []
    for entry in data or []:
        if entry is None:
            continue
        if isinstance(entry, tuple):
            # A server may answer with a literal: (head_bytes, literal_bytes).
            raw = b" ".join(part for part in entry if isinstance(part, bytes))
        elif isinstance(entry, bytes):
            raw = entry
        else:
            raw = str(entry).encode("utf-8", "replace")
        parsed = parse_list_line(_decode(raw))
        if parsed is not None:
            folders.append(parsed)
    return folders


def _connect(host: str, port: int, security: str, timeout: float):
    if security == "ssl":
        # imaplib.IMAP4_SSL defaults to ssl._create_stdlib_context(), which does
        # NOT verify certificates.  A password over an unverified channel is
        # worse than a failed login, so always pass a verifying context.
        return imaplib.IMAP4_SSL(
            host,
            port,
            ssl_context=ssl.create_default_context(),
            timeout=timeout,
        )
    connection = imaplib.IMAP4(host, port, timeout=timeout)
    if security == "starttls":
        connection.starttls(ssl.create_default_context())
    return connection


def connect(
    host: str,
    port: int,
    security: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
):
    """Open an IMAP connection without logging in.

    Used by the sync layer; callers own ``logout()``.  Raises
    :class:`ImapError` for an unsupported security mode or connection failure.
    """
    if security not in SECURITY_MODES:
        raise ImapError(f"unsupported security mode: {security!r}")
    try:
        return _connect(host, int(port), security, timeout)
    except ImapError:
        raise
    except (imaplib.IMAP4.error, OSError, ssl.SSLError, ValueError) as exc:
        raise ImapError(f"connect to {host}:{port} failed: {exc}") from None


def test_connection(
    host: str,
    port: int,
    security: str,
    username: str,
    password: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Log in to an IMAP server and return its folder list.

    Each folder is ``{"name", "delimiter", "flags"}`` with ``name`` already
    decoded from modified UTF-7.  Raises :class:`ImapError` on any
    connection, TLS, login or ``LIST`` failure; the message carries the
    server's reply with the password redacted.
    """
    if security not in SECURITY_MODES:
        raise ImapError(f"unsupported security mode: {security!r}")

    connection = None
    try:
        connection = _connect(host, int(port), security, timeout)
        try:
            connection.login(username, password)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"login failed: {_redact(_decode(str(exc)), password)}") from None

        try:
            status, data = connection.list()
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(f"LIST failed: {_redact(_decode(str(exc)), password)}") from None
        if status != "OK":
            raise ImapError(f"LIST failed: {_redact(_decode(data), password)}")
        return parse_list_response(data)
    except ImapError:
        raise
    except (imaplib.IMAP4.error, OSError, ssl.SSLError, ValueError) as exc:
        raise ImapError(f"connect to {host}:{port} failed: {_redact(str(exc), password)}") from None
    except Exception as exc:  # never leak a traceback or the password upward
        raise ImapError(f"IMAP probe failed: {type(exc).__name__}") from None
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass
