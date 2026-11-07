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


_CID_SRC = re.compile(
    r"""src\s*=\s*["']\s*cid:([^"']+?)\s*["']""", re.IGNORECASE
)

# The client inserts this exact divider line between a reply's new text and
# the quoted message; surrounding whitespace is tolerated.
QUOTE_SEPARATOR = "------------------ 原邮件 ------------------"


def _split_quote(body: str) -> Optional[tuple[str, str]]:
    """Split a reply body into ``(head, quoted)`` at the divider line.

    The divider itself is dropped; ``head`` is everything before it and
    ``quoted`` everything after it.  ``None`` when the body has no divider.
    """
    text = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == QUOTE_SEPARATOR:
            return "\n".join(lines[:index]), "\n".join(lines[index + 1 :])
    return None


def _signature_variants(signature: str) -> list[str]:
    """Plain-text renderings of the signature used to locate it in a body."""
    plain = signature_html.plain_text(signature)
    variants: list[str] = []
    for candidate in (
        plain,
        plain.replace("\n", " "),
        re.sub(r"\s+", " ", plain),
    ):
        candidate = candidate.strip()
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def _strip_signature_block(head: str, signature: str) -> str:
    """Remove the plain-text signature block from ``head`` (and blank lines).

    The block is located through the first-40-character text preview used
    elsewhere, then whole lines around it are removed, including the blank
    lines directly before and after it.
    """
    lines = str(head or "").split("\n")
    if not str(signature or "").strip():
        return head
    for candidate in _signature_variants(signature):
        joined = "\n".join(lines)
        index = joined.find(candidate)
        if index < 0:
            continue
        start_line = joined.count("\n", 0, index)
        end_char = index + len(candidate)
        end_line = joined.count("\n", 0, end_char)
        if end_char > 0 and joined[end_char - 1] == "\n":
            end_line -= 1
        first = start_line
        while first > 0 and not lines[first - 1].strip():
            first -= 1
        last = end_line
        while last + 1 < len(lines) and not lines[last + 1].strip():
            last += 1
        return "\n".join(lines[:first] + lines[last + 1 :])
    return head


def _compose_bodies(body: str, signature: str) -> tuple[str, str, str]:
    """Return ``(text_body, html_body, signature_html_used)``.

    With a quote divider, the HTML puts the signature between the reply head
    (minus the composer's plain-text signature preview) and the quoted text,
    while the text part is left exactly as the client produced it.  Without a
    divider the previous trailing-signature behaviour is kept.
    """
    if not str(signature or "").strip():
        return body, "", ""

    quoted = _split_quote(body)
    if quoted is not None:
        head, tail = quoted
        head_text = _strip_signature_block(head, signature).strip()
        quoted_text = tail.strip()
        pieces: list[str] = []
        if head_text:
            pieces.append(_html_from_text(head_text))
        pieces.append(signature)
        if quoted_text:
            pieces.append(_html_from_text(quoted_text))
        return body, "<br><br>".join(pieces), signature

    text_body, signature_to_append = _trailing_signature(body, signature)
    if not signature_to_append:
        return text_body, "", ""
    html_body = _html_from_text(text_body)
    if html_body:
        html_body += "<br><br>"
    html_body += signature_to_append
    return text_body, html_body, signature_to_append


def _related_images(
    signature: str, signature_images: Iterable[dict]
) -> list[tuple[str, dict]]:
    """Images referenced by ``src="cid:..."`` in the signature HTML.

    Matching is case-insensitive and only images actually referenced are
    attached; unknown cids are ignored (the HTML reference is left untouched).
    """
    if not str(signature or "").strip() or not signature_images:
        return []
    by_cid: dict[str, tuple[str, dict]] = {}
    for image in signature_images:
        cid = str(image.get("cid") or "").strip()
        if cid and image.get("data"):
            by_cid.setdefault(cid.lower(), (cid, image))

    wanted: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for match in _CID_SRC.finditer(signature):
        key = match.group(1).strip().lower()
        if key in seen or key not in by_cid:
            continue
        seen.add(key)
        wanted.append(by_cid[key])
    return wanted


def _plain_from_html(html_text: str) -> str:
    """Visible text of an HTML body, used as the plain-text fallback."""
    return signature_html.plain_text(html_text)


def _attach_html(
    message: EmailMessage,
    text_body: str,
    html_body: str,
    signature_images: Optional[Iterable[dict]],
) -> EmailMessage:
    """Add ``html_body`` as the alternative (and cid images as related parts)."""
    related = _related_images(html_body, signature_images or ())
    if not related:
        message.add_alternative(html_body, subtype="html")
        return message

    alternative = EmailMessage(policy=message.policy)
    alternative.set_content(text_body)
    alternative.add_alternative(html_body, subtype="html")

    root = EmailMessage(policy=message.policy)
    for key, value in message.items():
        if not key.lower().startswith("content-"):
            root[key] = value
    root.set_type("multipart/related")
    root.attach(alternative)
    for cid, image in related:
        mime = str(image.get("mime") or "")
        if "/" not in mime:
            continue
        maintype, subtype = mime.split("/", 1)
        part = EmailMessage(policy=message.policy)
        part.set_content(
            image["data"],
            maintype=maintype,
            subtype=subtype,
            cid=f"<{cid}>",
            disposition="inline",
            filename=str(image.get("filename") or "") or None,
        )
        root.attach(part)
    return root


def build_message(
    *,
    from_addr: str,
    to_addr: str,
    cc_addr: str = "",
    subject: str = "",
    body: str = "",
    body_html: str = "",
    signature_html: str = "",
    signature_images: Optional[Iterable[dict]] = None,
    message_id: Optional[str] = None,
) -> tuple[EmailMessage, str]:
    """Build the outgoing message; returns ``(message, message_id)``.

    A non-empty ``body_html`` (rich-text draft) wins: it is used verbatim as
    the HTML part, ``body`` is the text part and the account signature is
    *not* appended again.  Without it, the legacy build applies: an empty
    signature yields a single ``text/plain`` message; a non-empty one adds a
    ``text/html`` alternative (with the signature).  ``cid:`` images found in
    the HTML are attached as ``multipart/related`` parts.
    """
    message_id = message_id or new_message_id(from_addr)
    if str(body_html or "").strip():
        text_body = str(body or "")
        if not text_body.strip():
            text_body = _plain_from_html(body_html)
        html_body = str(body_html)
        message = EmailMessage()
        message["From"] = from_addr
        message["To"] = to_addr
        if cc_addr:
            message["Cc"] = cc_addr
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = message_id
        message.set_content(text_body)
        return (
            _attach_html(message, text_body, html_body, signature_images),
            message_id,
        )

    text_body, html_body, _ = _compose_bodies(body, signature_html)
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = to_addr
    if cc_addr:
        message["Cc"] = cc_addr
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = message_id
    message.set_content(text_body)
    if not html_body:
        return message, message_id

    return (
        _attach_html(message, text_body, html_body, signature_images),
        message_id,
    )


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
    signature_images: Optional[Iterable[dict]] = None,
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
        body_html=str(draft.get("bodyHtml") or ""),
        signature_html=str(account.get("signatureHtml") or ""),
        signature_images=signature_images,
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
