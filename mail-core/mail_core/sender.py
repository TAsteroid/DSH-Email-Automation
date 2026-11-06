"""Build and send one draft over SMTP, optionally with an HTML signature.

This is the only module that actually sends mail.  It knows nothing about
approvals or the database: the service passes in an account dictionary (from
``account.list``), the password it already decrypted with DPAPI, and a draft
dictionary (from ``draft.get``).

When the account has an HTML signature the message becomes
``multipart/alternative``: the text part is the body (with a trailing
plain-text copy of the signature removed, because the composer showed it), and
the HTML part is the escaped body plus the original signature HTML.

Transport selection follows ``smtpSecurity``: ``ssl`` (``SMTP_SSL``),
``starttls`` (``SMTP`` + ``STARTTLS``) or ``none`` (plain ``SMTP``).  The
certificate context is always ``ssl.create_default_context()`` and the
timeout is 20 seconds.

Errors are raised as :class:`SendError` with a one-line, secret-free message;
the service maps them onto ``SMTP_ERROR``.
"""

from __future__ import annotations

import html as html_lib
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Iterable, Optional

from . import signature as signature_html

DEFAULT_TIMEOUT = 20.0
SECURITY_MODES = ("ssl", "starttls", "none")


class SendError(Exception):
    """A send attempt failed; ``str(error)`` is safe to show to the user."""


def describe_error(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """One-line ``Type: text`` with every given secret replaced by ``***``."""
    text = " ".join(str(exc).split())
    described = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    for secret in secrets:
        if secret:
            described = described.replace(str(secret), "***")
    return described


def _domain_of(address: str) -> str:
    """The right-hand side of an address, used for our own ``Message-ID``."""
    _, _, domain = str(address or "").rpartition("@")
    domain = domain.split(",")[0].strip().strip("<>")
    return domain or "mail-core.local"


def new_message_id(from_addr: str) -> str:
    """Generate a unique ``Message-ID`` in the account's own domain."""
    return make_msgid(domain=_domain_of(from_addr))


def _html_from_text(body: str) -> str:
    """Escape a plain-text body and turn its newlines into ``<br>``."""
    text = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    return html_lib.escape(text, quote=False).replace("\n", "<br>\n")


def _trailing_signature(body: str, signature: str) -> tuple[str, str]:
    """Split the composer's trailing plain-text signature off the body.

    Returns ``(text_body, signature_to_append)``.  A signature that already
    appears at the end of the body (as the plain-text preview the composer
    shows) is removed from the text part and still appended as HTML; a
    signature that appears elsewhere in the body is left alone but not
    appended again, so the recipient never sees it twice.  An empty signature
    keeps the old plain-text-only behaviour.
    """
    if not str(signature or "").strip():
        return body, ""

    plain = signature_html.plain_text(signature)
    trimmed = str(body or "").rstrip()
    candidates = [plain, plain.replace("\n", " ")]
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate and trimmed.endswith(candidate):
            return trimmed[: -len(candidate)].rstrip(), signature

    fragment = signature_html.plain_fragment(signature)
    if fragment and fragment in re.sub(r"\s+", " ", str(body or "")):
        return body, ""
    return body, signature


def build_message(
    *,
    from_addr: str,
    to_addr: str,
    cc_addr: str = "",
    subject: str = "",
    body: str = "",
    signature_html: str = "",
    message_id: Optional[str] = None,
) -> tuple[EmailMessage, str]:
    """Build the outgoing message; returns ``(message, message_id)``.

    An empty signature yields the previous single-part ``text/plain`` message;
    a non-empty one adds a ``text/html`` alternative with the signature.
    """
    message_id = message_id or new_message_id(from_addr)
    text_body, signature_to_append = _trailing_signature(
        body, signature_html
    )
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = to_addr
    if cc_addr:
        message["Cc"] = cc_addr
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = message_id
    message.set_content(text_body)
    if signature_to_append:
        html_body = _html_from_text(text_body)
        if html_body:
            html_body += "<br><br>"
        html_body += signature_to_append
        message.add_alternative(html_body, subtype="html")
    return message, message_id


def _close(client) -> None:
    try:
        client.quit()
    except Exception:
        try:
            client.close()
        except Exception:
            pass


def _open(host: str, port: int, security: str, timeout: float):
    """Open and (for STARTTLS) upgrade the SMTP connection."""
    context = ssl.create_default_context()
    if security == "ssl":
        return smtplib.SMTP_SSL(host, int(port), timeout=timeout, context=context)
    if security == "none":
        return smtplib.SMTP(host, int(port), timeout=timeout)
    if security == "starttls":
        client = smtplib.SMTP(host, int(port), timeout=timeout)
        try:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        except Exception:
            _close(client)
            raise
        return client
    raise SendError(f"unsupported SMTP security mode: {security!r}")


def send_message(
    host: str,
    port: int,
    security: str,
    username: str,
    password: str,
    message: EmailMessage,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> None:
    """``login`` then ``send_message`` over the configured transport.

    The password only ever reaches ``smtplib.login``; it is never interpolated
    into an error message raised by this function.
    """
    if not host:
        raise SendError("SMTP host is not configured for this account")
    client = _open(host, int(port), security, timeout)
    try:
        if username:
            client.login(username, password)
        client.send_message(message)
    finally:
        _close(client)


def send_draft(
    account: dict,
    password: str,
    draft: dict,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Send one draft with the account's SMTP settings; returns its Message-ID."""
    from_addr = str(account.get("address") or "").strip()
    if not from_addr:
        raise SendError("account has no address")
    message, message_id = build_message(
        from_addr=from_addr,
        to_addr=str(draft.get("to") or ""),
        cc_addr=str(draft.get("cc") or ""),
        subject=str(draft.get("subject") or ""),
        body=str(draft.get("body") or ""),
        signature_html=str(account.get("signatureHtml") or ""),
    )
    send_message(
        str(account.get("smtpHost") or ""),
        int(account.get("smtpPort") or 0),
        str(account.get("smtpSecurity") or "ssl"),
        str(account.get("username") or ""),
        password,
        message,
        timeout=timeout,
    )
    return message_id
