"""On-demand attachment download and safe local storage.

Only the standard library is used.  ``attachment.fetch`` downloads exactly one
MIME part (never the whole mailbox), sanitises its filename, writes it under
``<data-dir>/attachments/<messageId>/`` and records the path + SHA-256 in the
database.  Inline resources and messages without attachments never reach this
module through ``attachment.list``.
"""

from __future__ import annotations

import hashlib
import imaplib
import os
import re
import ssl
from pathlib import Path
from typing import Optional

from . import imap_client, mime_parse
from .protocol import INTERNAL, MailCoreError, bad_request
from .store import Store, utc_now

ATTACHMENTS_DIRNAME = "attachments"
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
MAX_MAX_BYTES = 100 * 1024 * 1024
MAX_FILENAME_LENGTH = 180

# Windows/POSIX separators, the Windows-invalid punctuation and control
# characters.  The filename is a display label and a single path component;
# it is never allowed to introduce a directory.
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def safe_filename(filename: str, fallback: str = "attachment") -> str:
    """Reduce a MIME filename to one safe path component.

    Directory separators are stripped before the remaining unsafe characters
    are replaced, so ``../../etc/passwd`` becomes ``passwd`` rather than
    escaping the attachments directory.
    """
    text = str(filename or "")
    # Both separators: the server may use either regardless of our platform.
    text = text.replace("\\", "/")
    text = text.rsplit("/", 1)[-1]
    text = _UNSAFE_FILENAME.sub("_", text)
    text = " ".join(text.split())
    text = text.strip(" .")
    if not text or text in (".", ".."):
        text = fallback
    if text.split(".", 1)[0].upper() in _RESERVED_NAMES:
        text = f"_{text}"
    if len(text) > MAX_FILENAME_LENGTH:
        stem, dot, suffix = text.rpartition(".")
        if dot and len(suffix) <= 20:
            text = f"{text[: MAX_FILENAME_LENGTH - len(suffix) - 1]}.{suffix}"
        else:
            text = text[:MAX_FILENAME_LENGTH]
    return text


def _unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(filename)
    index = 2
    while index < 10000:
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1
    raise MailCoreError(INTERNAL, "cannot allocate an attachment filename")


def store_file(
    data_dir: str | Path, message_id: str, filename: str, data: bytes
) -> Path:
    """Write attachment bytes under the data dir; returns the final path."""
    root = (Path(data_dir) / ATTACHMENTS_DIRNAME).resolve()
    directory = (root / safe_filename(message_id, "message")).resolve()
    if not directory.is_relative_to(root):
        raise MailCoreError(INTERNAL, "invalid attachment directory")
    directory.mkdir(parents=True, exist_ok=True)

    target = _unique_path(directory, safe_filename(filename))
    if not target.resolve().is_relative_to(root):
        raise MailCoreError(INTERNAL, "attachment path escaped the data dir")

    temporary = target.with_name(target.name + ".part")
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return target


def _read_uidvalidity(connection, mailbox: str) -> Optional[int]:
    try:
        _type, data = connection.response("UIDVALIDITY")
    except Exception:
        return None
    if not data:
        return None
    for item in data:
        if item is None:
            continue
        raw = item.decode("utf-8", "replace") if isinstance(
            item, (bytes, bytearray)
        ) else str(item)
        try:
            return int(raw.strip())
        except (TypeError, ValueError):
            continue
    return None


def _split_fetch(data) -> tuple[bytes, Optional[bytes]]:
    """Split FETCH literals into ``(mime_headers, body)``."""
    mime_bytes = b""
    body: Optional[bytes] = None
    for item in data or []:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        head = item[0] if isinstance(item[0], (bytes, bytearray)) else b""
        literal = item[1] if isinstance(item[1], (bytes, bytearray)) else b""
        if b".MIME" in bytes(head).upper():
            mime_bytes = bytes(literal)
        else:
            body = bytes(literal)
    return mime_bytes, body


def _single_literal(data) -> Optional[bytes]:
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            literal = item[1]
            if isinstance(literal, (bytes, bytearray)):
                return bytes(literal)
    return None


def fetch_part(connection, uid: int, part_id: str) -> bytes:
    """Fetch one body part and undo its transfer encoding."""
    if not part_id:
        raise bad_request("attachment has no MIME part id")
    spec = f"(UID BODY.PEEK[{part_id}.MIME] BODY.PEEK[{part_id}])"
    try:
        status, data = connection.uid("FETCH", str(uid), spec)
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise imap_client.ImapError(
            f"FETCH part {part_id} failed: {exc}"
        ) from None

    mime_bytes, body = _split_fetch(data)
    if status != "OK" or body is None:
        # Some servers reject the compound fetch; retry for the body alone and
        # treat the bytes as already decoded.
        try:
            status, data = connection.uid(
                "FETCH", str(uid), f"(UID BODY.PEEK[{part_id}])"
            )
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise imap_client.ImapError(
                f"FETCH part {part_id} failed: {exc}"
            ) from None
        if status != "OK":
            raise imap_client.ImapError(f"FETCH part {part_id} was rejected")
        body = _single_literal(data)
        mime_bytes = b""

    if body is None:
        raise imap_client.ImapError(f"FETCH part {part_id} returned no data")
    return mime_parse.decode_part_bytes(mime_bytes, body)


def _already_downloaded(attachment: dict) -> Optional[dict]:
    path = str(attachment.get("path") or "")
    if not attachment.get("downloaded") or not path:
        return None
    file_path = Path(path)
    if not file_path.is_file():
        return None
    try:
        size = file_path.stat().st_size
    except OSError:
        return None
    return {
        "id": attachment["id"],
        "path": str(file_path),
        "size": size,
        "sha256": attachment.get("sha256", ""),
        "fetchedAt": attachment.get("fetchedAt", ""),
    }


def download(
    store: Store,
    data_dir: str | Path,
    attachment: dict,
    account: dict,
    password: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict:
    """Download one attachment and record it; returns the protocol result."""
    if (account.get("protocol") or "imap") == "pop3":
        raise bad_request("attachment download is not supported for POP3 accounts")

    existing = _already_downloaded(attachment)
    if existing is not None:
        return existing

    declared_size = int(attachment.get("size") or 0)
    if declared_size > max_bytes:
        raise bad_request(
            f"attachment is {declared_size} bytes, larger than maxBytes {max_bytes}"
        )

    connection = None
    try:
        connection = imap_client.connect(
            account["imapHost"],
            int(account["imapPort"]),
            account["imapSecurity"],
        )
        try:
            connection.login(account["username"], password)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise imap_client.ImapError(
                f"login failed: {_redact(exc, password)}"
            ) from None

        folder = str(attachment.get("folder") or "")
        wire_folder = imap_client.encode_mailbox(folder)
        try:
            status, data = connection.select(wire_folder)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise imap_client.ImapError(
                f"SELECT {folder} failed: {_redact(exc, password)}"
            ) from None
        if status != "OK":
            raise imap_client.ImapError(f"SELECT {folder} failed")

        server_uidvalidity = _read_uidvalidity(connection, wire_folder)
        if (
            server_uidvalidity is not None
            and int(attachment.get("uidvalidity") or 0) != server_uidvalidity
        ):
            raise imap_client.ImapError(
                "UIDVALIDITY changed since the message was synced; run sync.run"
            )

        data_bytes = fetch_part(
            connection, int(attachment["uid"]), str(attachment["partId"])
        )
    except MailCoreError:
        raise
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise imap_client.ImapError(
            f"attachment download failed: {_redact(exc, password)}"
        ) from None
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass

    if len(data_bytes) > max_bytes:
        raise bad_request(
            f"attachment is {len(data_bytes)} bytes, larger than maxBytes {max_bytes}"
        )

    path = store_file(
        data_dir, str(attachment["messageId"]), attachment.get("filename", ""), data_bytes
    )
    digest = hashlib.sha256(data_bytes).hexdigest()
    fetched_at = utc_now()
    store.set_attachment_download(
        str(attachment["id"]),
        local_path=str(path),
        sha256=digest,
        fetched_at=fetched_at,
    )
    return {
        "id": attachment["id"],
        "path": str(path),
        "size": len(data_bytes),
        "sha256": digest,
        "fetchedAt": fetched_at,
    }


def _redact(text, secret: str) -> str:
    value = str(text)
    return value.replace(secret, "***") if secret else value
