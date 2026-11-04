"""SMTP reachability probe: connect, ``EHLO``, optional STARTTLS, ``QUIT``.

No message is ever sent and no SMTP AUTH is attempted, so no password enters
this module at all.  Failures are returned as data
(``{"ok": False, "detail": ...}``) instead of raised: a broken SMTP server
must not fail the overall ``account.test`` result.

The certificate context is always ``ssl.create_default_context()``.
"""

from __future__ import annotations

import smtplib
import ssl

DEFAULT_TIMEOUT = 15.0
SECURITY_MODES = ("ssl", "starttls", "none")


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    name = type(exc).__name__
    # Server replies may contain newlines; keep ``detail`` one line.
    text = " ".join(text.split())
    return f"{name}: {text}" if text else name


def _connect(host: str, port: int, security: str, timeout: float):
    context = ssl.create_default_context()
    if security == "ssl":
        return smtplib.SMTP_SSL(host, int(port), timeout=timeout, context=context)
    return smtplib.SMTP(host, int(port), timeout=timeout)


def check_connection(
    host: str,
    port: int,
    security: str = "ssl",
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Probe an SMTP server; never raises and never sends mail.

    Returns ``{"ok": bool, "detail": str}`` where ``detail`` is empty on
    success and describes the failure otherwise.
    """
    if security not in SECURITY_MODES:
        return {"ok": False, "detail": f"unsupported security mode: {security!r}"}

    connection = None
    try:
        connection = _connect(host, int(port), security, timeout)
        connection.ehlo()
        if security == "starttls":
            connection.starttls(context=ssl.create_default_context())
            connection.ehlo()
        return {"ok": True, "detail": ""}
    except Exception as exc:
        return {"ok": False, "detail": _describe(exc)}
    finally:
        if connection is not None:
            try:
                connection.quit()
            except Exception:
                try:
                    connection.close()
                except Exception:
                    pass
