"""Minimal POP3 probe: log in and read the mailbox message count (``STAT``).

Only the standard library is used (``poplib`` / ``ssl``).  All three security
modes are supported: ``ssl`` (``POP3_SSL``), ``starttls`` (``POP3.stls()``)
and ``none``.  Failures are normalised into ``IMAP_ERROR`` -- the IPC protocol
reuses that code for POP3 -- with a ``pop3:`` prefix, and the password is
redacted from every message that leaves this module.
"""

from __future__ import annotations

import poplib
import ssl
from typing import Optional

from .protocol import IMAP_ERROR, MailCoreError

DEFAULT_TIMEOUT = 15.0
SECURITY_MODES = ("ssl", "starttls", "none")


class Pop3Error(MailCoreError):
    """POP3 connection/command failure; maps to the ``IMAP_ERROR`` code."""

    def __init__(self, message: str) -> None:
        super().__init__(IMAP_ERROR, f"pop3: {message}")


def _redact(text: str, secret: Optional[str]) -> str:
    if secret:
        return text.replace(secret, "***")
    return text


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _connect(host: str, port: int, security: str, timeout: float):
    context = ssl.create_default_context()
    if security == "ssl":
        # Always verify: poplib defaults to an unverified context too.
        return poplib.POP3_SSL(host, int(port), timeout=timeout, context=context)
    connection = poplib.POP3(host, int(port), timeout=timeout)
    if security == "starttls":
        try:
            connection.stls(context=context)
        except TypeError:
            # Python < 3.9 has no ``context`` parameter on ``stls``.
            connection.stls()
    return connection


def test_connection(
    host: str,
    port: int,
    security: str,
    username: str,
    password: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> int:
    """Log in to a POP3 server and return the number of messages (``STAT``).

    Raises :class:`Pop3Error` on any connection, TLS, login or ``STAT``
    failure; the message carries the server's reply with the password
    redacted.
    """
    if security not in SECURITY_MODES:
        raise Pop3Error(f"unsupported security mode: {security!r}")

    connection = None
    try:
        connection = _connect(host, int(port), security, timeout)
        try:
            connection.user(username)
            connection.pass_(password)
        except (poplib.error_proto, OSError) as exc:
            raise Pop3Error(f"login failed: {_redact(_decode(exc), password)}") from None

        try:
            count, _size = connection.stat()
        except (poplib.error_proto, OSError) as exc:
            raise Pop3Error(f"STAT failed: {_redact(_decode(exc), password)}") from None
        return int(count)
    except Pop3Error:
        raise
    except (poplib.error_proto, OSError, ssl.SSLError, ValueError) as exc:
        raise Pop3Error(
            f"connect to {host}:{port} failed: {_redact(_decode(exc), password)}"
        ) from None
    except Exception as exc:  # never leak a traceback or the password upward
        raise Pop3Error(f"POP3 probe failed: {type(exc).__name__}") from None
    finally:
        if connection is not None:
            try:
                connection.quit()
            except Exception:
                try:
                    connection.close()
                except Exception:
                    pass
