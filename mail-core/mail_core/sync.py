"""Mailbox synchronisation: IMAP/POP3 -> ``messages`` table.

This module owns the cursor rules from ``docs/IPC_PROTOCOL.md``:

* IMAP uses ``UIDVALIDITY`` + ``last_uid`` from ``sync_state``.  A changed
  UIDVALIDITY deletes the folder's cached rows (they no longer address the
  same messages) and restarts from UID 1.
* POP3 has no cursor: the most recent ``limit`` messages are re-read on every
  run and de-duplicated through the stable ``UIDL``-derived uid.
* ``INSERT OR IGNORE`` + the UNIQUE constraint make a repeated sync a no-op;
  ``stored`` counts only rows the database actually accepted.
* Messages already stored are never rolled back on a network error: the
  cursor and error status are persisted, and the next run resumes from there.
"""

from __future__ import annotations

import hashlib
import imaplib
import poplib
import re
import ssl
from datetime import datetime, timezone
from typing import Any, Optional

from . import imap_client, mime_parse, pop3_client
from .protocol import MailCoreError
from .store import Store

DEFAULT_FOLDER = "INBOX"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_RFC822_SIZE = re.compile(rb"RFC822\.SIZE\s+(\d+)", re.IGNORECASE)
_UIDVALIDITY_IN_STATUS = re.compile(r"UIDVALIDITY\s+(\d+)", re.IGNORECASE)

# imaplib errors that mean "the server or the network failed" (as opposed to a
# bug in this module).
_IMAP_FAILURES = (imaplib.IMAP4.error, OSError, ssl.SSLError, ValueError)
_POP3_FAILURES = (poplib.error_proto, OSError, ssl.SSLError, ValueError)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redact(text: Any, secret: str) -> str:
    value = str(text)
    if secret:
        value = value.replace(secret, "***")
    return value


def _text(data: Any) -> str:
    """Flatten an imaplib payload into a readable one-line error detail."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    if isinstance(data, (list, tuple)):
        return " ".join(_text(item) for item in data if item is not None)
    return "" if data is None else str(data)


def _record_error(
    store: Store,
    account_id: str,
    folder: str,
    *,
    uidvalidity: int,
    last_uid: int,
    error: str,
) -> None:
    """Persist the failure without masking the original exception."""
    try:
        store.set_sync_state(
            account_id,
            folder,
            uidvalidity=int(uidvalidity),
            last_uid=int(last_uid),
            last_sync_at=_now(),
            status="error",
            error=str(error)[:500],
        )
    except Exception:  # pragma: no cover - only a broken database gets here
        pass


def _result(
    account_id: str,
    folder: str,
    uidvalidity: int,
    fetched: int,
    stored: int,
    skipped: int,
    last_uid: int,
    started_at: str,
) -> dict:
    return {
        "accountId": account_id,
        "folder": folder,
        "uidValidity": int(uidvalidity),
        "fetched": fetched,
        "stored": stored,
        "skipped": skipped,
        "lastUid": int(last_uid),
        "startedAt": started_at,
        "finishedAt": _now(),
    }


def run_sync(
    store: Store,
    account: dict,
    password: str,
    folder: str = DEFAULT_FOLDER,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Sync one folder (or one POP3 mailbox) and return the protocol result."""
    receive_protocol = account.get("protocol") or "imap"
    if receive_protocol == "pop3":
        return _sync_pop3(store, account, password, folder, limit)
    return _sync_imap(store, account, password, folder, limit)


# -- IMAP ------------------------------------------------------------------


def _read_uidvalidity(connection, mailbox: str) -> Optional[int]:
    """Read UIDVALIDITY from the SELECT/EXAMINE response.

    ``mailbox`` is the wire token (see :func:`imap_client.encode_mailbox`).
    Falls back to ``STATUS`` for servers that do not include it untagged.
    Returns ``None`` when the server refuses to report one.
    """
    try:
        _type, data = connection.response("UIDVALIDITY")
    except Exception:
        data = None
    if data:
        for item in data:
            if item is None:
                continue
            try:
                return int(_text(item).strip())
            except (TypeError, ValueError):
                continue
    try:
        status, data = connection.status(mailbox, "(UIDVALIDITY)")
        if status == "OK":
            match = _UIDVALIDITY_IN_STATUS.search(_text(data))
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return None


def _parse_search(data: Any) -> list[int]:
    uids: set[int] = set()
    for item in data or []:
        text = _text(item)
        for token in text.split():
            if token.isdigit():
                uids.add(int(token))
    return sorted(uids)


def _fetch_one(connection, uid: int) -> tuple[bytes, int]:
    """Fetch one full message; returns ``(raw_bytes, rfc822_size)``."""
    status, data = connection.uid(
        "FETCH", str(uid), "(UID RFC822.SIZE BODY.PEEK[])"
    )
    if status != "OK":
        raise imap_client.ImapError(
            f"FETCH {uid} failed: {_text(data)}"
        )

    raw: Optional[bytes] = None
    size: Optional[int] = None
    for item in data or []:
        if not isinstance(item, tuple):
            continue
        head = item[0] if item else b""
        match = _RFC822_SIZE.search(head if isinstance(head, (bytes, bytearray)) else b"")
        if match:
            size = int(match.group(1))
        # A literal response is ``(head, literal_bytes)``.
        for part in item[1:]:
            if isinstance(part, (bytes, bytearray)):
                raw = bytes(part)
                break
        if raw is not None:
            break

    if raw is None:
        raise imap_client.ImapError(f"FETCH {uid} returned no message body")
    return raw, size if size is not None else len(raw)


def _sync_imap(
    store: Store,
    account: dict,
    password: str,
    folder: str,
    limit: int,
) -> dict:
    account_id = str(account["id"])
    started_at = _now()
    state = store.get_sync_state(account_id, folder) or {}
    previous_uidvalidity = state.get("uidvalidity")
    last_uid = int(state.get("lastUid") or 0)
    uidvalidity = int(previous_uidvalidity or 0)
    fetched = stored = skipped = 0
    connection = None
    # ``folder`` is the decoded name used by the database and the UI; the
    # server only accepts its modified UTF-7 (optionally quoted) form.
    wire_folder = imap_client.encode_mailbox(folder)

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

        try:
            status, data = connection.select(wire_folder)
        except _IMAP_FAILURES as exc:
            raise imap_client.ImapError(
                f"SELECT {folder} failed: {_redact(exc, password)}"
            ) from None
        if status != "OK":
            raise imap_client.ImapError(f"SELECT {folder} failed: {_text(data)}")

        server_uidvalidity = _read_uidvalidity(connection, wire_folder)
        uidvalidity_reset = (
            server_uidvalidity is not None
            and previous_uidvalidity is not None
            and int(previous_uidvalidity) != int(server_uidvalidity)
        )
        search_start = 1 if uidvalidity_reset else last_uid + 1

        try:
            status, data = connection.uid("SEARCH", None, f"UID {search_start}:*")
        except _IMAP_FAILURES as exc:
            raise imap_client.ImapError(
                f"UID SEARCH failed: {_redact(exc, password)}"
            ) from None
        if status != "OK":
            raise imap_client.ImapError(f"UID SEARCH failed: {_text(data)}")

        if uidvalidity_reset:
            # Old UIDs no longer mean the same messages: rebuild this folder.
            store.delete_messages(account_id, folder)
            last_uid = 0
        if server_uidvalidity is not None:
            uidvalidity = int(server_uidvalidity)

        # ``UID n:*`` returns the last message even when its UID is below n;
        # filtering by ``> last_uid`` keeps a repeated sync truly empty.
        pending = [uid for uid in _parse_search(data) if uid > last_uid]
        # 新邮件优先：待拉数量超过一次限额时，取最新的一批。
        # 否则首次同步会从最旧的邮件开始往里灌，界面上永远先看到很久以前的信。
        uids = pending[-limit:] if len(pending) > limit else pending

        for uid in uids:
            raw, size = _fetch_one(connection, uid)
            parsed = mime_parse.parse_message(raw, size)
            inserted = store.insert_message(
                account_id=account_id,
                folder=folder,
                uidvalidity=uidvalidity,
                uid=uid,
                message_id=parsed["messageId"],
                subject=parsed["subject"],
                from_addr=parsed["fromAddr"],
                to_addr=parsed["toAddr"],
                cc_addr=parsed["ccAddr"],
                date=parsed["date"],
                snippet=parsed["snippet"],
                body_text=parsed["text"],
                body_html=parsed["html"],
                size=parsed["size"],
            )
            fetched += 1
            if inserted:
                stored += 1
            else:
                skipped += 1
            last_uid = uid
    except MailCoreError as exc:
        _record_error(
            store,
            account_id,
            folder,
            uidvalidity=uidvalidity,
            last_uid=last_uid,
            error=exc.message,
        )
        raise
    except _IMAP_FAILURES as exc:
        message = _redact(exc, password)
        _record_error(
            store,
            account_id,
            folder,
            uidvalidity=uidvalidity,
            last_uid=last_uid,
            error=message,
        )
        raise imap_client.ImapError(f"sync {folder} failed: {message}") from None
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass

    store.set_sync_state(
        account_id,
        folder,
        uidvalidity=uidvalidity,
        last_uid=last_uid,
        last_sync_at=_now(),
        status="idle",
        error="",
    )
    return _result(
        account_id,
        folder,
        uidvalidity,
        fetched,
        stored,
        skipped,
        last_uid,
        started_at,
    )


# -- POP3 ------------------------------------------------------------------


def _parse_uidl(lines: Any) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    for raw in lines or []:
        text = raw.decode("ascii", "replace") if isinstance(
            raw, (bytes, bytearray)
        ) else str(raw)
        parts = text.split()
        if len(parts) >= 2 and parts[0].isdigit():
            entries.append((int(parts[0]), parts[1]))
    return entries


def _pop3_uid(uidl: str, ordinal: int) -> int:
    """Derive the integer uid the messages table keys a POP3 message by.

    The digits of the stable ``UIDL`` are used ("its integer part"); a UIDL
    with no digits is hashed so repeated syncs still map to the same uid.
    """
    digits = re.findall(r"\d+", uidl or "")
    if digits:
        value = int("".join(digits))
        if value > 0:
            return value
    digest = hashlib.sha1(
        (uidl or f"#{ordinal}").encode("utf-8", "replace")
    ).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF or ordinal


def _sync_pop3(
    store: Store,
    account: dict,
    password: str,
    folder: str,
    limit: int,
) -> dict:
    account_id = str(account["id"])
    started_at = _now()
    state = store.get_sync_state(account_id, folder) or {}
    last_uid = int(state.get("lastUid") or 0)
    fetched = stored = skipped = 0
    batch_last_uid = last_uid
    connection = None

    try:
        connection = pop3_client.connect(
            account["imapHost"],
            int(account["imapPort"]),
            account["imapSecurity"],
        )
        try:
            connection.user(account["username"])
            connection.pass_(password)
        except _POP3_FAILURES as exc:
            raise pop3_client.Pop3Error(
                f"login failed: {_redact(exc, password)}"
            ) from None

        try:
            _status, lines, _octets = connection.uidl()
        except _POP3_FAILURES as exc:
            raise pop3_client.Pop3Error(
                f"UIDL failed: {_redact(exc, password)}"
            ) from None
        entries = sorted(_parse_uidl(lines))
        # No server cursor: only the most recent ``limit`` messages are read.
        selected = entries[-limit:] if limit < len(entries) else entries

        for message_number, uidl in selected:
            uid = _pop3_uid(uidl, message_number)
            try:
                _status, body_lines, _octets = connection.retr(message_number)
            except _POP3_FAILURES as exc:
                raise pop3_client.Pop3Error(
                    f"RETR {message_number} failed: {_redact(exc, password)}"
                ) from None
            raw = b"\r\n".join(body_lines)
            if body_lines:
                raw += b"\r\n"
            parsed = mime_parse.parse_message(raw, len(raw))
            inserted = store.insert_message(
                account_id=account_id,
                folder=folder,
                uidvalidity=0,
                uid=uid,
                message_id=parsed["messageId"],
                subject=parsed["subject"],
                from_addr=parsed["fromAddr"],
                to_addr=parsed["toAddr"],
                cc_addr=parsed["ccAddr"],
                date=parsed["date"],
                snippet=parsed["snippet"],
                body_text=parsed["text"],
                body_html=parsed["html"],
                size=parsed["size"],
            )
            fetched += 1
            if inserted:
                stored += 1
            else:
                skipped += 1
            batch_last_uid = max(batch_last_uid, uid)
    except MailCoreError as exc:
        _record_error(
            store,
            account_id,
            folder,
            uidvalidity=0,
            last_uid=batch_last_uid,
            error=exc.message,
        )
        raise
    except _POP3_FAILURES as exc:
        message = _redact(exc, password)
        _record_error(
            store,
            account_id,
            folder,
            uidvalidity=0,
            last_uid=batch_last_uid,
            error=message,
        )
        raise pop3_client.Pop3Error(f"sync failed: {message}") from None
    finally:
        if connection is not None:
            try:
                connection.quit()
            except Exception:
                try:
                    connection.close()
                except Exception:
                    pass

    store.set_sync_state(
        account_id,
        folder,
        uidvalidity=0,
        last_uid=batch_last_uid,
        last_sync_at=_now(),
        status="idle",
        error="",
    )
    return _result(
        account_id,
        folder,
        0,
        fetched,
        stored,
        skipped,
        batch_last_uid,
        started_at,
    )
