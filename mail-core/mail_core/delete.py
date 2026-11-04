"""Delete one message: Trash-move or permanent expunge, plus local cleanup.

IMAP is the only receive protocol with a per-message delete:

* ``permanent=False`` (the default) copies the message to the account's Trash
  folder, flags the original ``\\Deleted`` and expunges it.  The Trash folder
  comes from the cached ``folder.list`` (produced by ``account.test``); when
  the account has no Trash folder the delete degrades to the permanent path
  and the result says ``trashed: false``.
* ``permanent=True`` flags and expunges directly.

POP3 has no per-message delete, so its server copy is left alone and only the
local row is dropped (``localOnly: true``), which the UI must call out.

The local row is removed **after** the server operation succeeds.  A failed
connection, permission or command leaves the row in place and raises
``IMAP_ERROR``, so the message never silently disappears from the index while
still existing on the server.
"""

from __future__ import annotations

import imaplib
import re
import ssl
from typing import Iterable, Optional

from . import imap_client
from .protocol import INTERNAL, NOT_FOUND, MailCoreError
from .store import Store

# Folder names that mean "trash": English (case-insensitive) and Chinese.
TRASH_PATTERN = re.compile(r"deleted|trash|已删除|废纸", re.IGNORECASE)

# imaplib failures that mean "the server or the network failed".
_IMAP_FAILURES = (imaplib.IMAP4.error, OSError, ssl.SSLError, ValueError)


def find_trash_folder(folders: Iterable[dict]) -> Optional[str]:
    """Return the first cached folder whose name looks like a Trash folder.

    The comparison is deliberately loose: servers call it ``Trash``,
    ``Deleted Messages``, ``[Gmail]/Trash`` or ``已删除``, and the pattern the
    UI agreed on is ``deleted|trash|已删除|废纸``.
    """
    for folder in folders or []:
        name = str(folder.get("name") or "")
        if name and TRASH_PATTERN.search(name):
            return name
    return None


def _flat(data) -> str:
    """Flatten an ``imaplib`` payload into one readable line."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    if isinstance(data, (list, tuple)):
        return " ".join(_flat(item) for item in data if item is not None)
    return "" if data is None else str(data)


def _redact(text, password: str) -> str:
    value = str(text)
    return value.replace(password, "***") if password else value


def _check(status, data, action: str, password: str) -> None:
    if status != "OK":
        raise imap_client.ImapError(
            f"{action} failed: {_redact(_flat(data), password)}"
        )


def delete_local(store: Store, message: dict) -> None:
    """Remove the cached row; a repeated delete is ``NOT_FOUND``."""
    if not store.delete_message(str(message["id"])):
        raise MailCoreError(NOT_FOUND, "message not found")


def _delete_on_server(
    connection, uid: int, trash_wire: Optional[str], password: str
) -> bool:
    """COPY (optional), flag ``\\Deleted``, EXPUNGE; returns "was trashed"."""
    trashed = False
    if trash_wire:
        try:
            status, data = connection.uid("COPY", str(uid), trash_wire)
        except _IMAP_FAILURES as exc:
            raise imap_client.ImapError(
                f"COPY {uid} to trash failed: {_redact(exc, password)}"
            ) from None
        _check(status, data, f"COPY {uid} to trash", password)
        trashed = True

    try:
        status, data = connection.uid("STORE", str(uid), "+FLAGS", r"(\Deleted)")
    except _IMAP_FAILURES as exc:
        raise imap_client.ImapError(
            f"STORE {uid} +FLAGS failed: {_redact(exc, password)}"
        ) from None
    _check(status, data, f"STORE {uid} +FLAGS", password)

    try:
        status, data = connection.expunge()
    except _IMAP_FAILURES as exc:
        raise imap_client.ImapError(
            f"EXPUNGE failed: {_redact(exc, password)}"
        ) from None
    _check(status, data, "EXPUNGE", password)
    return trashed


def delete_imap(
    store: Store,
    account: dict,
    password: str,
    message: dict,
    *,
    permanent: bool = False,
) -> dict:
    """Delete one IMAP message (Trash-move unless ``permanent``) + local row."""
    account_id = str(account["id"])
    folder = str(message.get("folder") or "")
    uid = int(message.get("uid") or 0)
    if not folder or uid <= 0:
        raise MailCoreError(
            INTERNAL, "message row has no IMAP folder/uid to delete"
        )

    trash = None if permanent else find_trash_folder(store.list_folders(account_id))
    if trash is not None and trash == folder:
        # Already inside Trash: copying onto itself would leave a duplicate
        # behind, so "move to Trash" collapses to a real delete.
        trash = None
    trash_wire = imap_client.encode_mailbox(trash) if trash else None

    connection = None
    try:
        connection = imap_client.connect(
            account["imapHost"],
            int(account["imapPort"]),
            account["imapSecurity"],
        )
        try:
            connection.login(account["username"], password)
        except _IMAP_FAILURES as exc:
            raise imap_client.ImapError(
                f"login failed: {_redact(exc, password)}"
            ) from None

        wire_folder = imap_client.encode_mailbox(folder)
        try:
            status, data = connection.select(wire_folder)
        except _IMAP_FAILURES as exc:
            raise imap_client.ImapError(
                f"SELECT {folder} failed: {_redact(exc, password)}"
            ) from None
        _check(status, data, f"SELECT {folder}", password)

        trashed = _delete_on_server(connection, uid, trash_wire, password)
    except MailCoreError:
        raise
    except _IMAP_FAILURES as exc:
        raise imap_client.ImapError(
            f"delete {folder} failed: {_redact(exc, password)}"
        ) from None
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass

    delete_local(store, message)
    return {"deleted": True, "trashed": trashed, "localOnly": False}
