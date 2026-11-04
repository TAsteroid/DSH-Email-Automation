"""Delete or restore one message, with server-side verification.

IMAP is the only receive protocol with a per-message delete:

* ``permanent=False`` (the default) copies the message to the account's Trash
  folder, flags the original ``\\Deleted`` and expunges it.  The Trash folder
  comes from the cached ``folder.list`` (produced by ``account.test``); when
  the account has no Trash folder the delete degrades to the permanent path
  and the result says ``trashed: false``.
* ``permanent=True`` flags and expunges directly.

``restore_imap`` is the mirror image: it copies a Trash message back to a real
folder and expunges the Trash copy.

POP3 has no per-message delete, so its server copy is left alone and only the
local row is dropped (``localOnly: true``), which the UI must call out.

The local row is removed **after** the server operation is verified.  An
``OK`` reply alone is not trusted, because some servers answer ``OK`` to a
``UID COPY`` / ``UID STORE`` that did nothing (an unknown UID is the common
case) and the mail then reappears on the next sync.  So the destination folder
is counted around the copy, and the source folder is searched for the UID
after the expunge; either check failing raises ``IMAP_ERROR`` and keeps the
local row, so the user sees a failure instead of a phantom delete.
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


def _parse_uids(data) -> list[str]:
    """UIDs from a ``SEARCH`` payload (``[b"1 2 3"]`` or ``[None]``)."""
    tokens: list[str] = []
    for item in data if isinstance(data, (list, tuple)) else [data]:
        if item is None:
            continue
        tokens.extend(_flat(item).split())
    return tokens


def _select(
    connection, name: str, wire: str, password: str, *, readonly: bool = False
) -> None:
    """``SELECT`` / ``EXAMINE`` one folder and fail closed on a non-OK reply."""
    try:
        status, data = connection.select(wire, readonly=readonly)
    except _IMAP_FAILURES as exc:
        raise imap_client.ImapError(
            f"SELECT {name} failed: {_redact(exc, password)}"
        ) from None
    _check(status, data, f"SELECT {name}", password)


def _search(connection, criteria: str, action: str, password: str) -> list[str]:
    try:
        status, data = connection.uid("SEARCH", None, criteria)
    except _IMAP_FAILURES as exc:
        raise imap_client.ImapError(
            f"{action} failed: {_redact(exc, password)}"
        ) from None
    _check(status, data, action, password)
    return _parse_uids(data)


def _count_mailbox(connection, name: str, password: str) -> int:
    """Number of messages in the currently selected mailbox."""
    return len(_search(connection, "ALL", f"UID SEARCH ALL in {name}", password))


def _copy_and_verify(
    connection,
    *,
    uid: int,
    source_folder: str,
    source_wire: str,
    target_name: str,
    target_wire: str,
    password: str,
) -> None:
    """``UID COPY`` then prove it landed by counting the target folder.

    An ``OK`` reply is not proof.  A COPY can be a no-op when the UID the
    client believes in no longer exists (a stale local row), so the target
    folder is counted with ``UID SEARCH ALL`` before and after; no growth
    raises ``IMAP_ERROR`` and the caller keeps the local row.
    """
    _select(connection, target_name, target_wire, password, readonly=True)
    before = _count_mailbox(connection, target_name, password)

    _select(connection, source_folder, source_wire, password)
    try:
        status, data = connection.uid("COPY", str(uid), target_wire)
    except _IMAP_FAILURES as exc:
        raise imap_client.ImapError(
            f"COPY {uid} to {target_name} failed: {_redact(exc, password)}"
        ) from None
    _check(status, data, f"COPY {uid} to {target_name}", password)

    _select(connection, target_name, target_wire, password, readonly=True)
    after = _count_mailbox(connection, target_name, password)
    if after <= before:
        raise imap_client.ImapError(
            f"COPY {uid} to {target_name} did not land: "
            f"{before} -> {after} message(s), local copy kept"
        )


def _flag_deleted_and_expunge(
    connection,
    *,
    uid: int,
    folder: str,
    wire_folder: str,
    password: str,
) -> None:
    """STORE ``\\Deleted`` + EXPUNGE and verify the UID really disappeared."""
    _select(connection, folder, wire_folder, password)
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

    _select(connection, folder, wire_folder, password, readonly=True)
    if _search(
        connection, f"UID {uid}", f"UID SEARCH UID {uid} in {folder}", password
    ):
        raise imap_client.ImapError(
            f"UID {uid} is still on the server in {folder} after EXPUNGE, "
            "local copy kept"
        )


def _delete_on_server(
    connection,
    *,
    uid: int,
    folder: str,
    wire_folder: str,
    trash_name: Optional[str],
    trash_wire: Optional[str],
    password: str,
) -> bool:
    """Move to Trash (optional) or expunge, verifying every step."""
    trashed = False
    if trash_wire:
        _copy_and_verify(
            connection,
            uid=uid,
            source_folder=folder,
            source_wire=wire_folder,
            target_name=trash_name or "Trash",
            target_wire=trash_wire,
            password=password,
        )
        trashed = True
    _flag_deleted_and_expunge(
        connection, uid=uid, folder=folder, wire_folder=wire_folder, password=password
    )
    return trashed


def is_trash_folder(name: str) -> bool:
    """Whether a decoded folder name is a Trash-class folder."""
    return bool(name) and TRASH_PATTERN.search(str(name)) is not None


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

        trashed = _delete_on_server(
            connection,
            uid=uid,
            folder=folder,
            wire_folder=imap_client.encode_mailbox(folder),
            trash_name=trash,
            trash_wire=trash_wire,
            password=password,
        )
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


def restore_imap(
    store: Store,
    account: dict,
    password: str,
    message: dict,
    folder: str,
) -> dict:
    """Copy a Trash message back to ``folder``, expunge the Trash copy.

    The direction of the verification is the mirror image of a delete: the
    *target* folder is counted before/after the ``COPY``, and the source
    Trash folder is searched for the UID after the expunge.  Any failed
    verification raises ``IMAP_ERROR`` and leaves the local row alone, so a
    restore that did not really happen is never presented as success.
    """
    account_id = str(account["id"])
    source = str(message.get("folder") or "")
    uid = int(message.get("uid") or 0)
    if not source or uid <= 0:
        raise MailCoreError(
            INTERNAL, "message row has no IMAP folder/uid to restore"
        )

    source_wire = imap_client.encode_mailbox(source)
    target_wire = imap_client.encode_mailbox(folder)

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

        _copy_and_verify(
            connection,
            uid=uid,
            source_folder=source,
            source_wire=source_wire,
            target_name=folder,
            target_wire=target_wire,
            password=password,
        )
        _flag_deleted_and_expunge(
            connection,
            uid=uid,
            folder=source,
            wire_folder=source_wire,
            password=password,
        )
    except MailCoreError:
        raise
    except _IMAP_FAILURES as exc:
        raise imap_client.ImapError(
            f"restore {source} -> {folder} failed: {_redact(exc, password)}"
        ) from None
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass

    delete_local(store, message)
    return {"restored": True, "folder": folder}
